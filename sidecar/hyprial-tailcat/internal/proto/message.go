// Package proto implements the hyprial-tailcat stdio protocol v6:
// newline-delimited JSON commands and events. It is a closed wire
// contract: unknown ops, unknown fields, and any version other than 5
// are rejected. See docs/design/tailnet-cutover-architecture-2026-10-03.md
// §3.3 (v3 baseline),
// docs/design/design-orgfs-requester-authentication-2026-10-08.md §3.3
// (v4: per-service admission on expose), and
// design branch design/socialware-data-layer-2026-10-06:
// docs/design/design-stable-zenoh-session-slots-2026-10-08.md §4
// (v4: fixed slot pool — up.slots, map-peer.slot, hold mode).
package proto

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/netip"
	"path/filepath"
	"strings"
	"sync"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/membership"
)

// Version is the only accepted protocol version. v5 adds deterministic
// per-server client-key derivation without exposing private material.
const Version = 6

// MaxRequestIDLength bounds correlation data accepted or echoed by v5.
const MaxRequestIDLength = 128

// Admission policy values accepted on expose commands. Absent means
// AdmissionAllow: an exposure is restricted to the allow set unless the
// daemon explicitly opens it.
const (
	AdmissionAny   = "any"
	AdmissionAllow = "allow"
)

// Error codes emitted on error events.
const (
	CodeBadRequest     = "BAD_REQUEST"
	CodeNotUp          = "NOT_UP"
	CodeAlreadyUp      = "ALREADY_UP"
	CodeKeyFileInvalid = "KEY_FILE_INVALID"
	CodeStartFailed    = "START_FAILED"
	CodePeerUnknown    = "PEER_UNKNOWN"
	CodeDialFailed     = "DIAL_FAILED"
	CodePortInUse      = "PORT_IN_USE"
	CodeInternal       = "INTERNAL"
)

// DefaultPeerPort is the Tailcat-side service port used when an up or
// map-peer command omits its port field.
const DefaultPeerPort = 7447

// DERPConfig selects a local test relay instead of the default DERP map.
type DERPConfig struct {
	Host             string `json:"host"`
	DERPPort         int    `json:"derpPort"`
	STUNPort         int    `json:"stunPort"`
	InsecureForTests bool   `json:"insecureForTests"`
}

// UpRequest is the parsed payload of an up command. Slots are the fixed
// outbound slot socket paths pre-listened in hold mode; SlotHoldMs bounds
// how long an unbound slot keeps a held connection (0 means the default,
// zenoh's open timeout).
type UpRequest struct {
	KeyFile         string
	MembershipStore string
	Owner           string
	DeviceID        string
	ControlPort     int
	InboundTarget   string
	PeerPort        int
	AddressFile     string
	DERP            *DERPConfig
	DERPMapURL      string
	DERPMapCacheDir string
	Slots           []string
	SlotHoldMs      int
}

// Command is a parsed protocol-v6 command. Which fields are populated
// depends on Op. Address is secret (it embeds the peer pre-shared key)
// and must never be logged or echoed back.
type Command struct {
	Op               string
	Membership       *membership.Request
	RequestID        string
	Name             string
	DeviceID         string
	Up               *UpRequest
	Peer             string
	Address          string // map-peer only; secret
	Slot             string // map-peer only; one of the up slots
	Port             int    // map-peer, expose, unexpose
	Target           string
	ProxyProtocol    string
	Admission        string // managed expose only; "allow"
	Addr             string
	ServerPublic     string
	RemotePort       int
	RecordGeneration int
	LocalPort        int
}

// PeerMapping is one entry of the peers array on state events: the
// slot the peer is bound to (the fixed pool is the only mapping shape).
type PeerMapping struct {
	Peer string `json:"peer"`
	Slot string `json:"slot,omitempty"`
}

// CopyErrors records the normalized outcomes of both io.Copy directions.
type CopyErrors struct {
	In  string `json:"in"`
	Out string `json:"out"`
}

