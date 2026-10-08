// Package engine owns the Tailcat server lifecycle and data plane for the
// hyprial-tailcat sidecar: up/down, the admission set, mapped peers,
// exposed ports, and peer-key lookup. It speaks no stdio protocol; the
// serve package drives it.
//
// Secret discipline (design doc §3.2): private keys, the pre-shared key,
// and Tailcat addresses (which embed the PSK) never enter argv, stdout
// events, or logs. The address is written only to the 0600 addressFile;
// peer addresses arrive via map-peer and are kept in memory only.
package engine

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/tailscale/tailcat"
	"go4.org/mem"
	"tailscale.com/tailcfg"
	"tailscale.com/types/key"
	"tailscale.com/types/logger"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/keys"
)

const dialTimeout = 5 * time.Second

// quiet discards Tailcat's internal debug logs so secrets (node keys in
// URLs, ConnInfo material) can never leak through the logging path.
func quiet(string, ...any) {}

// DERPOverride selects a local test relay instead of the default DERP
// map (design doc §3.3, up.derp; test-only).
type DERPOverride struct {
	Host             string
	DERPPort         int
	STUNPort         int
	InsecureForTests bool
}

// UpConfig carries the validated fields of an up command.
type UpConfig struct {
	KeyFile       string
	Allow         []string
	AllowAny      bool
	InboundTarget string // "tcp:127.0.0.1:N" or empty
	PeerPort      int
	AddressFile   string
	DERP          *DERPOverride
}

// Mapping describes one mapped peer.
type Mapping struct {
	Peer      string
	LocalPort int
}

// Exposure describes one exposed Tailcat port.
type Exposure struct {
	Port          int
	Target        string
	ProxyProtocol string
}

// Sentinel errors mapped to protocol error codes by the serve layer.
var (
	ErrNotUp       = errors.New("engine is not up")
	ErrAlreadyUp   = errors.New("engine is already up")
	ErrPeerUnknown = errors.New("unknown peer")
	ErrPortInUse   = errors.New("port already in use")
	ErrBadInput    = errors.New("bad input")
)

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
	address  string // secret: never logged or emitted
	port     uint16
	listener net.Listener
	lease    *clientLease
	cancel   context.CancelFunc
	pairs    map[*connPair]struct{}
}

type exposedPort struct {
	exposure Exposure
	listener net.Listener
	cancel   context.CancelFunc
	pairs    map[*connPair]struct{}
}

// Engine is the sidecar's single Tailcat server plus its mapped peers
// and exposures. The zero value is down; call Up.
type Engine struct {
	mu        sync.Mutex
	up        bool
	server    *tailcat.Server
	material  keys.Material
	allowKeys *tailcat.KeySet
	allowAny  atomic.Bool

	ctx    context.Context
	cancel context.CancelFunc

	inbound      net.Listener // peerPort listener toward inboundTarget
	inboundPairs map[*connPair]struct{}
	mapped       map[string]*mappedPeer
	services     map[string]*mappedService
	clients      map[string]*pooledClient
	exposed      map[int]*exposedPort
	deniedCount  atomic.Int64
	disconnected atomic.Int64
	wg           sync.WaitGroup

	serviceDialStartedForTest func(string)
}

func regionFromOverride(d *DERPOverride) *tailcfg.DERPRegion {
	host := d.Host
	ipv4 := "none"
	if addr, err := netip.ParseAddr(host); err == nil && addr.Is4() {
		ipv4 = host
	}
	return &tailcfg.DERPRegion{
		RegionID:   1,
		RegionCode: "local",
		Nodes: []*tailcfg.DERPNode{{
			Name:             "local1",
			RegionID:         1,
			HostName:         host,
			IPv4:             ipv4,
			IPv6:             "none",
			DERPPort:         d.DERPPort,
			STUNPort:         d.STUNPort,
			STUNTestIP:       host,
			InsecureForTests: d.InsecureForTests,
		}},
	}
}

