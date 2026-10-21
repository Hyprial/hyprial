package engine

// Fixed outbound slot pool (design branch
// design/socialware-data-layer-2026-10-06:
// docs/design/design-stable-zenoh-session-slots-2026-10-08.md §2, §4).
// Slots are unix socket listeners opened at Up and never closed
// while the engine runs: the local zenoh session's connect set points at
// them forever, so membership changes only rebind slots and the zenoh
// session is never rebuilt.

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode"
)

// defaultSlotHold matches zenoh's default open timeout (10 s): a held
// connection older than that has already been abandoned by the dialer,
// so the sidecar drops it and lets zenoh redial.
const defaultSlotHold = 10 * time.Second

// tcpSlotGrowSpec asks for one new loopback TCP slot with a
// kernel-assigned port: the Windows transport, where zenoh has no
// unixsock-stream dialer (notes/windows-unixsock-2026-10-08.md §1).
const tcpSlotGrowSpec = "tcp:127.0.0.1:0"

// unixPathLimit is the sockaddr_un.sun_path capacity including the final NUL.
func unixPathLimit() int {
	if runtime.GOOS == "darwin" {
		return 104
	}
	return 108
}

// slotName is the fixed naming rule shared with the daemon: a map-peer
// naming an unknown unix path binds lazily IF it lives in the directory
// of an up slot and follows this rule (pool growth, design §5).
var slotName = regexp.MustCompile(`^slot-\d+$`)

// slotPort is one fixed outbound slot. Unbound ("hold mode") it accepts
// and keeps connections unread, so the dialer's first handshake bytes
// stay queued in the kernel buffer; bound, it splices every held and
// future connection onto the mapped peer's Tailcat stream, which lets
// the pending handshake complete at once.
type slotPort struct {
	id       string // wire id: the socket path, or tcp:127.0.0.1:<port>
	listener net.Listener
	bound    *mappedPeer              // nil in hold mode
	held     map[net.Conn]*time.Timer // hold-mode connections and their drop timers
}

// listenSlots opens every slot listener and starts its accept loop.
// Called from Up after the engine is marked up.
func (e *Engine) listenSlots(specs []string, hold time.Duration) error {
	for _, spec := range specs {
		if _, err := e.openSlot(spec, hold); err != nil {
			return err
		}
	}
	return nil
}

// openSlot resolves one slot spec into a listening slot and registers
// it: a unix path listens on that path (its own wire id); a tcp spec
// listens on the loopback address, where port 0 resolves to a
// kernel-assigned port reported in the wire id.
func (e *Engine) openSlot(spec string, hold time.Duration) (*slotPort, error) {
	var listener net.Listener
	var id string
	var err error
	if strings.HasPrefix(spec, "tcp:") {
		address := spec[len("tcp:"):]
		listener, err = net.Listen("tcp4", address)
		if err != nil && address != "127.0.0.1:0" {
			fallback, fallbackErr := net.Listen("tcp4", "127.0.0.1:0")
			if fallbackErr == nil {
				fmt.Fprintf(
					os.Stderr,
					"hyprial-tailcat: tcp slot %s unavailable (%v); using %s\n",
					spec, err, fallback.Addr(),
				)
				listener, err = fallback, nil
			}
		}
		if err != nil {
			return nil, fmt.Errorf("listen on slot %s: %w", spec, err)
		}
		id = "tcp:" + listener.Addr().String()
	} else {
		if len(spec) >= unixPathLimit() {
			return nil, fmt.Errorf(
				"%w: unix slot path is %d bytes; max %d: %s",
				ErrBadInput, len(spec), unixPathLimit()-1, spec,
			)
		}
		// A fresh home has no <home>/state/tailcat/slots directory:
		// the daemon creates it before its up request, and the sidecar
		// owns its own bind path the same way (a lazy growth bind must
		// not depend on the daemon having created anything). 0700 is the
		// state-dir convention under the hyprial home.
		if err := os.MkdirAll(filepath.Dir(spec), 0o700); err != nil {
			return nil, fmt.Errorf("create slot directory for %s: %w", spec, err)
		}
		// A stale socket file from a crashed sidecar would fail the bind.
		if _, statErr := os.Stat(spec); statErr == nil {
			_ = os.Remove(spec)
		}
		listener, err = net.Listen("unix", spec)
		if err != nil {
			return nil, fmt.Errorf("listen on slot %s: %w", spec, err)
		}
		id = spec
	}
	slot := &slotPort{
		id: id, listener: listener,
		held: make(map[net.Conn]*time.Timer),
	}
	e.mu.Lock()
	if !e.up {
		// Down raced a lazy growth bind; the closed engine must not
		// leak a listener it would never close.
		e.mu.Unlock()
		_ = listener.Close()
		return nil, ErrNotUp
	}
	e.slots[id] = slot
	e.slotOrder = append(e.slotOrder, id)
	e.mu.Unlock()
	e.wg.Add(1)
	go e.acceptSlot(slot, hold)
	return slot, nil
}