// ConnectionClose is one completed connection through a mapped peer slot.
type ConnectionClose struct {
	Peer       string     `json:"peer"`
	Slot       string     `json:"slot,omitempty"`
	MapPort    *int       `json:"mapPort"`
	BytesIn    int64      `json:"bytesIn"`
	BytesOut   int64      `json:"bytesOut"`
	DurationMs int64      `json:"durationMs"`
	CloseSide  string     `json:"closeSide"`
	Errors     CopyErrors `json:"errors"`
}

// SlotConnectionTelemetry is one mapped slot's bounded close history.
type SlotConnectionTelemetry struct {
	Peer               string            `json:"peer"`
	Slot               string            `json:"slot,omitempty"`
	MapPort            *int              `json:"mapPort"`
	DialAttempts       int64             `json:"dialAttempts"`
	DialFailures       int64             `json:"dialFailures"`
	DialFailureReasons map[string]int64  `json:"dialFailureReasons"`
	SplicesEstablished int64             `json:"splicesEstablished"`
	DialFailureCount   int64             `json:"dialFailureCount"`
	LastDialAtMs       int64             `json:"lastDialAtMs"`
	LastDialError      string            `json:"lastDialError,omitempty"`
	ClosedCount        int64             `json:"closedCount"`
	CloseReasons       map[string]int64  `json:"closeReasons"`
	ErrorCounts        map[string]int64  `json:"errorCounts"`
	Recent             []ConnectionClose `json:"recent"`
}

// ConnectionTelemetry is additive status data, never a per-packet event stream.
type ConnectionTelemetry struct {
	RingCapacity int                       `json:"ringCapacity"`
	Slots        []SlotConnectionTelemetry `json:"slots"`
}

// Event is the common response envelope. Peers is a pointer so the key
// is always serialized on state events, even when empty.
type Event struct {
	V                   int                   `json:"v"`
	Event               string                `json:"event"`
	Sidecar             string                `json:"sidecar,omitempty"`
	Tailcat             string                `json:"tailcat,omitempty"`
	Capabilities        []string              `json:"capabilities,omitempty"`
	State               string                `json:"state,omitempty"`
	ServerPublic        string                `json:"serverPublic,omitempty"`
	ClientPublic        string                `json:"clientPublic,omitempty"`
	Peers               *[]PeerMapping        `json:"peers,omitempty"`
	Slots               []string              `json:"slots,omitempty"`
	ConnectionTelemetry *ConnectionTelemetry  `json:"connectionTelemetry,omitempty"`
	DERPMapURL          string                `json:"derpMapURL,omitempty"`
	DERPMapSource       string                `json:"derpMapSource,omitempty"`
	Code                string                `json:"code,omitempty"`
	Message             string                `json:"message,omitempty"`
	Peer                string                `json:"peer,omitempty"`
	Slot                string                `json:"slot,omitempty"`
	Port                int                   `json:"port,omitempty"`
	Target              string                `json:"target,omitempty"`
	ProxyProtocol       *string               `json:"proxyProtocol,omitempty"`
	Admission           *string               `json:"admission,omitempty"`
	Addr                string                `json:"addr,omitempty"`
	Found               *bool                 `json:"found,omitempty"`
	Key                 *string               `json:"key,omitempty"`
	RequestID           string                `json:"requestId,omitempty"`
	Service             *ServiceMapping       `json:"service,omitempty"`
	Services            *[]ServiceMapping     `json:"services,omitempty"`
	Name                any                   `json:"name,omitempty"`
	Removed             *bool                 `json:"removed,omitempty"`
	Op                  string                `json:"op,omitempty"`
	Data                json.RawMessage       `json:"data,omitempty"`
	Readiness           *membership.Readiness `json:"readiness,omitempty"`
	TailnetID           string                `json:"tailnetId,omitempty"`
	Revision            *uint64               `json:"revision,omitempty"`
}