// Up starts the Tailcat server from the key file, installs the admission
// set, writes the Tailcat address to addressFile (0600), and, when an
// inboundTarget is configured, listens on PeerPort and proxies to it.
func (e *Engine) Up(ctx context.Context, cfg UpConfig) (serverPublic, clientPublic string, err error) {
	e.mu.Lock()
	if e.up {
		e.mu.Unlock()
		return "", "", ErrAlreadyUp
	}
	e.mu.Unlock()

	material, err := keys.Load(cfg.KeyFile)
	if err != nil {
		return "", "", err
	}
	allowSet, err := parseAllowList(cfg.Allow)
	if err != nil {
		return "", "", err
	}
	server := &tailcat.Server{
		Key:          material.ServerPrivate,
		PresharedKey: material.PresharedKey,
		Logf:         logger.Logf(quiet),
	}
	if cfg.DERP != nil {
		server.Region = regionFromOverride(cfg.DERP)
	}
	engineCtx, cancel := context.WithCancel(context.Background())

	e.mu.Lock()
	e.allowKeys = allowSet
	e.allowAny.Store(cfg.AllowAny)
	e.mu.Unlock()
	server.AllowClient = func(k key.NodePublic) bool {
		e.mu.Lock()
		set := e.allowKeys
		any := e.allowAny.Load()
		e.mu.Unlock()
		if any || (set != nil && set.Contains(k)) {
			return true
		}
		e.deniedCount.Add(1)
		return false
	}

	if err := server.Start(); err != nil {
		cancel()
		return "", "", fmt.Errorf("start tailcat server: %w", err)
	}
	// The address embeds the pre-shared key; it goes only to the 0600 file.
	if err := writeAddressFile(cfg.AddressFile, string(server.TailcatAddr())); err != nil {
		_ = server.Close()
		cancel()
		return "", "", fmt.Errorf("write address file: %w", err)
	}

	e.mu.Lock()
	e.up = true
	e.server = server
	e.material = material
	e.ctx = engineCtx
	e.cancel = cancel
	e.inboundPairs = make(map[*connPair]struct{})
	e.mapped = make(map[string]*mappedPeer)
	e.services = make(map[string]*mappedService)
	e.clients = make(map[string]*pooledClient)
	e.exposed = make(map[int]*exposedPort)
	e.mu.Unlock()

	if cfg.InboundTarget != "" {
		listenCtx, listenCancel := context.WithTimeout(ctx, 30*time.Second)
		listener, err := server.Listen(listenCtx, "tcp", ":"+strconv.Itoa(cfg.PeerPort))
		listenCancel()
		if err != nil {
			e.Down()
			return "", "", fmt.Errorf("listen on peer port %d: %w", cfg.PeerPort, err)
		}
		e.mu.Lock()
		e.inbound = listener
		e.mu.Unlock()
		e.wg.Add(1)
		go e.acceptInbound(listener, cfg.InboundTarget)
	}
	return material.ServerPublic().String(), material.ClientPublic().String(), nil
}

func writeAddressFile(path, address string) error {
	handle, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0600)
	if err != nil {
		return fmt.Errorf("create address file: %w", err)
	}
	if _, err := handle.WriteString(address + "\n"); err != nil {
		_ = handle.Close()
		return fmt.Errorf("write address file: %w", err)
	}
	if err := handle.Close(); err != nil {
		return fmt.Errorf("close address file: %w", err)
	}
	return os.Chmod(path, 0600)
}

func parseAllowList(list []string) (*tailcat.KeySet, error) {
	set := &tailcat.KeySet{}
	for _, entry := range list {
		// The wire form carries the "nodekey:" prefix; the parser wants
		// the bare untyped hex.
		pub, err := key.ParseNodePublicUntyped(mem.S(strings.TrimPrefix(entry, "nodekey:")))
		if err != nil {
			return nil, fmt.Errorf("%w: bad nodekey in allow list", ErrBadInput)
		}
		set.Add(pub)
	}
	return set, nil
}

