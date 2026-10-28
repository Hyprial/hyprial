package engine

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/tailscale/tailcat"
	"tailscale.com/types/logger"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/keys"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/membership"
	"go4.org/mem"
	"tailscale.com/types/key"
)

type pooledClient struct {
	poolKey      string
	address      string
	client       *tailcat.Client
	refs         int
	leases       map[*clientLease]struct{}
	ready        atomic.Bool
	closeOnce    sync.Once
	closeErr     error
	closeForTest func()
}

type clientLease struct {
	entry *pooledClient
}

var errClientUnavailable = errors.New("tailcat client unavailable")

func parseClientAddress(address string) (tailcat.ConnInfo, error) {
	info, err := tailcat.ParseAddr(tailcat.Addr(address))
	if err != nil {
		return tailcat.ConnInfo{}, fmt.Errorf("%w: address is not a valid tailcat address", ErrBadInput)
	}
	return info, nil
}

// acquireClientLocked shares one lazily-started Tailcat transport between all
// mesh and service listeners for one server public key. Address or PSK changes
// cannot create a second connection with the same derived identity while the
// existing generation still has leases. The caller holds e.mu.
func (e *Engine) acquireClientLocked(address string) (*clientLease, error) {
	info, err := parseClientAddress(address)
	if err != nil {
		return nil, err
	}
	poolKey := info.ServerPublic.String()
	entry := e.clients[poolKey]
	if entry == nil || entry.address != address {
		clientKey, err := keys.DeriveClientPrivate(e.material.ClientPrivate, info.ServerPublic.NodePublic)
		if err != nil {
			return nil, err
		}
		client := &tailcat.Client{
			Key: clientKey, Server: tailcat.Addr(address),
			Logf: logger.Logf(quiet),
		}
		replacement := &pooledClient{
			poolKey: poolKey, address: address, client: client,
			leases: make(map[*clientLease]struct{}),
		}
		if entry != nil {
			// Close the old DERP connection before publishing a replacement
			// with the same derived key, then migrate every surviving lease.
			if err := e.closeClientForHandoffLocked(entry); err != nil {
				return nil, fmt.Errorf("%w: previous client generation did not close", errClientUnavailable)
			}
			replacement.refs = entry.refs
			for lease := range entry.leases {
				lease.entry = replacement
				replacement.leases[lease] = struct{}{}
			}
			entry.refs = 0
			entry.leases = nil
		}
		entry = replacement
		e.clients[poolKey] = entry
	}
	lease := &clientLease{entry: entry}
	entry.refs++
	entry.leases[lease] = struct{}{}
	return lease, nil
}

// dialTCPPort keeps the server's client generation after a failed stream
// dial. A port-level failure does not prove that the shared transport is bad,
// and replacing it while mesh or service leases remain would put two DERP
// connections with the same per-server identity on the relay.
func (e *Engine) dialTCPPort(ctx context.Context, lease *clientLease, port uint16) (net.Conn, error) {
	e.mu.Lock()
	if lease == nil || lease.entry == nil {
		e.mu.Unlock()
		return nil, errClientUnavailable
	}
	entry := lease.entry
	client := entry.client
	e.mu.Unlock()
	if client == nil {
		return nil, errClientUnavailable
	}
	// Tailcat's DialTCPPort registers only once per client. A restarted
	// server loses that registration, while this pooled client stays alive.
	// Resend it on the SAME transport; never replace a shared generation
	// merely because a port dial failed. Ping's cached ack is not a fresh
	// health check: the TCP operation must still establish connectivity.
	if _, err := client.Ping(ctx); err != nil {
		return nil, err
	}
	remote, err := client.DialTCPPort(ctx, port)
	if err != nil {
		return nil, err
	}
	entry.ready.Store(true)
	return remote, nil
}

