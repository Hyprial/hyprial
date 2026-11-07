// Pinned Tailcat selects one arbitrary map region for its initial Ping.
// Try advertised regions sequentially after real Ping failure, never after a
// diagnostic log. Share startup time while reserving up to ten seconds for a
// healthy handshake/control exchange. Close before reusing the derived identity.
// Once registered, retain the transport even when a later port or Ping fails.
// A bounded ten-minute authority/map hint survives lease teardown, never the
// transport. Failed attempts clear hints; engine generations reset the cache.
// Rejected or hanging regions consume their whole share on the unpatched
// tailscale.com dependency: no typed exhaustion notification exists there, so
// regional fallback relies only on this per-exchange budget and the hint.
// Proxied nodes of a multi-node region become single-node candidates
// (2026-11-03, #2322 review): derphttp dials a region's nodes in order with no
// per-node deadline, so a proxy that hangs the first node's CONNECT starved
// healthy later nodes; one candidate per proxied node gives each its own share.
// Direct nodes (no effective proxy, NO_PROXY honoured) stay grouped exactly as
// before (#2322 re-review): without a proxy the upstream regional Ping and its
// ten-second cap, transport lifecycle and diagnostics are unchanged.
package outbound

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"net"
	"slices"
	"sync"
	"time"

	"github.com/tailscale/tailcat"
	"tailscale.com/tailcfg"
	"tailscale.com/types/key"
)

type transport interface {
	Ping(context.Context) (tailcat.PingResult, error)
	DialTCPPort(context.Context, uint16) (net.Conn, error)
	DrainTCP(context.Context) error
	Close() error
}

type Client struct {
	mu            sync.Mutex
	startup       chan struct{}
	lifetime      context.Context
	cancel        context.CancelFunc
	current       transport
	addresses     []tailcat.Addr
	next          int
	established   bool
	restart       bool
	closed        bool
	makeTransport func(tailcat.Addr) transport
	hints         *RegionHints
	hintKey       [32]byte
	hintEpoch     uint64
	regionIDs     []tailcfg.DERPRegionID
}

type regionHint struct {
	region  tailcfg.DERPRegionID
	expires time.Time
}

type RegionHints struct {
	mu      sync.Mutex
	entries map[[32]byte]regionHint
	epoch   uint64
}

func (h *RegionHints) Clear() {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.epoch++
	h.entries = nil
}

func (h *RegionHints) lookup(k [32]byte) (tailcfg.DERPRegionID, uint64) {
	h.mu.Lock()
	defer h.mu.Unlock()
	entry := h.entries[k]
	if !time.Now().Before(entry.expires) {
		delete(h.entries, k)
		return 0, h.epoch
	}
	return entry.region, h.epoch
}

