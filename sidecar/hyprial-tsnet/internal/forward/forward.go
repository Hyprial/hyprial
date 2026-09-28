// Package forward provides the tsnet-to-loopback and loopback-to-tsnet TCP
// data plane. It knows no h2b identities or Zenoh protocol; it only owns TCP
// listeners, dials, and connection lifetime.
package forward

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"sort"
	"strconv"
	"sync"
	"time"
)

const dialTimeout = 5 * time.Second

// Overlay is the exact part of tsnet.Server the forwarding layer uses.
type Overlay interface {
	Listen(network, address string) (net.Listener, error)
	Dial(context.Context, string, string) (net.Conn, error)
}

type Config struct {
	// InboundTarget is an explicitly supplied local TCP listener. The
	// forwarding layer never assumes an isolated HYPRIAL_HOME has one.
	InboundTarget string
	// PeerPort is supplied by h2b from daemon.discovery.DEFAULT_PEER_PORT.
	PeerPort int
}

type Mapping struct {
	Peer      string
	LocalPort int
}

type connPair struct {
	left, right net.Conn
	once        sync.Once
}

func (p *connPair) close() {
	p.once.Do(func() {
		_ = p.left.Close()
		_ = p.right.Close()
	})
}

type mappedPeer struct {
	mapping  Mapping
	listener net.Listener
	ctx      context.Context
	cancel   context.CancelFunc
	pairs    map[*connPair]struct{}
}

type Forwarder struct {
	overlay Overlay
	config  Config
	ctx     context.Context
	cancel  context.CancelFunc

	mu           sync.Mutex
	closed       bool
	inbound      net.Listener
	inboundPairs map[*connPair]struct{}
	mapped       map[string]*mappedPeer
	wg           sync.WaitGroup
}

func New(overlay Overlay, config Config) (*Forwarder, error) {
	if overlay == nil {
		return nil, errors.New("forward overlay is required")
	}
	if config.PeerPort <= 0 || config.PeerPort > 65535 {
		return nil, fmt.Errorf("invalid peer port %d", config.PeerPort)
	}
	target, err := netip.ParseAddrPort(config.InboundTarget)
	if err != nil || !target.Addr().IsLoopback() || target.Port() == 0 {
		return nil, fmt.Errorf(
			"inbound target must be an explicit loopback host:port; got %q",
			config.InboundTarget,
		)
	}
	listener, err := overlay.Listen("tcp", ":"+strconv.Itoa(config.PeerPort))
	if err != nil {
		return nil, fmt.Errorf("listen on overlay port %d: %w", config.PeerPort, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	forwarder := &Forwarder{
		overlay:      overlay,
		config:       config,
		ctx:          ctx,
		cancel:       cancel,
		inbound:      listener,
		inboundPairs: make(map[*connPair]struct{}),
		mapped:       make(map[string]*mappedPeer),
	}
	forwarder.wg.Add(1)
	go forwarder.acceptInbound()
	return forwarder, nil
}

func (f *Forwarder) MapPeer(peer string) (Mapping, error) {
	address, err := netip.ParseAddr(peer)
	if err != nil {
		return Mapping{}, fmt.Errorf("peer must be an IP address; got %q", peer)
	}
	peer = address.String()
	f.mu.Lock()
	if f.closed {
		f.mu.Unlock()
		return Mapping{}, errors.New("forwarder is closed")
	}
	if existing := f.mapped[peer]; existing != nil {
		mapping := existing.mapping
		f.mu.Unlock()
		return mapping, nil
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		f.mu.Unlock()
		return Mapping{}, fmt.Errorf("allocate local forwarding port: %w", err)
	}
	local := listener.Addr().(*net.TCPAddr).Port
	ctx, cancel := context.WithCancel(f.ctx)
	mapped := &mappedPeer{
		mapping:  Mapping{Peer: peer, LocalPort: local},
		listener: listener,
		ctx:      ctx,
		cancel:   cancel,
		pairs:    make(map[*connPair]struct{}),
	}
	f.mapped[peer] = mapped
	f.mu.Unlock()

	f.wg.Add(1)
	go f.acceptOutbound(mapped)
	return mapped.mapping, nil
}

func (f *Forwarder) UnmapPeer(peer string) bool {
	address, err := netip.ParseAddr(peer)
	if err != nil {
		return false
	}
	peer = address.String()
	f.mu.Lock()
	mapped := f.mapped[peer]
	if mapped == nil {
		f.mu.Unlock()
		return false
	}
	delete(f.mapped, peer)
	mapped.cancel()
	_ = mapped.listener.Close()
	pairs := make([]*connPair, 0, len(mapped.pairs))
	for pair := range mapped.pairs {
		pairs = append(pairs, pair)
	}
	f.mu.Unlock()
	for _, pair := range pairs {
		pair.close()
	}
	return true
}

func (f *Forwarder) Mappings() []Mapping {
	f.mu.Lock()
	defer f.mu.Unlock()
	result := make([]Mapping, 0, len(f.mapped))
	for _, item := range f.mapped {
		result = append(result, item.mapping)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].Peer < result[j].Peer })
	return result
}

