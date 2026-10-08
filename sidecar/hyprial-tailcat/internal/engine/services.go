package engine

import (
	"context"
	"errors"
	"fmt"
	"net"
	"regexp"
	"sort"
	"strconv"
	"time"
)

var serviceName = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)

const (
	// MaxServiceConnections is the per-mapping cap across active and in-flight
	// local connections. Excess local callers are closed before a Tailcat dial.
	MaxServiceConnections   = 64
	serviceAcceptBackoffMin = 5 * time.Millisecond
	serviceAcceptBackoffMax = time.Second
)

// ServiceRequest is the private, validated map-service payload. Address stays
// in memory and is deliberately absent from ServiceMapping.
type ServiceRequest struct {
	Name             string
	DeviceID         string
	Address          string
	ServerPublic     string
	RemotePort       int
	RecordGeneration int
	LocalPort        int
}

// ServiceMapping is the complete non-secret observation returned on the wire.
type ServiceMapping struct {
	Name              string
	DeviceID          string
	RemotePort        int
	RecordGeneration  int
	LocalPort         int
	ActiveConnections int
	Path              string
	PathDetail        *string
	ObservedAtMs      *int64
	LastDialState     string
	LastDialMs        *float64
	LastError         *string
	LastErrorAtMs     *int64
}

type mappedService struct {
	request  ServiceRequest
	mapping  ServiceMapping
	listener net.Listener
	lease    *clientLease
	cancel   context.CancelFunc
	pairs    map[*connPair]struct{}
	pending  int
}

func validateServiceRequest(request ServiceRequest) error {
	if !serviceName.MatchString(request.Name) || !serviceName.MatchString(request.DeviceID) {
		return fmt.Errorf("%w: invalid service identity", ErrBadInput)
	}
	if request.RemotePort < 1 || request.RemotePort > 65535 || request.RecordGeneration < 1 ||
		(request.LocalPort != 0 && (request.LocalPort < 1024 || request.LocalPort > 65535)) {
		return fmt.Errorf("%w: invalid service port or generation", ErrBadInput)
	}
	info, err := parseClientAddress(request.Address)
	if err != nil {
		return err
	}
	if info.ServerPublic.String() != request.ServerPublic {
		return fmt.Errorf("%w: tailcat address does not match serverPublic", ErrBadInput)
	}
	return nil
}

func sameServiceRequest(existing ServiceRequest, requested ServiceRequest, actualPort int) bool {
	return existing.Name == requested.Name && existing.DeviceID == requested.DeviceID &&
		existing.Address == requested.Address && existing.ServerPublic == requested.ServerPublic &&
		existing.RemotePort == requested.RemotePort &&
		existing.RecordGeneration == requested.RecordGeneration &&
		(requested.LocalPort == 0 || requested.LocalPort == actualPort)
}

// MapService binds one literal loopback listener. It does not dial until a
// local connection arrives, so a successful map reports unknown upstream
// health and path rather than inventing readiness.
func (e *Engine) MapService(request ServiceRequest) (ServiceMapping, error) {
	e.mu.Lock()
	if !e.up {
		e.mu.Unlock()
		return ServiceMapping{}, ErrNotUp
	}
	e.mu.Unlock()
	if err := validateServiceRequest(request); err != nil {
		return ServiceMapping{}, err
	}
	e.mu.Lock()
	if !e.up {
		e.mu.Unlock()
		return ServiceMapping{}, ErrNotUp
	}
	if existing := e.services[request.Name]; existing != nil {
		if sameServiceRequest(existing.request, request, existing.mapping.LocalPort) {
			result := cloneServiceMapping(existing.mapping)
			e.mu.Unlock()
			return result, nil
		}
		e.mu.Unlock()
		return ServiceMapping{}, fmt.Errorf("%w: service name already has a different binding", ErrBadInput)
	}
	listener, err := net.Listen("tcp4", "127.0.0.1:"+strconv.Itoa(request.LocalPort))
	if err != nil {
		e.mu.Unlock()
		return ServiceMapping{}, ErrPortInUse
	}
	localPort := listener.Addr().(*net.TCPAddr).Port
	ctx, cancel := context.WithCancel(e.ctx)
	service := &mappedService{
		request: request, listener: listener, lease: e.acquireClientLocked(request.Address),
		cancel: cancel, pairs: make(map[*connPair]struct{}),
		mapping: ServiceMapping{
			Name: request.Name, DeviceID: request.DeviceID,
			RemotePort: request.RemotePort, RecordGeneration: request.RecordGeneration,
			LocalPort: localPort, Path: "unknown", LastDialState: "unknown",
		},
	}
	e.services[request.Name] = service
	e.wg.Add(1)
	result := cloneServiceMapping(service.mapping)
	e.mu.Unlock()
	go e.acceptService(ctx, service)
	return result, nil
}