// Allow replaces the admission set. When the new set is closed
// (allowAny=false), connected clients absent from it are disconnected
// via Server.DisconnectClient.
func (e *Engine) Allow(list []string, allowAny bool) (int, error) {
	set, err := parseAllowList(list)
	if err != nil {
		return 0, err
	}
	e.mu.Lock()
	if !e.up {
		e.mu.Unlock()
		return 0, ErrNotUp
	}
	e.allowKeys = set
	e.allowAny.Store(allowAny)
	server := e.server
	e.mu.Unlock()

	if allowAny {
		return len(list), nil
	}
	for _, peer := range server.Status().Peer {
		pub, err := key.ParseNodePublicUntyped(mem.S(strings.TrimPrefix(peer.PublicKey.String(), "nodekey:")))
		if err == nil && !set.Contains(pub) {
			if server.DisconnectClient(pub) {
				e.disconnected.Add(1)
			}
		}
	}
	return len(list), nil
}

// MapPeer listens on 127.0.0.1:0 and forwards each accepted connection
// to the peer's Tailcat address via Client.DialTCPPort. Repeating the
// call with the same peer and address is idempotent; a new address for a
// known peer replaces the mapping.
func (e *Engine) MapPeer(peer, address string, port int) (Mapping, error) {
	e.mu.Lock()
	if !e.up {
		e.mu.Unlock()
		return Mapping{}, ErrNotUp
	}
	e.mu.Unlock()
	if _, err := parseClientAddress(address); err != nil {
		// The parse error may embed the secret address; use a fixed message.
		return Mapping{}, fmt.Errorf("%w: address is not a valid tailcat address", ErrBadInput)
	}
	e.mu.Lock()
	if existing := e.mapped[peer]; existing != nil {
		if existing.address == address && existing.port == uint16(port) {
			mapping := existing.mapping
			e.mu.Unlock()
			return mapping, nil
		}
		e.unmapLocked(peer, existing)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		e.mu.Unlock()
		return Mapping{}, fmt.Errorf("allocate local forwarding port: %w", err)
	}
	local := listener.Addr().(*net.TCPAddr).Port
	lease := e.acquireClientLocked(address)
	ctx, cancel := context.WithCancel(e.ctx)
	mapped := &mappedPeer{
		mapping:  Mapping{Peer: peer, LocalPort: local},
		address:  address,
		port:     uint16(port),
		listener: listener,
		lease:    lease,
		cancel:   cancel,
		pairs:    make(map[*connPair]struct{}),
	}
	e.mapped[peer] = mapped
	e.wg.Add(1)
	e.mu.Unlock()
	go e.acceptOutbound(ctx, mapped, uint16(port))
	return mapped.mapping, nil
}

// UnmapPeer tears down a peer mapping.
func (e *Engine) UnmapPeer(peer string) error {
	e.mu.Lock()
	if !e.up {
		e.mu.Unlock()
		return ErrNotUp
	}
	mapped := e.mapped[peer]
	if mapped == nil {
		e.mu.Unlock()
		return ErrPeerUnknown
	}
	e.unmapLocked(peer, mapped)
	e.mu.Unlock()
	return nil
}

// unmapLocked removes the mapping and closes its listener and pairs; the
// caller must hold e.mu. Client teardown happens asynchronously.
func (e *Engine) unmapLocked(peer string, mapped *mappedPeer) {
	delete(e.mapped, peer)
	mapped.cancel()
	_ = mapped.listener.Close()
	for pair := range mapped.pairs {
		pair.close()
	}
	e.releaseClientLocked(mapped.lease)
}