// releaseClientLocked drops one listener lease. The transport closes only
// after the final mesh/service lease is gone. The caller holds e.mu.
func (e *Engine) releaseClientLocked(lease *clientLease) {
	if lease == nil || lease.entry == nil {
		return
	}
	entry := lease.entry
	lease.entry = nil
	delete(entry.leases, lease)
	if entry.refs > 0 {
		entry.refs--
	}
	if entry.refs > 0 {
		return
	}
	if e.clients[entry.poolKey] == entry {
		delete(e.clients, entry.poolKey)
	}
	e.closeClientLocked(entry)
}

func (e *Engine) closeClientForHandoffLocked(entry *pooledClient) error {
	entry.closeOnce.Do(func() {
		if entry.closeForTest != nil {
			entry.closeForTest()
			return
		}
		if entry.client != nil {
			entry.closeErr = entry.client.Close()
		}
	})
	return entry.closeErr
}

func (e *Engine) closeClientLocked(entry *pooledClient) {
	entry.closeOnce.Do(func() {
		if entry.closeForTest != nil {
			entry.closeForTest()
			return
		}
		if entry.client == nil {
			return
		}
		if entry.ready.Load() {
			drainCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			_ = entry.client.DrainTCP(drainCtx)
			cancel()
		}
		_ = entry.client.Close()
	})
}

func (e *Engine) clientLeaseCount(address string) int {
	e.mu.Lock()
	defer e.mu.Unlock()
	poolKey := address
	if info, err := parseClientAddress(address); err == nil {
		poolKey = info.ServerPublic.String()
	}
	if client := e.clients[poolKey]; client != nil {
		return client.refs
	}
	return 0
}

// diskDERPMapCache persists Tailcat DERP map responses owner-only so a
// temporary map fetch failure falls back to the last good bytes.
type diskDERPMapCache struct{ dir string }

func (c diskDERPMapCache) paths(rawURL string) (string, string) {
	base := filepath.Join(c.dir, "derpmap-"+url.QueryEscape(rawURL))
	return base + ".json", base + ".etag"
}

func (c diskDERPMapCache) Get(rawURL string) ([]byte, string, time.Time, bool) {
	dataPath, etagPath := c.paths(rawURL)
	info, err := os.Stat(dataPath)
	if err != nil {
		return nil, "", time.Time{}, false
	}
	data, err := os.ReadFile(dataPath)
	if err != nil {
		return nil, "", time.Time{}, false
	}
	etag, _ := os.ReadFile(etagPath)
	return data, strings.TrimSpace(string(etag)), info.ModTime(), true
}

func (c diskDERPMapCache) Put(rawURL string, data []byte, etag string) error {
	if err := os.MkdirAll(c.dir, 0o700); err != nil {
		return err
	}
	if err := os.Chmod(c.dir, 0o700); err != nil {
		return err
	}
	dataPath, etagPath := c.paths(rawURL)
	if err := os.WriteFile(dataPath, data, 0o600); err != nil {
		return err
	}
	if err := os.Chmod(dataPath, 0o600); err != nil {
		return err
	}
	if err := os.WriteFile(etagPath, []byte(etag), 0o600); err != nil {
		return err
	}
	return os.Chmod(etagPath, 0o600)
}

