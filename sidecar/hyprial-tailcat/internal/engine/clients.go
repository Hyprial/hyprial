package engine

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/tailscale/tailcat"
	"tailscale.com/types/logger"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/keys"
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
