// Package mobilebridge is the handwritten C ABI over mobileclient: the small,
// frozen surface the native (Rust) side calls to reach one approved service
// without ever holding a Go pointer, a raw connection or a network address.
//
// ABI freeze (REMOTE-CHAIN-PLAN-20261016 section 2, all six rows):
//
//  1. Bounded request/response buffers: the request line and the response
//     line are both capped at MaxFrameBytes (the same bound the node facade
//     enforces); an oversize request is refused, an oversize response is
//     refused with its required length -- never truncated.
//  2. cancel/close: hyprial_bridge_cancel aborts every exchange in flight on
//     the handle (a dial in progress included); hyprial_bridge_close is
//     terminal -- it maps mobileclient.Client.Close exactly once, and every
//     later call on that handle answers InvalidHandle.
//  3. Buffer ownership: the caller's buffers stay the caller's.  The request
//     is copied into Go memory before dialing; the response is copied into
//     the caller's buffer before returning.  Nothing is retained.
//  4. No retained pointers: only opaque int64 handles and plain bytes cross
//     the boundary; transports, connections and error strings stay inside.
//  5. Generation fence: handles are unique per open and never reused; after
//     close, every old handle answers InvalidHandle forever, so a late call
//     can never be served by a newer client generation.
//  6. Error mapping: transport errors keep fixed, redacted codes (a Tailcat
//     address never crosses); the Rust side maps the codes onto the frame
//     protocol's definitive/non-definitive classification.
//
// The dial target is fixed at open.  TransportTailnet is the production arm:
// the legacy hyprial_bridge_open entry has no identity argument, so that arm
// stays unavailable (ErrUnavailable); hyprial_bridge_open_tailnet carries one
// app-owned userspace node key and the approved server address, both supplied
// by the native composition root after pairing.  No identity is generated or
// guessed here, and an invalid key/address is a stable ErrConfig.
// TransportLoopback is the explicit host-test/local-development arm: it dials
// 127.0.0.1 on the given port
// through the same bounded mobileclient generation.
// TransportDirectTest is the user-configured direct-network test arm: the
// native side names one host (for the Android emulator this is the host
// loopback alias 10.0.2.2, never an adb reverse forward) and the same port,
// and the app UI labels the surface unauthenticated/not production.  The
// host-aware entry point is additive; hyprial_bridge_open keeps its frozen
// signature for every existing caller.
//
// This is a ``package main`` on purpose: it exists solely as the c-archive
// root the native side links (``go build -buildmode=c-archive``); nothing
// imports it as a Go library, and its unit tests run in the same package.
package main

/*
#include <stdint.h>
#include <string.h>
*/
import "C"

import (
	"bufio"
	"context"
	"errors"
	"net"
	"strconv"
	"strings"
	"sync"
	"time"
	"unsafe"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/mobileclient"
	"github.com/tailscale/tailcat"
	"tailscale.com/types/key"
)

// MaxFrameBytes bounds one request line and one response line, newline
// excluded; it mirrors the node facade's request-line bound.
const MaxFrameBytes = 1_048_576

// Fixed ABI codes: 0 is success, every failure is a stable negative code.
const (
	ErrOK                  = 0
	ErrInvalidHandle       = -1
	ErrConfig              = -2
	ErrClosed              = -3
	ErrBusy                = -4
	ErrUnavailable         = -5
	ErrCancelled           = -6
	ErrDeadline            = -7
	ErrRequestTooLarge     = -8
	ErrResponseTooLarge    = -9
	ErrResponseCapTooSmall = -10
)

// Transport kinds for hyprial_bridge_open / hyprial_bridge_open_host.
const (
	TransportTailnet    = 0
	TransportLoopback   = 1
	TransportDirectTest = 2
	MaxHostBytes        = 253
	DirectTestSurface   = "unauthenticated-test-surface-not-production"
	// A Tailcat node private key is a short textual key; the server address
	// names the admitted peer plus its relay set.  Both stay native-side and
	// are bounded before they reach the userspace client.
	MaxTailnetKeyBytes     = 512
	MaxTailnetAddressBytes = 4096
)