// SlotIDs reports the resolved slot wire ids, up order first, growth
// appended. State events carry them so the daemon can dial
// kernel-assigned tcp slots.
func (e *Engine) SlotIDs() []string {
	e.mu.Lock()
	defer e.mu.Unlock()
	if len(e.slotOrder) == 0 {
		return nil
	}
	ids := make([]string, len(e.slotOrder))
	copy(ids, e.slotOrder)
	return ids
}

// growSlotLocked decides whether an unknown map-peer slot is a lazy
// growth bind: the tcp grow spec always is; a unix path is when it
// shares a directory with an up unix slot and follows the naming rule.
// Returns false when the slot must be refused. The caller holds e.mu
// and must retry the lookup after openSlot (which relocks).
func (e *Engine) growSlotLocked(spec string) bool {
	if spec == tcpSlotGrowSpec {
		return true
	}
	if strings.HasPrefix(spec, "tcp:") {
		return false
	}
	if !slotName.MatchString(filepath.Base(spec)) {
		return false
	}
	dir := filepath.Dir(spec)
	for id := range e.slots {
		if !strings.HasPrefix(id, "tcp:") && filepath.Dir(id) == dir {
			return true
		}
	}
	return false
}

func (e *Engine) acceptSlot(slot *slotPort, hold time.Duration) {
	defer e.wg.Done()
	for {
		conn, err := slot.listener.Accept()
		if err != nil {
			return
		}
		e.mu.Lock()
		if !e.up {
			e.mu.Unlock()
			_ = conn.Close()
			return
		}
		if mapped := slot.bound; mapped != nil {
			e.wg.Add(1)
			e.mu.Unlock()
			go e.forwardSlotConn(mapped, conn)
			continue
		}
		slot.held[conn] = time.AfterFunc(hold, func() { e.dropHeld(slot, conn) })
		e.mu.Unlock()
	}
}

// dropHeld closes one hold-mode connection whose timer expired. A
// connection that bindSlotLocked already took over is left alone.
func (e *Engine) dropHeld(slot *slotPort, conn net.Conn) {
	e.mu.Lock()
	_, holding := slot.held[conn]
	if holding {
		delete(slot.held, conn)
	}
	e.mu.Unlock()
	if holding {
		_ = conn.Close()
	}
}