// Capabilities is the fixed capability list announced on hello.
var Capabilities = []string{
	"map-peer", "unmap-peer", "expose", "unexpose", "peer-key",
	"tailnet-membership-v1", "tailnet-control-v1", "unix-inbound-v1",
	"service-connect-v1", "forwarding-request-id-v1", "peer-slots-v1",
	"connection-telemetry-v1", "derp-map-v1", "client-key-derivation-v1",
}

func Hello(sidecar, tailcat string) Event {
	return Event{
		V: Version, Event: "hello", Sidecar: sidecar, Tailcat: tailcat,
		Capabilities: Capabilities,
	}
}

// State emits a bare state transition (e.g. "Starting").
func State(state string) Event {
	return Event{V: Version, Event: "state", State: state, Peers: &[]PeerMapping{}}
}

// RunningState is the successful up or status result; the peers key is
// always serialized, as [] when empty.  Slots are the resolved slot wire
// ids in up order (growth appended) -- on the tcp transport they carry
// the kernel-assigned ports the daemon dials; omitted when the pool is
// empty (a slot-free up).
func RunningState(serverPublic, clientPublic string, peers []PeerMapping, slots []string) Event {
	if peers == nil {
		peers = []PeerMapping{}
	}
	return Event{
		V: Version, Event: "state", State: "Running",
		ServerPublic: serverPublic, ClientPublic: clientPublic, Peers: &peers,
		Slots: slots,
	}
}

// SlotMapped is the peer-mapped event: a slot binding reporting the
// resolved slot wire id (there is no localPort shape in v5).
func SlotMapped(peer, slot string) Event {
	return Event{V: Version, Event: "peer-mapped", Peer: peer, Slot: slot}
}

func PeerUnmapped(peer string) Event {
	return Event{V: Version, Event: "peer-unmapped", Peer: peer}
}

func ClientKeyDerived(serverPublic, clientPublic string) Event {
	return Event{
		V: Version, Event: "client-key-derived",
		ServerPublic: serverPublic, ClientPublic: clientPublic,
	}
}

// Exposed always serializes proxyProtocol (even when empty, per §3.3)
// and admission.
func Exposed(port int, target, proxyProtocol, admission string) Event {
	return Event{
		V: Version, Event: "exposed", Port: port, Target: target,
		ProxyProtocol: &proxyProtocol, Admission: &admission,
	}
}

func Unexposed(port int) Event {
	return Event{V: Version, Event: "unexposed", Port: port}
}

// PeerKey always serializes key (empty when found is false), per §3.3.
func PeerKey(addr string, found bool, key string) Event {
	return Event{V: Version, Event: "peer-key", Addr: addr, Found: &found, Key: &key}
}

func Error(code, message string) Event {
	return Event{V: Version, Event: "error", Code: code, Message: message}
}

func Exited() Event { return Event{V: Version, Event: "exited"} }

// Writer serializes whole JSON lines.
type Writer struct {
	mu  sync.Mutex
	enc *json.Encoder
}

func NewWriter(w io.Writer) *Writer { return &Writer{enc: json.NewEncoder(w)} }

func (w *Writer) Write(event Event) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if event.Event == "membership-changed" {
		var code any
		if event.Code != "" {
			code = event.Code
		}
		return w.enc.Encode(map[string]any{"v": event.V, "event": event.Event, "tailnetId": event.TailnetID, "revision": event.Revision, "code": code})
	}
	return w.enc.Encode(event)
}

var errProtocol = errors.New("protocol error")

func ProtocolError(message string) error { return fmt.Errorf("%w: %s", errProtocol, message) }
func IsProtocolError(err error) bool     { return errors.Is(err, errProtocol) }