// UnmapService is idempotent: removed says whether a listener existed.
func (e *Engine) UnmapService(name string) (bool, error) {
	e.mu.Lock()
	if !e.up {
		e.mu.Unlock()
		return false, ErrNotUp
	}
	service := e.services[name]
	if service == nil {
		e.mu.Unlock()
		return false, nil
	}
	e.unmapServiceLocked(name, service)
	e.mu.Unlock()
	return true, nil
}

func (e *Engine) unmapServiceLocked(name string, service *mappedService) {
	delete(e.services, name)
	service.cancel()
	_ = service.listener.Close()
	for pair := range service.pairs {
		pair.close()
	}
	e.releaseClientLocked(service.lease)
}

// ServiceStatus returns name-sorted immutable copies and never mixes service
// listeners into peer Status.
func (e *Engine) ServiceStatus() ([]ServiceMapping, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if !e.up {
		return nil, ErrNotUp
	}
	result := make([]ServiceMapping, 0, len(e.services))
	for _, service := range e.services {
		result = append(result, cloneServiceMapping(service.mapping))
	}
	sort.Slice(result, func(i, j int) bool { return result[i].Name < result[j].Name })
	return result, nil
}

func cloneServiceMapping(value ServiceMapping) ServiceMapping {
	result := value
	if value.PathDetail != nil {
		copy := *value.PathDetail
		result.PathDetail = &copy
	}
	if value.ObservedAtMs != nil {
		copy := *value.ObservedAtMs
		result.ObservedAtMs = &copy
	}
	if value.LastDialMs != nil {
		copy := *value.LastDialMs
		result.LastDialMs = &copy
	}
	if value.LastError != nil {
		copy := *value.LastError
		result.LastError = &copy
	}
	if value.LastErrorAtMs != nil {
		copy := *value.LastErrorAtMs
		result.LastErrorAtMs = &copy
	}
	return result
}

func (e *Engine) acceptService(ctx context.Context, service *mappedService) {
	defer e.wg.Done()
	backoff := serviceAcceptBackoffMin
	for {
		local, err := service.listener.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			var temporary interface{ Temporary() bool }
			if errors.As(err, &temporary) && temporary.Temporary() {
				timer := time.NewTimer(backoff)
				select {
				case <-ctx.Done():
					if !timer.Stop() {
						<-timer.C
					}
					return
				case <-timer.C:
				}
				backoff *= 2
				if backoff > serviceAcceptBackoffMax {
					backoff = serviceAcceptBackoffMax
				}
				continue
			}
			now := time.Now().UnixMilli()
			message := "service listener failed"
			e.mu.Lock()
			if e.up && e.services[service.request.Name] == service {
				service.mapping.LastDialState = "error"
				service.mapping.LastError = &message
				service.mapping.LastErrorAtMs = &now
			}
			e.mu.Unlock()
			return
		}
		backoff = serviceAcceptBackoffMin
		e.mu.Lock()
		if !e.up || e.services[service.request.Name] != service {
			e.mu.Unlock()
			_ = local.Close()
			return
		}
		if service.mapping.ActiveConnections+service.pending >= MaxServiceConnections {
			e.mu.Unlock()
			_ = local.Close()
			continue
		}
		service.pending++
		e.wg.Add(1)
		e.mu.Unlock()
		go func() {
			defer e.wg.Done()
			e.handleServiceConnection(ctx, service, local)
		}()
	}
}

func (e *Engine) handleServiceConnection(ctx context.Context, service *mappedService, local net.Conn) {
	started := time.Now()
	if hook := e.serviceDialStartedForTest; hook != nil {
		hook(service.request.Name)
	}
	dialCtx, cancel := context.WithTimeout(ctx, dialTimeout)
	remote, err := e.dialTCPPort(dialCtx, service.lease, uint16(service.request.RemotePort))
	cancel()
	duration := float64(time.Since(started)) / float64(time.Millisecond)
	now := time.Now().UnixMilli()
	e.mu.Lock()
	if service.pending > 0 {
		service.pending--
	}
	if !e.up || e.services[service.request.Name] != service {
		e.mu.Unlock()
		_ = local.Close()
		if remote != nil {
			_ = remote.Close()
		}
		return
	}
	service.mapping.LastDialMs = &duration
	if err != nil {
		message := "service dial failed"
		service.mapping.LastDialState = "error"
		service.mapping.LastError = &message
		service.mapping.LastErrorAtMs = &now
		e.mu.Unlock()
		_ = local.Close()
		return
	}
	service.mapping.LastDialState = "ok"
	service.mapping.ActiveConnections++
	pair := &connPair{left: local, right: remote}
	service.pairs[pair] = struct{}{}
	e.mu.Unlock()
	e.splice(pair, func() {
		e.mu.Lock()
		delete(service.pairs, pair)
		if service.mapping.ActiveConnections > 0 {
			service.mapping.ActiveConnections--
		}
		e.mu.Unlock()
	})
}