// forwardSlotConn dials the mapped peer and splices one slot connection
// (held or fresh) onto the resulting Tailcat stream. The caller has
// already incremented e.wg.
func (e *Engine) forwardSlotConn(mapped *mappedPeer, local net.Conn) {
	defer e.wg.Done()
	dialCtx, cancel := context.WithTimeout(mapped.ctx, dialTimeout)
	remote, err := e.dialTCPPort(dialCtx, mapped.lease, mapped.port)
	cancel()
	if err != nil {
		_ = local.Close()
		return
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
	e.splice(pair, func(result spliceResult) {
		e.mu.Lock()
		mapped.history.record(result)
		delete(mapped.pairs, pair)
		e.mu.Unlock()
	})
}

// bindSlotLocked points slot at mapped and takes over its held
// connections. The caller holds e.mu and must forward the returned
// connections after releasing the lock.
func (e *Engine) bindSlotLocked(slot *slotPort, mapped *mappedPeer) []net.Conn {
	slot.bound = mapped
	taken := make([]net.Conn, 0, len(slot.held))
	for conn, timer := range slot.held {
		timer.Stop()
		taken = append(taken, conn)
	}
	slot.held = make(map[net.Conn]*time.Timer)
	return taken
}

// mapPeerSlot binds a slot to the peer's Tailcat address. Repeating
// the call with the same peer, address, and slot is idempotent; pointing
// a bound slot at a new peer, or a known peer at a new slot or address,
// closes the old connections first (rebind). An unknown slot that
// matches the growth contract (the tcp grow spec, or a unix path in an
// up slot's directory following the naming rule) is bound lazily --
// that is what lets the daemon grow the pool without a sidecar restart.
func (e *Engine) mapPeerSlot(peer, address string, port int, slotSpec string) (Mapping, error) {
	e.mu.Lock()
	slot := e.slots[slotSpec]
	if slot == nil {
		grow := e.growSlotLocked(slotSpec)
		hold := e.slotHold
		e.mu.Unlock()
		if !grow {
			return Mapping{}, fmt.Errorf("%w: slot is not one of the up slots", ErrBadInput)
		}
		opened, err := e.openSlot(slotSpec, hold)
		if err != nil {
			return Mapping{}, fmt.Errorf("%w: %s", ErrBadInput, err)
		}
		e.mu.Lock()
		slot = opened
	}
	if existing := e.mapped[peer]; existing != nil {
		if existing.address == address && existing.port == uint16(port) && existing.slot == slot {
			mapping := existing.mapping
			e.mu.Unlock()
			return mapping, nil
		}
		e.unmapLocked(peer, existing)
	}
	if other := slot.bound; other != nil {
		e.unmapLocked(other.mapping.Peer, other)
	}
	lease := e.acquireClientLocked(address)
	ctx, cancel := context.WithCancel(e.ctx)
	mapped := &mappedPeer{
		mapping: Mapping{Peer: peer, Slot: slot.id},
		address: address,
		port:    uint16(port),
		slot:    slot,
		lease:   lease,
		ctx:     ctx,
		cancel:  cancel,
		pairs:   make(map[*connPair]struct{}),
		history: newConnectionHistory(peer, slot.id, telemetryMapPort(slot.id)),
	}
	e.mapped[peer] = mapped
	held := e.bindSlotLocked(slot, mapped)
	e.wg.Add(len(held))
	e.mu.Unlock()
	for _, conn := range held {
		go e.forwardSlotConn(mapped, conn)
	}
	return mapped.mapping, nil
}

// closeSlotsLocked tears down every slot listener and held connection;
// the caller holds e.mu (Down).
func (e *Engine) closeSlotsLocked() {
	for id, slot := range e.slots {
		delete(e.slots, id)
		_ = slot.listener.Close()
		for conn, timer := range slot.held {
			timer.Stop()
			_ = conn.Close()
		}
		slot.held = make(map[net.Conn]*time.Timer)
	}
	e.slotOrder = nil
}

// heldCount reports how many hold-mode connections a slot currently
// keeps. Test evidence hook: held > 0 proves hold mode accepted without
// reading, and held == 0 after the timeout proves the drop fired.
func (e *Engine) heldCount(slotPath string) int {
	e.mu.Lock()
	defer e.mu.Unlock()
	if slot := e.slots[slotPath]; slot != nil {
		return len(slot.held)
	}
	return 0
}

const connectionHistoryCapacity = 16

type copyOutcome struct {
	bytes int64
	err   error
}

type spliceResult struct {
	bytesIn    int64
	bytesOut   int64
	durationMs int64
	closeSide  string
	errorIn    string
	errorOut   string
}

// ConnectionClose is one completed map-peer splice. In is peer-to-local;
// out is local-to-peer.
type ConnectionClose struct {
	Peer       string
	Slot       string
	MapPort    *int
	BytesIn    int64
	BytesOut   int64
	DurationMs int64
	CloseSide  string
	ErrorIn    string
	ErrorOut   string
}

// SlotConnectionTelemetry is the bounded status projection for one map-peer listener.
type SlotConnectionTelemetry struct {
	Peer        string
	Slot        string
	MapPort     *int
	ClosedCount int64
	ErrorCounts map[string]int64
	Recent      []ConnectionClose
}

// ConnectionTelemetry is the complete bounded map-peer splice projection.
type ConnectionTelemetry struct {
	RingCapacity int
	Slots        []SlotConnectionTelemetry
}

type connectionHistory struct {
	peer        string
	slot        string
	mapPort     *int
	closedCount int64
	errorCounts map[string]int64
	recent      []ConnectionClose
}

func newConnectionHistory(peer, slot string, mapPort *int) *connectionHistory {
	return &connectionHistory{
		peer: peer, slot: slot, mapPort: mapPort,
		errorCounts: make(map[string]int64),
		recent:      make([]ConnectionClose, 0, connectionHistoryCapacity),
	}
}

func telemetryMapPort(slot string) *int {
	if !strings.HasPrefix(slot, "tcp:") {
		return nil
	}
	_, raw, err := net.SplitHostPort(strings.TrimPrefix(slot, "tcp:"))
	if err != nil {
		return nil
	}
	port, err := strconv.Atoi(raw)
	if err != nil {
		return nil
	}
	return &port
}

func (h *connectionHistory) record(result spliceResult) {
	h.closedCount++
	h.errorCounts[result.errorIn]++
	h.errorCounts[result.errorOut]++
	record := ConnectionClose{
		Peer: h.peer, Slot: h.slot, MapPort: h.mapPort,
		BytesIn: result.bytesIn, BytesOut: result.bytesOut,
		DurationMs: result.durationMs, CloseSide: result.closeSide,
		ErrorIn: result.errorIn, ErrorOut: result.errorOut,
	}
	if len(h.recent) == connectionHistoryCapacity {
		copy(h.recent, h.recent[1:])
		h.recent[len(h.recent)-1] = record
		return
	}
	h.recent = append(h.recent, record)
}

func (h *connectionHistory) snapshot() SlotConnectionTelemetry {
	counts := make(map[string]int64, len(h.errorCounts))
	for class, count := range h.errorCounts {
		counts[class] = count
	}
	return SlotConnectionTelemetry{
		Peer: h.peer, Slot: h.slot, MapPort: h.mapPort, ClosedCount: h.closedCount,
		ErrorCounts: counts, Recent: append([]ConnectionClose(nil), h.recent...),
	}
}

type copyFunc func(io.Writer, io.Reader) (int64, error)

type directedCopy struct {
	direction string
	outcome   copyOutcome
}

func collectSplice(pair *connPair, copier copyFunc) spliceResult {
	started := time.Now()
	done := make(chan directedCopy, 2)
	go func() {
		count, err := copier(pair.left, pair.right)
		done <- directedCopy{direction: "in", outcome: copyOutcome{bytes: count, err: err}}
	}()
	go func() {
		count, err := copier(pair.right, pair.left)
		done <- directedCopy{direction: "out", outcome: copyOutcome{bytes: count, err: err}}
	}()
	first := <-done
	pair.close()
	second := <-done
	outcomes := map[string]copyOutcome{first.direction: first.outcome, second.direction: second.outcome}
	classIn := normalizeCopyError(outcomes["in"].err)
	classOut := normalizeCopyError(outcomes["out"].err)
	return spliceResult{
		bytesIn: outcomes["in"].bytes, bytesOut: outcomes["out"].bytes,
		durationMs: max(0, time.Since(started).Milliseconds()),
		closeSide:  closeSide(classIn, classOut),
		errorIn:    classIn, errorOut: classOut,
	}
}

func closeSide(classIn, classOut string) string {
	peerClosed := classIn != "canceled"
	localClosed := classOut != "canceled"
	switch {
	case peerClosed && localClosed:
		return "both"
	case peerClosed:
		return "peer"
	case localClosed:
		return "local"
	default:
		return "both"
	}
}

func normalizeCopyError(err error) string {
	switch {
	case err == nil || errors.Is(err, io.EOF):
		return "eof"
	case errors.Is(err, context.Canceled), errors.Is(err, net.ErrClosed):
		return "canceled"
	case errors.Is(err, os.ErrDeadlineExceeded):
		return "timeout"
	case errors.Is(err, syscall.ECONNRESET), errors.Is(err, syscall.EPIPE):
		return "reset"
	}
	var netError net.Error
	if errors.As(err, &netError) && netError.Timeout() {
		return "timeout"
	}
	name := strings.TrimPrefix(reflect.TypeOf(err).String(), "*")
	name = strings.Map(func(r rune) rune {
		if unicode.IsLetter(r) || unicode.IsDigit(r) || strings.ContainsRune("._-", r) {
			return unicode.ToLower(r)
		}
		return -1
	}, name)
	if name == "" {
		name = "error"
	}
	if len(name) > 40 {
		name = name[:40]
	}
	return fmt.Sprintf("other:%s", name)
}

func (e *Engine) splice(pair *connPair, cleanup func(spliceResult)) {
	e.wg.Add(1)
	go func() {
		defer e.wg.Done()
		result := collectSplice(pair, io.Copy)
		cleanup(result)
	}()
}