// ParseCommand rejects malformed, version-mismatched, unknown-op,
// missing, and extra fields, keeping v5 a closed wire contract.
func ParseCommand(line []byte) (Command, error) {
	var raw map[string]json.RawMessage
	if err := decodeExact(line, &raw); err != nil {
		return Command{}, ProtocolError("request must be one JSON object")
	}
	vRaw, ok := raw["v"]
	if !ok {
		return Command{}, ProtocolError("missing v")
	}
	var version int
	if err := json.Unmarshal(vRaw, &version); err != nil || version != Version {
		return Command{}, ProtocolError("unsupported protocol version")
	}
	opRaw, ok := raw["op"]
	if !ok {
		return Command{}, ProtocolError("missing op")
	}
	var op string
	if err := json.Unmarshal(opRaw, &op); err != nil {
		return Command{}, ProtocolError("op must be a string")
	}
	if membership.IsOp(op) {
		r, err := membership.Parse(line, false)
		return Command{Op: op, RequestID: r.RequestID, Membership: &r}, err
	}
	switch op {
	case "up":
		return parseUp(line, raw)
	case "map-peer":
		return parseMapPeer(line, raw)
	case "map-service":
		return parseMapService(line, raw)
	case "unmap-service":
		return parseUnmapService(line, raw)
	case "service-status":
		return parseServiceStatus(line, raw)
	case "unmap-peer":
		if err := onlyKeys(raw, "v", "op", "requestId", "peer"); err != nil {
			return Command{}, err
		}
		var wire struct {
			V         int    `json:"v"`
			Op        string `json:"op"`
			RequestID string `json:"requestId,omitempty"`
			Peer      string `json:"peer"`
		}
		if err := decodeExact(line, &wire); err != nil || wire.Peer == "" {
			return Command{}, ProtocolError("peer must be a non-empty string")
		}
		requestID, err := optionalRequestID(raw)
		return Command{Op: op, RequestID: requestID, Peer: wire.Peer}, err
	case "allow":
		return Command{}, ProtocolError("allow is retired")
	case "derive-client-key":
		if err := onlyKeys(raw, "v", "op", "requestId", "serverPublic"); err != nil {
			return Command{}, err
		}
		var wire struct {
			V            int    `json:"v"`
			Op           string `json:"op"`
			RequestID    string `json:"requestId,omitempty"`
			ServerPublic string `json:"serverPublic"`
		}
		if err := decodeExact(line, &wire); err != nil || !strings.HasPrefix(wire.ServerPublic, "nodekey:") {
			return Command{}, ProtocolError("serverPublic must be a nodekey string")
		}
		requestID, err := optionalRequestID(raw)
		if err != nil {
			return Command{}, err
		}
		if requestID == "" {
			return Command{}, ProtocolError("derive-client-key requires requestId")
		}
		return Command{Op: op, RequestID: requestID, ServerPublic: wire.ServerPublic}, nil
	case "expose":
		return parseExpose(line, raw)
	case "unexpose":
		if err := onlyKeys(raw, "v", "op", "requestId", "port"); err != nil {
			return Command{}, err
		}
		var wire struct {
			V         int    `json:"v"`
			Op        string `json:"op"`
			RequestID string `json:"requestId,omitempty"`
			Port      int    `json:"port"`
		}
		if err := decodeExact(line, &wire); err != nil || wire.Port <= 0 || wire.Port > 65535 {
			return Command{}, ProtocolError("port must be a valid TCP port")
		}
		requestID, err := optionalRequestID(raw)
		return Command{Op: op, RequestID: requestID, Port: wire.Port}, err
	case "peer-key":
		if err := onlyKeys(raw, "v", "op", "requestId", "addr"); err != nil {
			return Command{}, err
		}
		var wire struct {
			V         int    `json:"v"`
			Op        string `json:"op"`
			RequestID string `json:"requestId,omitempty"`
			Addr      string `json:"addr"`
		}
		if err := decodeExact(line, &wire); err != nil {
			return Command{}, ProtocolError("invalid peer-key fields")
		}
		if _, err := netip.ParseAddrPort(wire.Addr); err != nil {
			return Command{}, ProtocolError("addr must be an IP:port")
		}
		requestID, err := optionalRequestID(raw)
		return Command{Op: op, RequestID: requestID, Addr: wire.Addr}, err
	case "status":
		if err := onlyKeys(raw, "v", "op", "requestId"); err != nil {
			return Command{}, err
		}
		requestID, err := optionalRequestID(raw)
		return Command{Op: op, RequestID: requestID}, err
	case "down":
		if err := onlyKeys(raw, "v", "op"); err != nil {
			return Command{}, err
		}
		return Command{Op: op}, nil
	default:
		return Command{}, ProtocolError("unknown op")
	}
}