// Expose listens on the Tailcat server port and proxies each connection
// to target. With proxyProtocol "v2" a PROXY v2 header precedes the
// stream: source is the peer's Tailcat IP and TLV 0xE0 carries the
// peer's nodekey text.
func (e *Engine) Expose(ctx context.Context, exposure Exposure) (Exposure, error) {
	e.mu.Lock()
	if !e.up {
		e.mu.Unlock()
		return Exposure{}, ErrNotUp
	}
	if existing := e.exposed[exposure.Port]; existing != nil {
		current := existing.exposure
		e.mu.Unlock()
		if current == exposure {
			return current, nil
		}
		return Exposure{}, ErrPortInUse
	}
	server := e.server
	e.mu.Unlock()

	listenCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	listener, err := server.Listen(listenCtx, "tcp", ":"+strconv.Itoa(exposure.Port))
	cancel()
	if err != nil {
		if strings.Contains(err.Error(), "already in use") {
			return Exposure{}, ErrPortInUse
		}
		return Exposure{}, fmt.Errorf("listen on tailcat port %d: %w", exposure.Port, err)
	}
	acceptCtx, acceptCancel := context.WithCancel(e.ctx)
	exposed := &exposedPort{
		exposure: exposure, listener: listener,
		cancel: acceptCancel, pairs: make(map[*connPair]struct{}),
	}
	e.mu.Lock()
	e.exposed[exposure.Port] = exposed
	e.mu.Unlock()
	e.wg.Add(1)
	go e.acceptExposure(acceptCtx, exposed)
	return exposure, nil
}

// Unexpose tears down an exposed port.
func (e *Engine) Unexpose(port int) error {
	e.mu.Lock()
	if !e.up {
		e.mu.Unlock()
		return ErrNotUp
	}
	exposed := e.exposed[port]
	if exposed == nil {
		e.mu.Unlock()
		return ErrPortInUse
	}
	delete(e.exposed, port)
	exposed.cancel()
	_ = exposed.listener.Close()
	for pair := range exposed.pairs {
		pair.close()
	}
	e.mu.Unlock()
	return nil
}

// PeerKey resolves the node public key of the peer behind addr, an
// ip:port as seen by an exposed target.
func (e *Engine) PeerKey(addr string) (found bool, public string, err error) {
	e.mu.Lock()
	if !e.up {
		e.mu.Unlock()
		return false, "", ErrNotUp
	}
	server := e.server
	e.mu.Unlock()
	ap, err := netip.ParseAddrPort(addr)
	if err != nil {
		return false, "", fmt.Errorf("%w: addr must be an IP:port", ErrBadInput)
	}
	pub, ok := server.PeerKey(&net.TCPAddr{IP: ap.Addr().AsSlice(), Port: int(ap.Port())})
	if !ok {
		return false, "", nil
	}
	return true, pub.String(), nil
}

// Status reports the engine state for the status command.
func (e *Engine) Status() (state string, serverPublic, clientPublic string, peers []Mapping) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if !e.up {
		return "Down", "", "", []Mapping{}
	}
	return "Running",
		e.material.ServerPublic().String(),
		e.material.ClientPublic().String(),
		e.mappingsLocked()
}

func (e *Engine) mappingsLocked() []Mapping {
	result := make([]Mapping, 0, len(e.mapped))
	for _, mapped := range e.mapped {
		result = append(result, mapped.mapping)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].Peer < result[j].Peer })
	return result
}

// DeniedCount reports how many client announcements the admission hook
// has rejected. Test evidence hook: a denied counter increment proves the
// hook fired rather than a generic network timeout.
func (e *Engine) DeniedCount() int64 {
	return e.deniedCount.Load()
}

// DisconnectedCount reports how many connected clients Allow has dropped
// via Server.DisconnectClient. Test evidence hook: DisconnectClient
// taking effect is observable even though Server.Status lags the client
// set.
func (e *Engine) DisconnectedCount() int64 {
	return e.disconnected.Load()
}