// loopbackTransport is the development arm: exclusive, boring TCP on
// 127.0.0.1, honouring the dial context like every mobileclient transport.
type loopbackTransport struct{}

func (loopbackTransport) DialTCPPort(ctx context.Context, port uint16) (net.Conn, error) {
	var dialer net.Dialer
	return dialer.DialContext(ctx, "tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(int(port))))
}

func (loopbackTransport) Close() error { return nil }

// directTestTransport is the explicit direct-network test arm.  The host is
// fixed at open; it is a plain IP/DNS name such as 10.0.2.2 (the Android
// emulator's host loopback alias), never an adb reverse target and never a
// Tailcat secret address.  It is deliberately not the production arm: this
// path carries no identity/admission and must be labelled as such by the UI.
type directTestTransport struct{ host string }

func (d directTestTransport) DialTCPPort(ctx context.Context, port uint16) (net.Conn, error) {
	var dialer net.Dialer
	return dialer.DialContext(ctx, "tcp", net.JoinHostPort(d.host, strconv.Itoa(int(port))))
}

func (directTestTransport) Close() error { return nil }

// validDirectHost bounds and validates the user-supplied host.  It accepts
// DNS names and IPv4/IPv6 literals (IPv6 zones included); it refuses empty,
// URL-shaped, whitespace-containing and control-character input so a bad
// configuration is a stable ErrConfig rather than an opaque dial error.
func validDirectHost(host string) bool {
	if host == "" || len(host) > MaxHostBytes || strings.TrimSpace(host) != host {
		return false
	}
	for _, r := range host {
		switch {
		case r >= 'a' && r <= 'z':
		case r >= 'A' && r <= 'Z':
		case r >= '0' && r <= '9':
		case r == '.', r == '-', r == '_', r == ':', r == '%':
		default:
			return false
		}
	}
	return true
}

type inflight struct {
	cancel context.CancelFunc
	conn   net.Conn
}

type handle struct {
	id      int64
	client  *mobileclient.Client
	timeout time.Duration

	mu      sync.Mutex
	closed  bool
	seq     uint64
	cancels map[uint64]*inflight
}

var (
	handlesMu  sync.Mutex
	handles    = map[int64]*handle{}
	nextHandle int64
)

func openHandle(servicePort uint16, timeoutMs, limit, kind int32) (int64, int32) {
	return openHandleAt("", servicePort, timeoutMs, limit, kind)
}

// openHandleAt is the host-aware opener.  The legacy hyprial_bridge_open
// entry point delegates with an empty host, preserving its frozen shape;
// TransportDirectTest requires a validated host and fails closed otherwise.
func openHandleAt(host string, servicePort uint16, timeoutMs, limit, kind int32) (int64, int32) {
	if servicePort == 0 || timeoutMs <= 0 || timeoutMs > 30_000 || limit < 1 || limit > 32 {
		return 0, ErrConfig
	}
	var transport mobileclient.Transport
	switch kind {
	case TransportTailnet:
		// The production arm needs an app-owned userspace identity; the
		// dedicated entry point below carries it.  The legacy open call has
		// no identity argument and must never guess one, so the arm stays
		// unavailable (the frozen contract) rather than becoming a caller
		// configuration error.
		return 0, ErrUnavailable
	case TransportLoopback:
		if host != "" {
			return 0, ErrConfig
		}
		transport = loopbackTransport{}
	case TransportDirectTest:
		if !validDirectHost(host) {
			return 0, ErrConfig
		}
		transport = directTestTransport{host: host}
	default:
		return 0, ErrConfig
	}
	return registerTransport(transport, servicePort, timeoutMs, limit)
}

func registerTransport(transport mobileclient.Transport, servicePort uint16, timeoutMs, limit int32) (int64, int32) {
	client, err := mobileclient.New(
		transport, servicePort, time.Duration(timeoutMs)*time.Millisecond, int(limit),
	)
	if err != nil {
		_ = transport.Close()
		return 0, ErrConfig
	}
	handlesMu.Lock()
	nextHandle++
	id := nextHandle
	handles[id] = &handle{id: id, client: client, timeout: time.Duration(timeoutMs) * time.Millisecond,
		cancels: map[uint64]*inflight{}}
	handlesMu.Unlock()
	return id, ErrOK
}

// openTailnetAt constructs the actual pinned userspace Tailcat client for one
// app-owned node key and admitted server address.  The key and address are
// supplied by the native composition root after device pairing/approval; this
// function creates no identity and falls back to no other transport.  A
// malformed key/address or invalid bounds is a stable ErrConfig.  The client
// is not dialled here: the first exchange performs Tailcat's admission
// handshake, and a refusal stays ErrUnavailable.
func openTailnetAt(clientPrivate, serverAddress string, servicePort uint16, timeoutMs, limit int32) (int64, int32) {
	if servicePort == 0 || timeoutMs <= 0 || timeoutMs > 30_000 || limit < 1 || limit > 32 {
		return 0, ErrConfig
	}
	if clientPrivate == "" || len(clientPrivate) > MaxTailnetKeyBytes ||
		serverAddress == "" || len(serverAddress) > MaxTailnetAddressBytes ||
		strings.TrimSpace(clientPrivate) != clientPrivate ||
		strings.TrimSpace(serverAddress) != serverAddress {
		return 0, ErrConfig
	}
	var private key.NodePrivate
	if err := private.UnmarshalText([]byte(clientPrivate)); err != nil {
		return 0, ErrConfig
	}
	if _, err := tailcat.ParseAddr(tailcat.Addr(serverAddress)); err != nil {
		return 0, ErrConfig
	}
	transport := &tailcat.Client{
		Key: private, Server: tailcat.Addr(serverAddress),
		Logf: func(string, ...any) {},
	}
	return registerTransport(transport, servicePort, timeoutMs, limit)
}

func lookup(id int64) (*handle, int32) {
	handlesMu.Lock()
	defer handlesMu.Unlock()
	found := handles[id]
	if found == nil {
		return nil, ErrInvalidHandle
	}
	return found, ErrOK
}

func closeHandle(id int64) int32 {
	found, code := lookup(id)
	if code != ErrOK {
		return code
	}
	found.mu.Lock()
	if found.closed {
		found.mu.Unlock()
		return ErrOK
	}
	found.closed = true
	pending := found.cancels
	found.cancels = map[uint64]*inflight{}
	found.mu.Unlock()
	for _, item := range pending {
		item.cancel()
		if item.conn != nil {
			_ = item.conn.SetDeadline(time.Now())
		}
	}
	handlesMu.Lock()
	delete(handles, id)
	handlesMu.Unlock()
	_ = found.client.Close()
	return ErrOK
}

func cancelHandle(id int64) int32 {
	found, code := lookup(id)
	if code != ErrOK {
		return code
	}
	found.mu.Lock()
	if found.closed {
		found.mu.Unlock()
		return ErrInvalidHandle
	}
	pending := found.cancels
	found.cancels = map[uint64]*inflight{}
	found.mu.Unlock()
	for _, item := range pending {
		item.cancel()
		if item.conn != nil {
			_ = item.conn.SetDeadline(time.Now())
		}
	}
	return ErrOK
}

func mapDialError(err error) int32 {
	switch {
	case err == nil:
		return ErrOK
	case errors.Is(err, mobileclient.ErrClosed):
		return ErrClosed
	case errors.Is(err, mobileclient.ErrBusy):
		return ErrBusy
	case errors.Is(err, mobileclient.ErrUnavailable), errors.Is(err, mobileclient.ErrConfig):
		return ErrUnavailable
	case errors.Is(err, context.Canceled):
		return ErrCancelled
	case errors.Is(err, context.DeadlineExceeded):
		return ErrDeadline
	default:
		return ErrUnavailable
	}
}

// readBoundedLine reads one newline-terminated line of at most MaxFrameBytes
// bytes (newline excluded).  It returns the line without its newline.
func readBoundedLine(reader *bufio.Reader) ([]byte, int32) {
	line, err := reader.ReadSlice('\n')
	if err != nil && !(errors.Is(err, bufio.ErrBufferFull)) && len(line) == 0 {
		return nil, ErrUnavailable
	}
	collected := append([]byte(nil), line...)
	for !hasNewline(collected) {
		if len(collected) > MaxFrameBytes {
			return nil, ErrResponseTooLarge
		}
		more, err := reader.ReadSlice('\n')
		collected = append(collected, more...)
		if err != nil && len(more) == 0 {
			return nil, ErrUnavailable
		}
	}
	if len(collected) > MaxFrameBytes+1 {
		return nil, ErrResponseTooLarge
	}
	return collected[:len(collected)-1], ErrOK
}

func hasNewline(data []byte) bool {
	return len(data) > 0 && data[len(data)-1] == '\n'
}

// exchange dials one fresh connection (the same connect-per-call shape the
// desktop relay uses), writes the request as one line and returns one
// response line without its newline.
func exchange(id int64, request []byte, responseCap int32) ([]byte, int32, int32) {
	if len(request) == 0 || len(request) > MaxFrameBytes {
		return nil, ErrRequestTooLarge, 0
	}
	if responseCap < 1 {
		return nil, ErrConfig, 0
	}
	found, code := lookup(id)
	if code != ErrOK {
		return nil, code, 0
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	item := &inflight{cancel: cancel}
	found.mu.Lock()
	if found.closed {
		found.mu.Unlock()
		return nil, ErrInvalidHandle, 0
	}
	key := found.seq
	found.seq++
	found.cancels[key] = item
	found.mu.Unlock()
	defer func() {
		found.mu.Lock()
		delete(found.cancels, key)
		found.mu.Unlock()
	}()

	conn, err := found.client.DialContext(ctx)
	if err != nil {
		return nil, mapDialError(err), 0
	}
	defer conn.Close()
	found.mu.Lock()
	item.conn = conn
	closedDuringDial := found.closed
	found.mu.Unlock()
	if closedDuringDial {
		return nil, ErrCancelled, 0
	}
	if ctx.Err() != nil {
		return nil, ErrCancelled, 0
	}
	deadline := time.Now().Add(found.timeout)
	_ = conn.SetDeadline(deadline)

	line := append(append([]byte(nil), request...), '\n')
	if _, err := conn.Write(line); err != nil {
		return nil, exchangeIOError(err, ctx), 0
	}
	reader := bufio.NewReaderSize(conn, 4096)
	body, readCode := readBoundedLine(reader)
	if readCode != ErrOK {
		if readCode == ErrUnavailable {
			return nil, exchangeIOError(ctx.Err(), ctx), 0
		}
		return nil, readCode, 0
	}
	if ctx.Err() != nil {
		return nil, ErrCancelled, 0
	}
	if len(body) > int(responseCap) {
		return nil, ErrResponseCapTooSmall, int32(len(body))
	}
	return body, ErrOK, 0
}

func exchangeIOError(err error, ctx context.Context) int32 {
	if ctxErr := ctx.Err(); ctxErr != nil {
		if errors.Is(ctxErr, context.DeadlineExceeded) {
			return ErrDeadline
		}
		return ErrCancelled
	}
	if err == nil {
		return ErrUnavailable
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return ErrDeadline
	}
	return ErrUnavailable
}

// --- C ABI ------------------------------------------------------------------

//export hyprial_bridge_open
func hyprial_bridge_open(servicePort C.uint16_t, timeoutMs, limit, transportKind C.int32_t, outHandle *C.int64_t) C.int32_t {
	if outHandle == nil {
		return C.int32_t(ErrConfig)
	}
	*outHandle = 0
	id, code := openHandle(uint16(servicePort), int32(timeoutMs), int32(limit), int32(transportKind))
	if code != ErrOK {
		return C.int32_t(code)
	}
	*outHandle = C.int64_t(id)
	return C.int32_t(ErrOK)
}

//export hyprial_bridge_open_host
func hyprial_bridge_open_host(host *C.uint8_t, hostLen C.int32_t,
	servicePort C.uint16_t, timeoutMs, limit, transportKind C.int32_t, outHandle *C.int64_t) C.int32_t {
	if outHandle == nil {
		return C.int32_t(ErrConfig)
	}
	*outHandle = 0
	if host == nil || hostLen <= 0 || hostLen > MaxHostBytes {
		return C.int32_t(ErrConfig)
	}
	// Copy in; the caller's buffer is never retained.
	name := C.GoStringN((*C.char)(unsafe.Pointer(host)), hostLen)
	id, code := openHandleAt(name, uint16(servicePort), int32(timeoutMs), int32(limit), int32(transportKind))
	if code != ErrOK {
		return C.int32_t(code)
	}
	*outHandle = C.int64_t(id)
	return C.int32_t(ErrOK)
}

//export hyprial_bridge_open_tailnet
func hyprial_bridge_open_tailnet(clientPrivate *C.uint8_t, clientPrivateLen C.int32_t,
	serverAddress *C.uint8_t, serverAddressLen C.int32_t,
	servicePort C.uint16_t, timeoutMs, limit C.int32_t, outHandle *C.int64_t) C.int32_t {
	if outHandle == nil {
		return C.int32_t(ErrConfig)
	}
	*outHandle = 0
	if clientPrivate == nil || clientPrivateLen <= 0 ||
		serverAddress == nil || serverAddressLen <= 0 ||
		clientPrivateLen > MaxTailnetKeyBytes ||
		serverAddressLen > MaxTailnetAddressBytes {
		return C.int32_t(ErrConfig)
	}
	// Copy in: the caller's buffers are never retained.  Both values are
	// native-owned secrets/endpoints and never cross to the WebView.
	keyText := C.GoStringN((*C.char)(unsafe.Pointer(clientPrivate)), clientPrivateLen)
	address := C.GoStringN((*C.char)(unsafe.Pointer(serverAddress)), serverAddressLen)
	id, code := openTailnetAt(keyText, address, uint16(servicePort), int32(timeoutMs), int32(limit))
	if code != ErrOK {
		return C.int32_t(code)
	}
	*outHandle = C.int64_t(id)
	return C.int32_t(ErrOK)
}

//export hyprial_bridge_exchange
func hyprial_bridge_exchange(id C.int64_t, request *C.uint8_t, requestLen C.int32_t,
	response *C.uint8_t, responseCap C.int32_t, outLen *C.int32_t) C.int32_t {
	if outLen == nil {
		return C.int32_t(ErrConfig)
	}
	*outLen = 0
	if request == nil || requestLen <= 0 || response == nil || responseCap <= 0 {
		return C.int32_t(ErrConfig)
	}
	// Copy in: the caller's buffer is never retained and never read after
	// this call returns.
	payload := C.GoBytes(unsafe.Pointer(request), requestLen)
	body, code, required := exchange(int64(id), payload, int32(responseCap))
	if code == ErrResponseCapTooSmall {
		*outLen = C.int32_t(required)
		return C.int32_t(code)
	}
	if code != ErrOK {
		return C.int32_t(code)
	}
	if len(body) > 0 {
		C.memcpy(unsafe.Pointer(response), unsafe.Pointer(&body[0]), C.size_t(len(body)))
	}
	*outLen = C.int32_t(len(body))
	return C.int32_t(ErrOK)
}

//export hyprial_bridge_cancel
func hyprial_bridge_cancel(id C.int64_t) C.int32_t {
	return C.int32_t(cancelHandle(int64(id)))
}

//export hyprial_bridge_close
func hyprial_bridge_close(id C.int64_t) C.int32_t {
	return C.int32_t(closeHandle(int64(id)))
}

func main() {}