// tcpSlotGrowSpec asks for one new loopback TCP slot with a
// kernel-assigned port (the Windows transport -- zenoh dials no unix
// sockets there; design §8 and notes/windows-unixsock-2026-10-08.md).
const tcpSlotGrowSpec = "tcp:127.0.0.1:0"

// ValidateSlotSpec accepts the two slot wire-id forms: an absolute unix
// socket path, or "tcp:<loopback-ip>:<port>" (port 0 = kernel-assigned,
// the grow spec).
func ValidateSlotSpec(spec string) error {
	if strings.HasPrefix(spec, "tcp:") {
		address, err := netip.ParseAddrPort(spec[len("tcp:"):])
		if err != nil || !address.Addr().IsLoopback() {
			return ProtocolError("tcp slot must be an explicit loopback ip:port")
		}
		return nil
	}
	if !filepath.IsAbs(spec) {
		return ProtocolError("slot must be an absolute socket path or a tcp:ip:port spec")
	}
	return nil
}

type upWire struct {
	V               int             `json:"v"`
	Op              string          `json:"op"`
	RequestID       string          `json:"requestId"`
	MembershipStore string          `json:"membershipStore"`
	Owner           string          `json:"owner"`
	DeviceID        string          `json:"deviceId"`
	ControlPort     int             `json:"controlPort,omitempty"`
	KeyFile         string          `json:"keyFile"`
	InboundTarget   string          `json:"inboundTarget,omitempty"`
	PeerPort        int             `json:"peerPort,omitempty"`
	AddressFile     string          `json:"addressFile"`
	DERP            json.RawMessage `json:"derp,omitempty"`
	DERPMapURL      string          `json:"derpMapURL,omitempty"`
	DERPMapCacheDir string          `json:"derpMapCacheDir,omitempty"`
	Slots           []string        `json:"slots,omitempty"`
	SlotHoldMs      int             `json:"slotHoldMs,omitempty"`
}

