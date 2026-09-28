package node

import (
	"context"
	"errors"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tsnet/internal/forward"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tsnet/internal/proto"
	"tailscale.com/ipn"
	"tailscale.com/ipn/ipnstate"
)

const (
	CodeControlUnreachable  = "CONTROL_UNREACHABLE"
	CodeAuthDenied          = "AUTH_DENIED"
	CodeAuthKeyInvalid      = "AUTHKEY_INVALID"
	CodeAuthKeyUnexpected   = "AUTHKEY_UNEXPECTED"
	CodeAuthKeyMissing      = "AUTHKEY_MISSING"
	CodeTimeout             = "TIMEOUT"
	CodeStateDir            = "STATE_DIR_UNWRITABLE"
	CodeForwardStateMissing = "FORWARD_STATE_MISSING"
	CodeForwardUnavailable  = "FORWARD_UNAVAILABLE"
)

type Failure struct {
	Code    string
	Message string
	// Emitted reports that the Manager already attempted to write the `error`
	// event itself, before its slow backend Close. A fatal consumer must not
	// write a second `error`; it only writes `exited` (wire contract:
	// error → exited, once each — only the error timing relative to Close
	// changed, card 6cdcc406).
	Emitted bool
}

type Manager struct {
	restartForPromotion bool
	factory             BackendFactory
	emit                func(proto.Event) error
	fatal               func(Failure)

	mu            sync.Mutex
	backend       Backend
	cancel        context.CancelFunc
	done          chan struct{}
	statePath     string
	pendingOrigin string
	pending       bool
	controlURL    string
	stopping      bool
	wireVersion   int
	forwarder     *forward.Forwarder
	forwardConfig proto.UpRequest
}

func NewManager(factory BackendFactory, emit func(proto.Event) error, fatal func(Failure)) *Manager {
	return &Manager{factory: factory, emit: emit, fatal: fatal, restartForPromotion: runtime.GOOS == "windows"}
}

func (m *Manager) Start(request proto.UpRequest) error {
	if request.Join == "interactive" && request.AuthKey != nil {
		return &codedError{code: CodeAuthKeyUnexpected, message: "interactive join does not accept an auth key"}
	}
	if request.Join == "preauthkey" && !request.Resume && (request.AuthKey == nil || *request.AuthKey == "") {
		return &codedError{code: CodeAuthKeyMissing, message: "preauthkey join requires an auth key"}
	}
	if request.Resume {
		if request.Version != proto.ForwardVersion {
			return proto.ProtocolError("resume requires forwarding protocol v2")
		}
		info, err := os.Stat(filepath.Join(request.Dir, "node"))
		if err != nil || !info.IsDir() {
			return &codedError{code: CodeForwardStateMissing, message: "forwarding resume requires existing node state"}
		}
	}

	m.mu.Lock()
	if m.backend != nil {
		m.mu.Unlock()
		return proto.ProtocolError("up already started")
	}
	m.mu.Unlock()

	statePath, pending, err := prepareStatePath(request.Dir)
	if err != nil {
		return &codedError{code: CodeStateDir, message: "state directory is not writable"}
	}

	config := Config{
		ControlURL: request.ControlURL,
		Hostname:   request.Hostname,
		Dir:        statePath,
		Ephemeral:  request.Ephemeral,
		HasAuthKey: request.AuthKey != nil,
	}
	if request.AuthKey != nil {
		config.AuthKey = *request.AuthKey
	}
	backend := m.factory(config)
	if err := backend.Start(); err != nil {
		removePending(statePath, pending)
		failure := classifyFailure(err, config.HasAuthKey, "")
		return &codedError{code: failure.Code, message: failure.Message}
	}

	ctx, cancel := context.WithCancel(context.Background())
	watcher, err := backend.WatchIPNBus(ctx, ipn.NotifyInitialState)
	if err != nil {
		cancel()
		_ = backend.Close()
		removePending(statePath, pending)
		failure := classifyFailure(err, config.HasAuthKey, "")
		return &codedError{code: failure.Code, message: failure.Message}
	}

	m.mu.Lock()
	m.backend = backend
	m.cancel = cancel
	m.done = make(chan struct{})
	m.statePath = statePath
	if pending {
		m.pendingOrigin = statePath
	}
	m.pending = pending
	m.controlURL = request.ControlURL
	m.wireVersion = request.Version
	if m.wireVersion == 0 {
		m.wireVersion = proto.Version
	}
	m.forwardConfig = request
	m.mu.Unlock()

	var timeout <-chan time.Time
	var timer *time.Timer
	if request.TimeoutSeconds != nil {
		timer = time.NewTimer(time.Duration(*request.TimeoutSeconds) * time.Second)
		timeout = timer.C
	}
	go m.run(ctx, watcher, timeout, timer, config.HasAuthKey, request.Proxy != "", request.ControlURL != "")
	return nil
}