// connectedClientKeys reports the nodekeys of currently connected
// clients. Used by tests to observe DisconnectClient effects.
func (e *Engine) connectedClientKeys() []string {
	e.mu.Lock()
	defer e.mu.Unlock()
	if !e.up {
		return nil
	}
	var result []string
	for _, peer := range e.server.Status().Peer {
		result = append(result, peer.PublicKey.String())
	}
	sort.Strings(result)
	return result
}

// Down closes every listener, client, and the server. It is idempotent.
func (e *Engine) Down() {
	e.mu.Lock()
	if !e.up {
		e.mu.Unlock()
		return
	}
	e.up = false
	e.cancel()
	if e.inbound != nil {
		_ = e.inbound.Close()
	}
	for pair := range e.inboundPairs {
		pair.close()
	}
	for peer, mapped := range e.mapped {
		e.unmapLocked(peer, mapped)
	}
	for name, service := range e.services {
		e.unmapServiceLocked(name, service)
	}
	for port, exposed := range e.exposed {
		delete(e.exposed, port)
		exposed.cancel()
		_ = exposed.listener.Close()
		for pair := range exposed.pairs {
			pair.close()
		}
	}
	server := e.server
	e.server = nil
	e.mu.Unlock()
	_ = server.Close()
	e.wg.Wait()
}

func (e *Engine) acceptInbound(listener net.Listener, inboundTarget string) {
	defer e.wg.Done()
	address := strings.TrimPrefix(inboundTarget, "tcp:")
	for {
		remote, err := listener.Accept()
		if err != nil {
			return
		}
		dialer := net.Dialer{Timeout: dialTimeout}
		local, err := dialer.DialContext(e.ctx, "tcp", address)
		if err != nil {
			_ = remote.Close()
			continue
		}
		pair := &connPair{left: remote, right: local}
		e.mu.Lock()
		if !e.up {
			e.mu.Unlock()
			pair.close()
			return
		}
		e.inboundPairs[pair] = struct{}{}
		e.mu.Unlock()
		e.splice(pair, func() {
			e.mu.Lock()
			delete(e.inboundPairs, pair)
			e.mu.Unlock()
		})
	}
}

func (e *Engine) acceptOutbound(ctx context.Context, mapped *mappedPeer, port uint16) {
	defer e.wg.Done()
	for {
		local, err := mapped.listener.Accept()
		if err != nil {
			return
		}
		dialCtx, cancel := context.WithTimeout(ctx, dialTimeout)
		remote, err := e.dialTCPPort(dialCtx, mapped.lease, port)
		cancel()
		if err != nil {
			_ = local.Close()
			continue
		}
		pair := &connPair{left: local, right: remote}
		e.mu.Lock()
		if !e.up || e.mapped[mapped.mapping.Peer] != mapped {
			e.mu.Unlock()
			pair.close()
			return
		}
		mapped.pairs[pair] = struct{}{}
		e.mu.Unlock()
		e.splice(pair, func() {
			e.mu.Lock()
			delete(mapped.pairs, pair)
			e.mu.Unlock()
		})
	}
}

func (e *Engine) acceptExposure(ctx context.Context, exposed *exposedPort) {
	defer e.wg.Done()
	for {
		remote, err := exposed.listener.Accept()
		if err != nil {
			return
		}
		e.mu.Lock()
		if !e.up || e.exposed[exposed.exposure.Port] != exposed {
			e.mu.Unlock()
			_ = remote.Close()
			return
		}
		e.wg.Add(1)
		e.mu.Unlock()
		go func(conn net.Conn) {
			defer e.wg.Done()
			e.handleExposure(ctx, exposed, conn)
		}(remote)
	}
}