func parseUp(line []byte, raw map[string]json.RawMessage) (Command, error) {
	if err := onlyKeys(
		raw, "v", "op", "requestId", "keyFile", "membershipStore", "owner", "deviceId", "controlPort",
		"inboundTarget", "peerPort", "addressFile", "derp",
		"derpMapURL", "derpMapCacheDir", "slots", "slotHoldMs",
	); err != nil {
		return Command{}, err
	}
	for _, key := range []string{"requestId", "keyFile", "membershipStore", "owner", "deviceId", "addressFile", "inboundTarget"} {
		if _, ok := raw[key]; !ok {
			return Command{}, ProtocolError("missing " + key)
		}
	}
	var wire upWire
	if err := decodeExact(line, &wire); err != nil {
		return Command{}, ProtocolError("invalid up fields")
	}
	if !filepath.IsAbs(wire.KeyFile) {
		return Command{}, ProtocolError("keyFile must be an absolute path")
	}
	if !filepath.IsAbs(wire.AddressFile) {
		return Command{}, ProtocolError("addressFile must be an absolute path")
	}
	if !membership.ID(wire.RequestID, 128) || !membership.ID(wire.Owner, 1024) || !membership.ID(wire.DeviceID, 1024) || !filepath.IsAbs(wire.MembershipStore) {
		return Command{}, ProtocolError("invalid membership configuration")
	}
	if wire.ControlPort == 0 {
		wire.ControlPort = 7448
	}
	if wire.ControlPort < 1 || wire.ControlPort > 65535 {
		return Command{}, ProtocolError("invalid controlPort")
	}
	if wire.PeerPort < 0 || wire.PeerPort > 65535 {
		return Command{}, ProtocolError("peerPort must be a valid TCP port")
	}
	if wire.PeerPort == 0 {
		wire.PeerPort = DefaultPeerPort
	}
	if !strings.HasPrefix(wire.InboundTarget, "unix:") || !filepath.IsAbs(strings.TrimPrefix(wire.InboundTarget, "unix:")) || wire.ControlPort == wire.PeerPort {
		return Command{}, ProtocolError("private Unix inboundTarget and separate control port required")
	}
	if wire.DERPMapURL != "" && !strings.HasPrefix(wire.DERPMapURL, "https://") {
		return Command{}, ProtocolError("derpMapURL must be https")
	}
	if wire.DERPMapCacheDir != "" && !filepath.IsAbs(wire.DERPMapCacheDir) {
		return Command{}, ProtocolError("derpMapCacheDir must be an absolute path")
	}
	if wire.DERP != nil && wire.DERPMapURL != "" {
		return Command{}, ProtocolError("derp and derpMapURL are mutually exclusive")
	}
	seen := make(map[string]struct{}, len(wire.Slots))
	for _, slot := range wire.Slots {
		if err := ValidateSlotSpec(slot); err != nil {
			return Command{}, err
		}
		// A tcp grow spec (port 0) may repeat: each occurrence asks for
		// its own kernel-assigned listener.
		if slot == tcpSlotGrowSpec {
			continue
		}
		if _, dup := seen[slot]; dup {
			return Command{}, ProtocolError("slots entries must be unique")
		}
		seen[slot] = struct{}{}
	}
	if wire.SlotHoldMs < 0 {
		return Command{}, ProtocolError("slotHoldMs must not be negative")
	}
	if wire.SlotHoldMs > 0 && len(wire.Slots) == 0 {
		return Command{}, ProtocolError("slotHoldMs requires slots")
	}
	var derp *DERPConfig
	if len(wire.DERP) > 0 && !bytes.Equal(bytes.TrimSpace(wire.DERP), []byte("null")) {
		var value DERPConfig
		if err := decodeExact(wire.DERP, &value); err != nil {
			return Command{}, ProtocolError("invalid derp fields")
		}
		if value.Host == "" || value.DERPPort <= 0 || value.DERPPort > 65535 ||
			value.STUNPort <= 0 || value.STUNPort > 65535 {
			return Command{}, ProtocolError("derp requires host, derpPort, and stunPort")
		}
		derp = &value
	}
	return Command{Op: "up", RequestID: wire.RequestID, Up: &UpRequest{
		KeyFile: wire.KeyFile, MembershipStore: wire.MembershipStore, Owner: wire.Owner, DeviceID: wire.DeviceID, ControlPort: wire.ControlPort,
		InboundTarget: wire.InboundTarget, PeerPort: wire.PeerPort,
		AddressFile: wire.AddressFile, DERP: derp,
		DERPMapURL: wire.DERPMapURL, DERPMapCacheDir: wire.DERPMapCacheDir,
		Slots: wire.Slots, SlotHoldMs: wire.SlotHoldMs,
	}}, nil
}

func parseMapPeer(line []byte, raw map[string]json.RawMessage) (Command, error) {
	if err := onlyKeys(raw, "v", "op", "requestId", "peer", "address", "slot", "port"); err != nil {
		return Command{}, err
	}
	if _, ok := raw["address"]; !ok {
		return Command{}, ProtocolError("missing address")
	}
	var wire struct {
		V         int    `json:"v"`
		Op        string `json:"op"`
		RequestID string `json:"requestId,omitempty"`
		Peer      string `json:"peer"`
		Address   string `json:"address"`
		Slot      string `json:"slot,omitempty"`
		Port      int    `json:"port,omitempty"`
	}
	if err := decodeExact(line, &wire); err != nil {
		return Command{}, ProtocolError("invalid map-peer fields")
	}
	if wire.Peer == "" {
		return Command{}, ProtocolError("peer must be a non-empty string")
	}
	if !strings.HasPrefix(wire.Address, "tc") {
		return Command{}, ProtocolError("address must be a tailcat address")
	}
	if _, present := raw["slot"]; !present {
		return Command{}, ProtocolError("missing slot")
	}
	if err := ValidateSlotSpec(wire.Slot); err != nil {
		return Command{}, err
	}
	if wire.Port < 0 || wire.Port > 65535 {
		return Command{}, ProtocolError("port must be a valid TCP port")
	}
	if wire.Port == 0 {
		wire.Port = DefaultPeerPort
	}
	requestID, err := optionalRequestID(raw)
	return Command{
		Op: "map-peer", RequestID: requestID, Peer: wire.Peer,
		Address: wire.Address, Slot: wire.Slot, Port: wire.Port,
	}, err
}