func (m *Manager) run(ctx context.Context, watcher Watcher, timeout <-chan time.Time, timer *time.Timer, hasAuthKey, hasProxy, customControl bool) {
	defer func() {
		if timer != nil {
			timer.Stop()
		}
		_ = watcher.Close()
		m.mu.Lock()
		done := m.done
		m.mu.Unlock()
		close(done)
	}()

	watchCtx, stopWatch := context.WithCancel(ctx)
	defer stopWatch()
	results := watchNotifications(watchCtx, watcher)

	seenURLs := map[string]struct{}{}
	lastState := ""
	ready := false
	sawBrowse := false
	for {
		select {
		case <-ctx.Done():
			return
		case <-timeout:
			failure := Failure{Code: CodeTimeout, Message: "node did not reach Running before timeout"}
			if hasProxy {
				failure = Failure{Code: CodeControlUnreachable, Message: "control plane is unreachable"}
			} else if hasAuthKey && lastState == ipn.NeedsLogin.String() {
				failure = Failure{Code: CodeAuthKeyInvalid, Message: "control plane rejected the auth key"}
			} else if customControl && !sawBrowse {
				failure = Failure{Code: CodeControlUnreachable, Message: "control plane is unreachable"}
			}
			m.fail(failure)
			return
		case item := <-results:
			if item.err != nil {
				if ctx.Err() != nil {
					return
				}
				m.fail(classifyFailure(item.err, hasAuthKey, lastState))
				return
			}
			if item.notify.State != nil {
				lastState = item.notify.State.String()
				stateEvent := proto.State(lastState)
				if m.forwardingVersion() {
					stateEvent = proto.ForwardState(lastState)
				}
				if err := m.emit(stateEvent); err != nil {
					m.fail(Failure{Code: CodeControlUnreachable, Message: "stdout is unavailable"})
					return
				}
				if *item.notify.State == ipn.Running && !ready {
					m.mu.Lock()
					restart := m.restartForPromotion && m.pending
					m.mu.Unlock()
					if restart {
						stopWatch()
						_ = watcher.Close()
						replacement, failure := m.restartPromotedBackend(ctx)
						if failure != nil {
							m.fail(*failure)
							return
						}
						watcher = replacement
						watchCtx, stopWatch = context.WithCancel(ctx)
						// Capture the replacement cancel explicitly; promotion happens once.
						defer stopWatch()
						results = watchNotifications(watchCtx, watcher)
						continue // Require Running from the reopened durable state before ready.
					}
					if failure := m.promoteAndReady(ctx); failure != nil {
						m.fail(*failure)
						return
					}
					ready = true
				}
			}
			if item.notify.BrowseToURL != nil {
				sawBrowse = true
				url := *item.notify.BrowseToURL
				if _, seen := seenURLs[url]; !seen {
					seenURLs[url] = struct{}{}
					if err := m.emit(proto.BrowseToURL(url)); err != nil {
						m.fail(Failure{Code: CodeControlUnreachable, Message: "stdout is unavailable"})
						return
					}
				}
			}
			if item.notify.ErrMessage != nil {
				m.fail(classifyFailure(errors.New(*item.notify.ErrMessage), hasAuthKey, lastState))
				return
			}
		}
	}
}

type watchResult struct {
	notify ipn.Notify
	err    error
}

