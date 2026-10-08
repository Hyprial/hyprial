package engine

import (
	"context"
	"errors"
	"fmt"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"github.com/tailscale/tailcat"
	"tailscale.com/types/logger"
)

type pooledClient struct {
	address      string
	client       *tailcat.Client
	refs         int
	retired      bool
	ready        atomic.Bool
	closeOnce    sync.Once
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
// mesh and service listeners for one secret address. The caller holds e.mu.
func (e *Engine) acquireClientLocked(address string) *clientLease {
	entry := e.clients[address]
	if entry == nil || entry.retired {
		client := &tailcat.Client{
			Key: e.material.ClientPrivate, Server: tailcat.Addr(address),
			Logf: logger.Logf(quiet),
		}
		entry = &pooledClient{address: address, client: client}
		e.clients[address] = entry
	}
	entry.refs++
	return &clientLease{entry: entry}
}

// dialTCPPort retires a failed client generation so a later mapping gets a
// fresh Client and repeats Tailcat's admission handshake. Existing leases keep
// their generation until they are explicitly remapped or released.
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
		e.retireClient(entry)
		return nil, errClientUnavailable
	}
	remote, err := client.DialTCPPort(ctx, port)
	if err != nil {
		e.retireClient(entry)
		return nil, err
	}
	entry.ready.Store(true)
	return remote, nil
}

func (e *Engine) retireClient(entry *pooledClient) {
	if entry == nil {
		return
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if entry.retired {
		return
	}
	entry.retired = true
	if e.clients[entry.address] == entry {
		delete(e.clients, entry.address)
	}
}

// releaseClientLocked drops one listener lease. The transport closes only
// after the final mesh/service lease is gone. The caller holds e.mu.
func (e *Engine) releaseClientLocked(lease *clientLease) {
	if lease == nil || lease.entry == nil {
		return
	}
	entry := lease.entry
	lease.entry = nil
	if entry.refs > 0 {
		entry.refs--
	}
	if entry.refs > 0 {
		return
	}
	entry.retired = true
	if e.clients[entry.address] == entry {
		delete(e.clients, entry.address)
	}
	e.closeClientLocked(entry)
}

func (e *Engine) closeClientLocked(entry *pooledClient) {
	entry.closeOnce.Do(func() {
		e.wg.Add(1)
		go func() {
			defer e.wg.Done()
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
		}()
	})
}

func (e *Engine) clientLeaseCount(address string) int {
	e.mu.Lock()
	defer e.mu.Unlock()
	if client := e.clients[address]; client != nil {
		return client.refs
	}
	return 0
}