func parseExpose(line []byte, raw map[string]json.RawMessage) (Command, error) {
	if err := onlyKeys(raw, "v", "op", "requestId", "port", "target", "proxyProtocol", "admission"); err != nil {
		return Command{}, err
	}
	for _, key := range []string{"port", "target", "proxyProtocol"} {
		if _, ok := raw[key]; !ok {
			return Command{}, ProtocolError("missing " + key)
		}
	}
	var wire struct {
		V             int    `json:"v"`
		Op            string `json:"op"`
		RequestID     string `json:"requestId,omitempty"`
		Port          int    `json:"port"`
		Target        string `json:"target"`
		ProxyProtocol string `json:"proxyProtocol"`
		Admission     string `json:"admission,omitempty"`
	}
	if err := decodeExact(line, &wire); err != nil {
		return Command{}, ProtocolError("invalid expose fields")
	}
	// Managed exposures always use membership admission; the wire spelling
	// remains "allow". Public "any" admission is rejected.
	if _, ok := raw["admission"]; !ok {
		wire.Admission = AdmissionAllow
	}
	if err := ValidateExposure(wire.Port, wire.Target, wire.ProxyProtocol, wire.Admission); err != nil {
		return Command{}, err
	}
	requestID, err := optionalRequestID(raw)
	return Command{
		Op: "expose", RequestID: requestID, Port: wire.Port,
		Target: wire.Target, ProxyProtocol: wire.ProxyProtocol, Admission: wire.Admission,
	}, err
}

func optionalRequestID(raw map[string]json.RawMessage) (string, error) {
	value, ok := raw["requestId"]
	if !ok {
		return "", nil
	}
	var requestID string
	if err := json.Unmarshal(value, &requestID); err != nil || requestID == "" || len(requestID) > MaxRequestIDLength {
		return "", ProtocolError("requestId must be a non-empty string")
	}
	return requestID, nil
}

// ValidateExposure checks a port/target/proxyProtocol/admission tuple.
func ValidateExposure(port int, target, proxyProtocol, admission string) error {
	if port <= 0 || port > 65535 {
		return ProtocolError("port must be a valid TCP port")
	}
	if proxyProtocol != "" && proxyProtocol != "v2" {
		return ProtocolError("proxyProtocol must be empty or v2")
	}
	if admission != AdmissionAllow {
		return ProtocolError("admission must be allow")
	}
	switch {
	case strings.HasPrefix(target, "unix:"):
		if !filepath.IsAbs(strings.TrimPrefix(target, "unix:")) {
			return ProtocolError("unix target must be absolute")
		}
	case strings.HasPrefix(target, "tcp:"):
		return validateTCPTarget(target)
	default:
		return ProtocolError("target must start with unix: or tcp:")
	}
	return nil
}

func validateTCPTarget(target string) error {
	address, err := netip.ParseAddrPort(strings.TrimPrefix(target, "tcp:"))
	if err != nil || !address.Addr().IsLoopback() || address.Port() == 0 {
		return ProtocolError("tcp target must be an explicit loopback address")
	}
	return nil
}