func watchNotifications(ctx context.Context, watcher Watcher) <-chan watchResult {
	results := make(chan watchResult, 1)
	go func() {
		for {
			notify, err := watcher.Next()
			select {
			case results <- watchResult{notify, err}:
			case <-ctx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()
	return results
}

// Windows cannot rename tsnet's state directory while its backend holds files
// open. Stop it before the atomic rename, then resume the SAME persisted identity.
// Serializing against Stop prevents a replacement backend escaping ownership.
func (m *Manager) restartPromotedBackend(ctx context.Context) (Watcher, *Failure) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.stopping || ctx.Err() != nil {
		return nil, &Failure{Code: CodeStateDir, Message: "node promotion cancelled"}
	}
	if err := m.backend.Close(); err != nil {
		return nil, &Failure{Code: CodeStateDir, Message: "cannot close node state before promotion"}
	}
	nodePath := filepath.Join(filepath.Dir(m.statePath), "node")
	if err := os.Rename(m.statePath, nodePath); err != nil {
		return nil, &Failure{Code: CodeStateDir, Message: "cannot atomically promote closed node state"}
	}
	m.statePath, m.pending = nodePath, false
	request := m.forwardConfig
	// No auth key is replayed: the first backend already completed enrollment.
	backend := m.factory(Config{ControlURL: request.ControlURL, Hostname: request.Hostname, Dir: nodePath, Ephemeral: request.Ephemeral})
	m.backend = backend
	if err := backend.Start(); err != nil {
		return nil, &Failure{Code: CodeStateDir, Message: "cannot resume promoted node state"}
	}
	watcher, err := backend.WatchIPNBus(ctx, ipn.NotifyInitialState)
	if err != nil {
		return nil, &Failure{Code: CodeStateDir, Message: "cannot watch promoted node state"}
	}
	return watcher, nil
}

func (m *Manager) promoteAndReady(ctx context.Context) *Failure {
	m.mu.Lock()
	backend := m.backend
	statePath := m.statePath
	pending := m.pending
	controlURL := m.controlURL
	m.mu.Unlock()

	if pending {
		nodePath := filepath.Join(filepath.Dir(statePath), "node")
		if err := os.Rename(statePath, nodePath); err != nil {
			return &Failure{Code: CodeStateDir, Message: "cannot atomically promote node state"}
		}
		m.mu.Lock()
		m.statePath = nodePath
		m.pending = false
		m.mu.Unlock()
	}
	status, event, err := waitReadyStatus(ctx, backend, controlURL)
	if err != nil {
		return &Failure{Code: CodeControlUnreachable, Message: "Running status is incomplete"}
	}
	version := m.currentWireVersion()
	if version == proto.ForwardVersion {
		dataBackend, ok := backend.(DataBackend)
		if !ok {
			return &Failure{Code: CodeForwardUnavailable, Message: "tsnet backend has no forwarding data plane"}
		}
		m.mu.Lock()
		request := m.forwardConfig
		m.mu.Unlock()
		forwarder, startErr := forward.New(dataBackend, forward.Config{
			InboundTarget: request.InboundTarget,
			PeerPort:      request.PeerPort,
		})
		if startErr != nil {
			return &Failure{Code: CodeForwardUnavailable, Message: "cannot start forwarding data plane"}
		}
		m.mu.Lock()
		m.forwarder = forwarder
		m.mu.Unlock()
	}
	event.V = version
	if version == proto.ForwardVersion {
		peers := peerAddresses(status)
		event.Peers = &peers
	}
	if err != nil {
		return &Failure{Code: CodeControlUnreachable, Message: "Running status is incomplete"}
	}
	if err := m.emit(event); err != nil {
		return &Failure{Code: CodeControlUnreachable, Message: "stdout is unavailable"}
	}
	return nil
}

// IPN's Running notification can precede the LocalAPI's complete status snapshot.
// Keep the strict ready contract, but allow that snapshot to converge after resume.
func waitReadyStatus(ctx context.Context, backend Backend, controlURL string) (*ipnstate.Status, proto.Event, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	for {
		if err := ctx.Err(); err != nil {
			return nil, proto.Event{}, err
		}
		status, err := backend.Status(ctx)
		if err != nil {
			return nil, proto.Event{}, err
		}
		if status != nil && status.BackendState == ipn.Running.String() {
			event, err := statusEvent("ready", status, controlURL)
			if err == nil {
				return status, event, nil
			}
		}
		timer := time.NewTimer(50 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, proto.Event{}, ctx.Err()
		case <-timer.C:
		}
	}
}

func (m *Manager) Status(ctx context.Context) (proto.Event, error) {
	m.mu.Lock()
	backend := m.backend
	controlURL := m.controlURL
	m.mu.Unlock()
	if backend == nil {
		return proto.Event{}, proto.ProtocolError("status before up")
	}
	status, err := backend.Status(ctx)
	if err != nil {
		return proto.Event{}, &codedError{code: CodeControlUnreachable, message: "cannot read node status"}
	}
	return statusEvent("status", status, controlURL)
}

func (m *Manager) ForwardStatus(ctx context.Context) (proto.Event, error) {
	m.mu.Lock()
	backend := m.backend
	controlURL := m.controlURL
	forwarder := m.forwarder
	m.mu.Unlock()
	if backend == nil || forwarder == nil {
		return proto.Event{}, &codedError{code: CodeForwardUnavailable, message: "forwarding data plane is not ready"}
	}
	status, err := backend.Status(ctx)
	if err != nil {
		return proto.Event{}, &codedError{code: CodeControlUnreachable, message: "cannot read node status"}
	}
	event, err := statusEvent("status", status, controlURL)
	if err != nil {
		return proto.Event{}, err
	}
	event.V = proto.ForwardVersion
	peers := peerAddresses(status)
	event.Peers = &peers
	for _, mapping := range forwarder.Mappings() {
		event.Mappings = append(event.Mappings, proto.PeerMapping{
			Peer: mapping.Peer, LocalPort: mapping.LocalPort,
		})
	}
	return event, nil
}

func (m *Manager) MapPeer(peer string) (proto.Event, error) {
	m.mu.Lock()
	forwarder := m.forwarder
	m.mu.Unlock()
	if forwarder == nil {
		return proto.Event{}, &codedError{code: CodeForwardUnavailable, message: "forwarding data plane is not ready"}
	}
	mapping, err := forwarder.MapPeer(peer)
	if err != nil {
		return proto.Event{}, &codedError{code: CodeForwardUnavailable, message: "cannot map forwarding peer"}
	}
	return proto.PeerMapped(mapping.Peer, mapping.LocalPort), nil
}

func (m *Manager) UnmapPeer(peer string) (proto.Event, error) {
	m.mu.Lock()
	forwarder := m.forwarder
	m.mu.Unlock()
	if forwarder == nil {
		return proto.Event{}, &codedError{code: CodeForwardUnavailable, message: "forwarding data plane is not ready"}
	}
	if !forwarder.UnmapPeer(peer) {
		return proto.Event{}, &codedError{code: CodeForwardUnavailable, message: "forwarding peer is not mapped"}
	}
	return proto.PeerUnmapped(peer), nil
}

func (m *Manager) Stop() {
	m.mu.Lock()
	if m.backend == nil {
		m.mu.Unlock()
		return
	}
	m.stopping = true
	cancel, backend, done, forwarder := m.cancel, m.backend, m.done, m.forwarder
	m.forwarder = nil
	m.mu.Unlock()
	cancel()
	if forwarder != nil {
		forwarder.Close()
	}
	_ = backend.Close()
	<-done
	m.mu.Lock()
	pendingOrigin := m.pendingOrigin
	m.mu.Unlock()
	removePending(pendingOrigin, pendingOrigin != "")
}

// fail terminates a failed up in the card-6cdcc406 order: pending state is
// removed and the `error` event is written while the slow backend Close
// (≈5 s in the field) has not even started, so the peer learns the
// classification immediately; Close follows; the fatal signal then carries
// Emitted=true so serve writes only `exited` and returns. A failing emit
// (stdout unavailable) must not wedge shutdown: the attempt still counts as
// emitted — serve must not retry the error on the same broken writer — and
// Close still runs.
func (m *Manager) fail(failure Failure) {
	m.mu.Lock()
	backend, forwarder := m.backend, m.forwarder
	m.forwarder = nil
	statePath, pending := m.statePath, m.pending
	pendingOrigin := m.pendingOrigin
	stopping := m.stopping
	m.mu.Unlock()
	if stopping {
		return
	}
	removePending(statePath, pending)
	if pendingOrigin != statePath {
		removePending(pendingOrigin, pendingOrigin != "")
	}
	errorEvent := proto.Error(failure.Code, failure.Message)
	if m.forwardingVersion() {
		errorEvent = proto.ForwardError(failure.Code, failure.Message)
	}
	_ = m.emit(errorEvent)
	if forwarder != nil {
		forwarder.Close()
	}
	if backend != nil {
		_ = backend.Close()
	}
	failure.Emitted = true
	m.fatal(failure)
}

func (m *Manager) forwardingVersion() bool {
	return m.currentWireVersion() == proto.ForwardVersion
}

func (m *Manager) currentWireVersion() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.wireVersion
}

// peerAddresses returns the addresses of every online peer. It never returns
// nil: an empty list is a real answer ("no online peer yet") and callers take
// its address so the key survives omitempty on the wire.
func peerAddresses(status *ipnstate.Status) []string {
	if status == nil {
		return []string{}
	}
	seen := make(map[string]struct{})
	peers := make([]string, 0)
	for _, peer := range status.Peer {
		if peer == nil || !peer.Online {
			continue
		}
		for _, address := range peer.TailscaleIPs {
			value := address.String()
			if _, ok := seen[value]; ok {
				continue
			}
			seen[value] = struct{}{}
			peers = append(peers, value)
		}
	}
	sort.Strings(peers)
	return peers
}

func prepareStatePath(base string) (path string, pending bool, err error) {
	if err := os.MkdirAll(base, 0o700); err != nil {
		return "", false, err
	}
	nodePath := filepath.Join(base, "node")
	info, statErr := os.Stat(nodePath)
	switch {
	case statErr == nil && info.IsDir():
		probe, err := os.CreateTemp(nodePath, ".write-probe-")
		if err != nil {
			return "", false, err
		}
		probePath := probe.Name()
		if err := probe.Close(); err != nil {
			_ = os.Remove(probePath)
			return "", false, err
		}
		if err := os.Remove(probePath); err != nil {
			return "", false, err
		}
		return nodePath, false, nil
	case statErr == nil:
		return "", false, fmt.Errorf("node path is not a directory")
	case !errors.Is(statErr, os.ErrNotExist):
		return "", false, statErr
	}
	pendingPath, err := os.MkdirTemp(base, ".pending-")
	if err != nil {
		return "", false, err
	}
	if err := os.Chmod(pendingPath, 0o700); err != nil {
		_ = os.RemoveAll(pendingPath)
		return "", false, err
	}
	return pendingPath, true, nil
}

func removePending(path string, pending bool) {
	if pending && path != "" {
		_ = os.RemoveAll(path)
	}
}

func statusEvent(kind string, status *ipnstate.Status, controlURL string) (proto.Event, error) {
	if status == nil || status.Self == nil {
		return proto.Event{}, errors.New("missing self status")
	}
	var ip4, ip6 string
	for _, address := range status.Self.TailscaleIPs {
		if address.Is4() && ip4 == "" {
			ip4 = address.String()
		}
		if address.Is6() && ip6 == "" {
			ip6 = address.String()
		}
	}
	if ip4 == "" || ip6 == "" {
		return proto.Event{}, errors.New("missing Tailscale IP")
	}
	profile, ok := status.User[status.Self.UserID]
	if !ok || profile.LoginName == "" {
		return proto.Event{}, errors.New("missing user profile")
	}
	hexKey := status.Self.PublicKey.UntypedHexString()
	if len(hexKey) < 16 {
		return proto.Event{}, errors.New("missing node public key")
	}
	tags := make([]string, 0)
	if status.Self.Tags != nil {
		for _, tag := range status.Self.Tags.All() {
			tags = append(tags, tag)
		}
	}
	control := controlURL
	event := proto.Event{
		V: proto.Version, Event: kind, State: status.BackendState,
		IP4: ip4, IP6: ip6, Hostname: strings.TrimSuffix(status.Self.DNSName, "."),
		User: profile.LoginName, NodeKeyFingerprint: "nodekey:" + hexKey[:16],
		ControlURL: &control,
	}
	if kind == "ready" {
		event.Tags = &tags
	}
	return event, nil
}

func classifyFailure(err error, hasAuthKey bool, lastState string) Failure {
	text := strings.ToLower(err.Error())
	switch {
	case strings.Contains(text, "auth key"), strings.Contains(text, "authkey"), strings.Contains(text, "invalid key"), strings.Contains(text, "unauthorized"), strings.Contains(text, "status 401"):
		return Failure{Code: CodeAuthKeyInvalid, Message: "control plane rejected the auth key"}
	case strings.Contains(text, "denied"), strings.Contains(text, "rejected"):
		return Failure{Code: CodeAuthDenied, Message: "control plane denied authentication"}
	case errors.Is(err, context.DeadlineExceeded):
		if hasAuthKey && lastState == ipn.NeedsLogin.String() {
			return Failure{Code: CodeAuthKeyInvalid, Message: "control plane rejected the auth key"}
		}
		return Failure{Code: CodeTimeout, Message: "node did not reach Running before timeout"}
	default:
		return Failure{Code: CodeControlUnreachable, Message: "control plane is unreachable"}
	}
}

type codedError struct {
	code    string
	message string
}

func (e *codedError) Error() string { return e.code + ": " + e.message }
func ErrorDetails(err error) (string, string, bool) {
	var coded *codedError
	if errors.As(err, &coded) {
		return coded.code, coded.message, true
	}
	return "", "", false
}

// Keep netip linked explicitly in this package's status seam; it catches
// accidental replacement of real netip addresses with strings in tests.
var _ netip.Addr