func (h *RegionHints) remember(k [32]byte, epoch uint64, region tailcfg.DERPRegionID) {
	if h == nil || region == 0 {
		return
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if epoch != h.epoch {
		return
	}
	if h.entries == nil {
		h.entries = make(map[[32]byte]regionHint)
	}
	if _, exists := h.entries[k]; !exists && len(h.entries) >= 128 {
		var oldest [32]byte
		var expiry time.Time
		for candidate, entry := range h.entries {
			if expiry.IsZero() || entry.expires.Before(expiry) {
				oldest, expiry = candidate, entry.expires
			}
		}
		delete(h.entries, oldest)
	}
	h.entries[k] = regionHint{region: region, expires: time.Now().Add(10 * time.Minute)}
}

func (h *RegionHints) forget(k [32]byte, epoch uint64, region tailcfg.DERPRegionID) {
	if h == nil {
		return
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if epoch == h.epoch && h.entries[k].region == region {
		delete(h.entries, k)
	}
}

func (h *RegionHints) NewClient(k key.NodePrivate, address tailcat.Addr, info tailcat.ConnInfo, diagnostic *Diagnostics) *Client {
	return newClient(k, address, info, diagnostic, h, environment.proxiesNode)
}

func NewClient(k key.NodePrivate, address tailcat.Addr, info tailcat.ConnInfo, diagnostic *Diagnostics) *Client {
	return newClient(k, address, info, diagnostic, nil, environment.proxiesNode)
}

func splitProxiedNodes(region *tailcfg.DERPRegion, proxied func(*tailcfg.DERPNode) bool) []*tailcfg.DERPRegion {
	var parts []*tailcfg.DERPRegion
	var direct []*tailcfg.DERPNode
	flush := func() {
		if len(direct) > 0 {
			part := *region
			part.Nodes = direct
			parts = append(parts, &part)
			direct = nil
		}
	}
	for _, node := range region.Nodes {
		if !proxied(node) {
			direct = append(direct, node)
			continue
		}
		flush()
		part := *region
		part.Nodes = []*tailcfg.DERPNode{node}
		parts = append(parts, &part)
	}
	flush()
	if len(parts) <= 1 {
		return []*tailcfg.DERPRegion{region}
	}
	return parts
}

func newClient(k key.NodePrivate, address tailcat.Addr, info tailcat.ConnInfo, diagnostic *Diagnostics, hints *RegionHints, proxied func(*tailcfg.DERPNode) bool) *Client {
	regions := slices.Clone(info.Region)
	slices.SortStableFunc(regions, func(a, b *tailcfg.DERPRegion) int { return int(a.RegionID) - int(b.RegionID) })
	encoded, marshalErr := json.Marshal(struct {
		Authority string
		Regions   []*tailcfg.DERPRegion
	}{info.ServerPublic.String(), regions})
	if marshalErr != nil {
		hints = nil
	}
	hintKey := sha256.Sum256(encoded)
	var epoch uint64
	if hints != nil {
		var preferred tailcfg.DERPRegionID
		preferred, epoch = hints.lookup(hintKey)
		for i, region := range regions {
			if region.RegionID == preferred {
				ordered := append([]*tailcfg.DERPRegion{region}, regions[:i]...)
				regions = append(ordered, regions[i+1:]...)
				break
			}
		}
	}
	var candidates []*tailcfg.DERPRegion
	for _, region := range regions {
		candidates = append(candidates, splitProxiedNodes(region, proxied)...)
	}
	addresses := []tailcat.Addr{address}
	regionIDs := []tailcfg.DERPRegionID{0}
	if len(candidates) == 1 {
		regionIDs[0] = candidates[0].RegionID
	}
	if len(candidates) > 1 {
		addresses, regionIDs = nil, nil
		for _, region := range candidates {
			candidate := info
			candidate.Region = []*tailcfg.DERPRegion{region}
			addresses = append(addresses, candidate.Addr())
			regionIDs = append(regionIDs, region.RegionID)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	c := &Client{
		startup: make(chan struct{}, 1), lifetime: ctx, cancel: cancel,
		addresses: addresses, regionIDs: regionIDs, hints: hints, hintKey: hintKey, hintEpoch: epoch,
		makeTransport: func(a tailcat.Addr) transport {
			return &tailcat.Client{Key: k, Server: a, Logf: diagnostic.Logf}
		},
	}
	c.current = c.makeTransport(addresses[0])
	c.next = 1
	return c
}

func (c *Client) Ping(parent context.Context) (tailcat.PingResult, error) {
	ctx, cancel := context.WithTimeout(parent, 30*time.Second)
	stop := context.AfterFunc(c.lifetime, cancel)
	defer func() { stop(); cancel() }()
	select {
	case c.startup <- struct{}{}:
		defer func() { <-c.startup }()
	case <-ctx.Done():
		return tailcat.PingResult{}, ctx.Err()
	}
	deadline, _ := ctx.Deadline()
	reserve := min(10*time.Second, time.Until(deadline)/3)
	for {
		c.mu.Lock()
		if c.closed || ctx.Err() != nil {
			c.mu.Unlock()
			return tailcat.PingResult{}, context.Canceled
		}
		if c.restart && len(c.addresses) > 1 {
			if err := c.current.Close(); err != nil {
				c.mu.Unlock()
				return tailcat.PingResult{}, errors.New("previous regional transport did not close")
			}
			c.current = c.makeTransport(c.addresses[0])
			c.next = 1
		}
		c.restart = false
		current := c.current
		remaining := len(c.addresses) - c.next + 1
		established := c.established
		c.mu.Unlock()
		attempt := ctx
		stopAttempt := func() {}
		if !established && remaining > 1 {
			budget := time.Until(deadline)
			if budget > reserve {
				budget -= reserve
			} else {
				budget /= 2
			}
			attempt, stopAttempt = context.WithTimeout(ctx, budget/time.Duration(remaining))
		}
		result, err := current.Ping(attempt)
		stopAttempt()
		c.mu.Lock()
		if err == nil && !c.closed {
			c.established = true
			c.hints.remember(c.hintKey, c.hintEpoch, c.regionIDs[c.next-1])
		} else if err != nil {
			c.hints.forget(c.hintKey, c.hintEpoch, c.regionIDs[c.next-1])
		}
		if err == nil || c.closed || ctx.Err() != nil || c.established || c.next == len(c.addresses) {
			c.restart = err != nil && !c.established
			c.mu.Unlock()
			return result, err
		}
		if closeErr := current.Close(); closeErr != nil {
			c.mu.Unlock()
			return tailcat.PingResult{}, errors.New("previous regional transport did not close")
		}
		c.current = c.makeTransport(c.addresses[c.next])
		c.next++
		c.mu.Unlock()
	}
}

func (c *Client) snapshot() transport {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.current
}

func (c *Client) DialTCPPort(ctx context.Context, port uint16) (net.Conn, error) {
	c.mu.Lock()
	current, region := c.current, c.regionIDs[c.next-1]
	c.mu.Unlock()
	conn, err := current.DialTCPPort(ctx, port)
	if err != nil {
		c.hints.forget(c.hintKey, c.hintEpoch, region)
	}
	return conn, err
}

func (c *Client) DrainTCP(ctx context.Context) error {
	return c.snapshot().DrainTCP(ctx)
}

func (c *Client) Close() error {
	c.cancel()
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return nil
	}
	c.closed = true
	return c.current.Close()
}