func (e *Engine) handleExposure(ctx context.Context, exposed *exposedPort, remote net.Conn) {
	exposure := exposed.exposure
	var peerKeyText string
	if exposure.ProxyProtocol == "v2" {
		// Fail closed: an identity-carrying exposure never forwards a
		// connection whose peer key could not be established.
		e.mu.Lock()
		server := e.server
		e.mu.Unlock()
		if server == nil {
			_ = remote.Close()
			return
		}
		pub, ok := server.PeerKey(remote.RemoteAddr())
		if !ok {
			_ = remote.Close()
			return
		}
		peerKeyText = pub.String()
	}
	network, address := splitTarget(exposure.Target)
	dialer := net.Dialer{Timeout: dialTimeout}
	local, err := dialer.DialContext(ctx, network, address)
	if err != nil {
		_ = remote.Close()
		return
	}
	if exposure.ProxyProtocol == "v2" {
		header, err := buildProxyV2Header(remote.RemoteAddr(), remote.LocalAddr(), exposure.Port, peerKeyText)
		if err != nil {
			_ = remote.Close()
			_ = local.Close()
			return
		}
		if _, err := local.Write(header); err != nil {
			_ = remote.Close()
			_ = local.Close()
			return
		}
	}
	pair := &connPair{left: remote, right: local}
	e.mu.Lock()
	if !e.up || e.exposed[exposure.Port] != exposed {
		e.mu.Unlock()
		pair.close()
		return
	}
	exposed.pairs[pair] = struct{}{}
	e.mu.Unlock()
	e.splice(pair, func() {
		e.mu.Lock()
		delete(exposed.pairs, pair)
		e.mu.Unlock()
	})
}

func splitTarget(target string) (network, address string) {
	if strings.HasPrefix(target, "unix:") {
		return "unix", strings.TrimPrefix(target, "unix:")
	}
	return "tcp", strings.TrimPrefix(target, "tcp:")
}

var proxyV2Signature = [12]byte{0x0d, 0x0a, 0x0d, 0x0a, 0x00, 0x0d, 0x0a, 0x51, 0x55, 0x49, 0x54, 0x0a}

// buildProxyV2Header builds a PROXY v2 header for an accepted Tailcat
// connection. Tailcat addresses are IPv6, so the header always uses the
// TCP6 family; TLV 0xE0 carries the peer nodekey text.
func buildProxyV2Header(source, destination net.Addr, port int, peerKeyText string) ([]byte, error) {
	sourceAP, err := addrToAddrPort(source)
	if err != nil {
		return nil, err
	}
	destinationAP, err := addrToAddrPort(destination)
	if err != nil {
		return nil, err
	}
	destinationAP = netip.AddrPortFrom(destinationAP.Addr(), uint16(port))
	tlv := []byte(peerKeyText)
	payloadLength := 36 + 3 + len(tlv)
	if payloadLength > 65535 {
		return nil, errors.New("PROXY v2 TLV is too large")
	}
	header := make([]byte, 16, 16+payloadLength)
	copy(header[:12], proxyV2Signature[:])
	header[12] = 0x21 // version 2, PROXY command
	header[13] = 0x21 // TCP over IPv6
	binary.BigEndian.PutUint16(header[14:16], uint16(payloadLength))
	as16 := sourceAP.Addr().As16()
	header = append(header, as16[:]...)
	ad16 := destinationAP.Addr().As16()
	header = append(header, ad16[:]...)
	ports := make([]byte, 4)
	binary.BigEndian.PutUint16(ports[:2], sourceAP.Port())
	binary.BigEndian.PutUint16(ports[2:], destinationAP.Port())
	header = append(header, ports...)
	header = append(header, 0xe0, byte(len(tlv)>>8), byte(len(tlv)))
	header = append(header, tlv...)
	return header, nil
}

func addrToAddrPort(addr net.Addr) (netip.AddrPort, error) {
	switch a := addr.(type) {
	case *net.TCPAddr:
		return a.AddrPort(), nil
	case *net.UDPAddr:
		return a.AddrPort(), nil
	default:
		return netip.AddrPort{}, fmt.Errorf("unexpected address type %T", addr)
	}
}

func (e *Engine) splice(pair *connPair, cleanup func()) {
	e.wg.Add(1)
	go func() {
		defer e.wg.Done()
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