// eventFields is the closed per-event key set used by ParseEvent.
var eventFields = map[string][]string{
	"hello":              {"sidecar", "tailcat", "capabilities"},
	"state":              {"state", "serverPublic", "clientPublic", "peers", "slots", "connectionTelemetry", "derpMapURL", "derpMapSource", "requestId", "readiness"},
	"peer-mapped":        {"peer", "slot", "requestId"},
	"peer-unmapped":      {"peer", "requestId"},
	"allowed":            {"count", "allowAny", "requestId"},
	"client-key-derived": {"serverPublic", "clientPublic", "requestId"},
	"exposed":            {"port", "target", "proxyProtocol", "admission", "requestId"},
	"unexposed":          {"port", "requestId"},
	"peer-key":           {"addr", "found", "key", "requestId"},
	"service-mapped":     {"requestId", "service"},
	"service-unmapped":   {"requestId", "name", "removed"},
	"service-status":     {"requestId", "services"},
	"service-failed":     {"requestId", "op", "name", "code", "message"},
	"error":              {"code", "message", "requestId", "op"},
	"membership-result":  {"requestId", "op", "data"},
	"membership-changed": {"tailnetId", "revision", "code"},
	"readiness":          {"readiness"},
	"exited":             {},
}

// ParseEvent validates one protocol-v6 event line: version 6, a known
// event name, and only the fields defined for that event. Tests and
// contract golden checkers use it to exercise the same closed contract
// on the event side.
func ParseEvent(line []byte) (Event, error) {
	var raw map[string]json.RawMessage
	if err := decodeExact(line, &raw); err != nil {
		return Event{}, ProtocolError("event must be one JSON object")
	}
	var version int
	if value, ok := raw["v"]; !ok {
		return Event{}, ProtocolError("missing v")
	} else if err := json.Unmarshal(value, &version); err != nil || version != Version {
		return Event{}, ProtocolError("unsupported protocol version")
	}
	var name string
	if value, ok := raw["event"]; !ok {
		return Event{}, ProtocolError("missing event")
	} else if err := json.Unmarshal(value, &name); err != nil {
		return Event{}, ProtocolError("event must be a string")
	}
	fields, ok := eventFields[name]
	if !ok {
		return Event{}, ProtocolError("unknown event")
	}
	if err := onlyKeys(raw, append([]string{"v", "event"}, fields...)...); err != nil {
		return Event{}, err
	}
	var event Event
	if err := decodeExact(line, &event); err != nil {
		return Event{}, ProtocolError("invalid event fields")
	}
	if strings.HasPrefix(name, "service-") {
		if err := validateServiceEvent(raw, event); err != nil {
			return Event{}, err
		}
	} else if _, err := optionalRequestID(raw); err != nil {
		return Event{}, err
	}
	if name == "membership-result" {
		if event.RequestID == "" || membership.ValidateResult(event.Op, event.Data) != nil {
			return Event{}, ProtocolError("invalid membership result")
		}
	}
	if name == "readiness" || (name == "state" && event.Readiness != nil) {
		var r membership.Readiness
		if membership.DecodeFields(raw["readiness"], &r) != nil {
			return Event{}, ProtocolError("invalid readiness")
		}
	}
	if name == "membership-changed" {
		if !membership.ID(event.TailnetID, 1024) || event.Revision == nil || *event.Revision > membership.MaxNumber || raw["code"] == nil {
			return Event{}, ProtocolError("invalid membership change")
		}
	}
	invalidDerived := event.RequestID == "" ||
		!strings.HasPrefix(event.ServerPublic, "nodekey:") ||
		!strings.HasPrefix(event.ClientPublic, "nodekey:")
	if name == "client-key-derived" && invalidDerived {
		return Event{}, ProtocolError("invalid client-key-derived event")
	}
	return event, nil
}

func onlyKeys(raw map[string]json.RawMessage, allowed ...string) error {
	set := make(map[string]struct{}, len(allowed))
	for _, key := range allowed {
		set[key] = struct{}{}
	}
	for key := range raw {
		if _, ok := set[key]; !ok {
			return ProtocolError("unknown field " + key)
		}
	}
	return nil
}

func decodeExact(data []byte, target any) error {
	if err := membership.Decode(data, new(any)); err != nil {
		return err
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(target); err != nil {
		return err
	}
	if dec.Decode(new(any)) != io.EOF {
		return errors.New("trailing JSON")
	}
	return nil
}