func (e *Engine) startMembership(cfg UpConfig) error {
	self := membership.Sender{Owner: cfg.Owner, DeviceID: cfg.DeviceID, MasterPublic: e.material.ClientPublic().String(), ServerPublic: e.material.ServerPublic().String(), Address: string(e.server.TailcatAddr())}
	master := e.material.ClientPrivate
	derive := func(server string) (string, error) {
		pub, err := key.ParseNodePublicUntyped(mem.S(strings.TrimPrefix(server, "nodekey:")))
		if err != nil {
			return "", ErrBadInput
		}
		d, err := keys.DeriveClientPrivate(master, pub)
		if err != nil {
			return "", err
		}
		return d.Public().String(), nil
	}
	member, err := membership.Open(cfg.MembershipStore, self, derive, e.dialControl)
	if err != nil {
		return err
	}
	e.mu.Lock()
	e.membership = member
	e.controlPairs = map[net.Conn]struct{}{}
	e.mu.Unlock()
	member.SetHooks(func(id string, rev uint64, code string) { e.membershipChanged(id, rev, code) }, func() bool {
		e.mu.Lock()
		defer e.mu.Unlock()
		return len(e.mapped) == 0 && len(e.services) == 0 && len(e.inboundPairs) == 0 && len(e.exposed) == 0
	})
	port := cfg.ControlPort
	if port == 0 {
		port = 7448
	}
	ctx, cancel := context.WithTimeout(e.ctx, 30*time.Second)
	defer cancel()
	listener, err := e.server.Listen(ctx, "tcp", ":"+strconv.Itoa(port))
	if err != nil {
		return err
	}
	e.mu.Lock()
	e.control = listener
	e.wg.Add(2)
	e.mu.Unlock()
	go func() { defer e.wg.Done(); member.Run(e.ctx) }()
	go func() {
		defer e.wg.Done()
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			pub, ok := e.inboundPeerKey(conn)
			if !ok {
				conn.Close()
				continue
			}
			e.mu.Lock()
			if !e.up || len(e.controlPairs) >= 64 {
				e.mu.Unlock()
				conn.Close()
				continue
			}
			e.controlPairs[conn] = struct{}{}
			e.wg.Add(1)
			e.mu.Unlock()
			go func() {
				defer e.wg.Done()
				member.ServeControl(conn, pub.String())
				e.mu.Lock()
				delete(e.controlPairs, conn)
				e.mu.Unlock()
			}()
		}
	}()
	return nil
}

type controlConn struct {
	net.Conn
	once    sync.Once
	release func()
}

func (c *controlConn) Close() error { err := c.Conn.Close(); c.once.Do(c.release); return err }
func (e *Engine) dialControl(ctx context.Context, a membership.Authority) (net.Conn, error) {
	e.mu.Lock()
	if !e.up {
		e.mu.Unlock()
		return nil, ErrNotUp
	}
	lease, err := e.acquireClientLocked(a.Address)
	e.mu.Unlock()
	if err != nil {
		return nil, err
	}
	release := func() { e.mu.Lock(); e.releaseClientLocked(lease); e.mu.Unlock() }
	conn, err := e.dialTCPPort(ctx, lease, uint16(a.ControlPort))
	if err != nil {
		release()
		return nil, err
	}
	return &controlConn{Conn: conn, release: release}, nil
}
func (e *Engine) Membership(ctx context.Context, r membership.Request) (json.RawMessage, error) {
	e.mu.Lock()
	m := e.membership
	e.mu.Unlock()
	if m == nil {
		return nil, ErrNotUp
	}
	return m.Execute(ctx, r)
}
func (e *Engine) Readiness() membership.Readiness {
	e.mu.Lock()
	m := e.membership
	up := e.up
	e.mu.Unlock()
	if m == nil || !up {
		return membership.Readiness{}
	}
	return m.Readiness()
}
func (e *Engine) permitsAddressLocked(address string) bool {
	return e.membership == nil || e.membership.AllowsAddress(address)
}
func (e *Engine) membershipChanged(id string, rev uint64, code string) {
	e.mu.Lock()
	m := e.membership
	if !e.up || m == nil {
		e.mu.Unlock()
		return
	}
	for pair := range e.inboundPairs {
		if !m.Admits(pair.peerKey.String()) {
			pair.close()
			e.disconnected.Add(1)
		}
	}
	for _, exposed := range e.exposed {
		for pair := range exposed.pairs {
			if !m.Admits(pair.peerKey.String()) {
				pair.close()
				e.disconnected.Add(1)
			}
		}
	}
	for name, mapped := range e.mapped {
		if !m.AllowsAddress(mapped.address) {
			e.unmapLocked(name, mapped)
		}
	}
	for name, service := range e.services {
		if !m.AllowsAddress(service.request.Address) {
			e.unmapServiceLocked(name, service)
		}
	}
	hook := e.MembershipChanged
	e.mu.Unlock()
	if hook != nil {
		hook(id, rev, code)
	}
}