func (f *Forwarder) Close() {
	f.mu.Lock()
	if f.closed {
		f.mu.Unlock()
		return
	}
	f.closed = true
	f.cancel()
	_ = f.inbound.Close()
	pairs := make([]*connPair, 0, len(f.inboundPairs))
	for pair := range f.inboundPairs {
		pairs = append(pairs, pair)
	}
	for _, mapped := range f.mapped {
		mapped.cancel()
		_ = mapped.listener.Close()
		for pair := range mapped.pairs {
			pairs = append(pairs, pair)
		}
	}
	f.mapped = make(map[string]*mappedPeer)
	f.mu.Unlock()
	for _, pair := range pairs {
		pair.close()
	}
	f.wg.Wait()
}

func (f *Forwarder) acceptInbound() {
	defer f.wg.Done()
	for {
		remote, err := f.inbound.Accept()
		if err != nil {
			return
		}
		dialer := net.Dialer{Timeout: dialTimeout}
		local, err := dialer.DialContext(f.ctx, "tcp", f.config.InboundTarget)
		if err != nil {
			_ = remote.Close()
			continue
		}
		pair := &connPair{left: remote, right: local}
		f.mu.Lock()
		if f.closed {
			f.mu.Unlock()
			pair.close()
			return
		}
		f.inboundPairs[pair] = struct{}{}
		f.mu.Unlock()
		f.splice(pair, func() {
			f.mu.Lock()
			delete(f.inboundPairs, pair)
			f.mu.Unlock()
		})
	}
}

func (f *Forwarder) acceptOutbound(mapped *mappedPeer) {
	defer f.wg.Done()
	target := net.JoinHostPort(mapped.mapping.Peer, strconv.Itoa(f.config.PeerPort))
	for {
		local, err := mapped.listener.Accept()
		if err != nil {
			return
		}
		remote, err := f.overlay.Dial(mapped.ctx, "tcp", target)
		if err != nil {
			_ = local.Close()
			continue
		}
		pair := &connPair{left: local, right: remote}
		f.mu.Lock()
		if f.closed || f.mapped[mapped.mapping.Peer] != mapped {
			f.mu.Unlock()
			pair.close()
			return
		}
		mapped.pairs[pair] = struct{}{}
		f.mu.Unlock()
		f.splice(pair, func() {
			f.mu.Lock()
			delete(mapped.pairs, pair)
			f.mu.Unlock()
		})
	}
}

func (f *Forwarder) splice(pair *connPair, cleanup func()) {
	f.wg.Add(1)
	go func() {
		defer f.wg.Done()
		done := make(chan struct{}, 2)
		copyOne := func(destination, source net.Conn) {
			_, _ = io.Copy(destination, source)
			done <- struct{}{}
		}
		go copyOne(pair.left, pair.right)
		go copyOne(pair.right, pair.left)
		<-done
		pair.close()
		<-done
		cleanup()
	}()
}
