// Package proto implements the hyprial-tailcat stdio protocol v3:
// newline-delimited JSON commands and events. It is a closed wire
// contract: unknown ops, unknown fields, and any version other than 3
// are rejected. See docs/design/tailnet-cutover-architecture-2026-10-03.md
// §3.3.
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
)

// Version is the only accepted protocol version.
const Version = 3

// MaxRequestIDLength bounds correlation data accepted or echoed by v3.
const MaxRequestIDLength = 128

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

// UpRequest is the parsed payload of an up command.
type UpRequest struct {
	KeyFile       string
	Allow         []string
	AllowAny      bool
	InboundTarget string
	PeerPort      int
	AddressFile   string
	DERP          *DERPConfig
}

// Command is a parsed protocol-v3 command. Which fields are populated
// depends on Op. Address is secret (it embeds the peer pre-shared key)
// and must never be logged or echoed back.
type Command struct {
	Op               string
	RequestID        string
	Name             string
	DeviceID         string
	Up               *UpRequest
	Peer             string
	Address          string // map-peer only; secret
	Port             int    // map-peer, expose, unexpose
	Keys             []string
	AllowAny         bool
	Target           string
	ProxyProtocol    string
	Addr             string
	ServerPublic     string
	RemotePort       int
	RecordGeneration int
	LocalPort        int
}

// PeerMapping is one entry of the peers array on state events.
type PeerMapping struct {
	Peer      string `json:"peer"`
	LocalPort int    `json:"localPort"`
}

// Event is the common response envelope. Peers is a pointer so the key
// is always serialized on state events, even when empty.
type Event struct {
	V             int               `json:"v"`
	Event         string            `json:"event"`
	Sidecar       string            `json:"sidecar,omitempty"`
	Tailcat       string            `json:"tailcat,omitempty"`
	Capabilities  []string          `json:"capabilities,omitempty"`
	State         string            `json:"state,omitempty"`
	ServerPublic  string            `json:"serverPublic,omitempty"`
	ClientPublic  string            `json:"clientPublic,omitempty"`
	Peers         *[]PeerMapping    `json:"peers,omitempty"`
	Code          string            `json:"code,omitempty"`
	Message       string            `json:"message,omitempty"`
	Peer          string            `json:"peer,omitempty"`
	LocalPort     int               `json:"localPort,omitempty"`
	Count         *int              `json:"count,omitempty"`
	AllowAny      *bool             `json:"allowAny,omitempty"`
	Port          int               `json:"port,omitempty"`
	Target        string            `json:"target,omitempty"`
	ProxyProtocol *string           `json:"proxyProtocol,omitempty"`
	Addr          string            `json:"addr,omitempty"`
	Found         *bool             `json:"found,omitempty"`
	Key           *string           `json:"key,omitempty"`
	RequestID     string            `json:"requestId,omitempty"`
	Service       *ServiceMapping   `json:"service,omitempty"`
	Services      *[]ServiceMapping `json:"services,omitempty"`
	Name          any               `json:"name,omitempty"`
	Removed       *bool             `json:"removed,omitempty"`
	Op            string            `json:"op,omitempty"`
}

// Capabilities is the fixed capability list announced on hello.
var Capabilities = []string{
	"map-peer", "unmap-peer", "allow", "expose", "unexpose", "peer-key",
	"service-connect-v1", "forwarding-request-id-v1",
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
// always serialized, as [] when empty.
func RunningState(serverPublic, clientPublic string, peers []PeerMapping) Event {
	if peers == nil {
		peers = []PeerMapping{}
	}
	return Event{
		V: Version, Event: "state", State: "Running",
		ServerPublic: serverPublic, ClientPublic: clientPublic, Peers: &peers,
	}
}

func PeerMapped(peer string, localPort int) Event {
	return Event{V: Version, Event: "peer-mapped", Peer: peer, LocalPort: localPort}
}

func PeerUnmapped(peer string) Event {
	return Event{V: Version, Event: "peer-unmapped", Peer: peer}
}

func Allowed(count int, allowAny bool) Event {
	return Event{V: Version, Event: "allowed", Count: &count, AllowAny: &allowAny}
}

// Exposed always serializes proxyProtocol, even when empty, per §3.3.
func Exposed(port int, target, proxyProtocol string) Event {
	return Event{V: Version, Event: "exposed", Port: port, Target: target, ProxyProtocol: &proxyProtocol}
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
	return w.enc.Encode(event)
}

var errProtocol = errors.New("protocol error")

func ProtocolError(message string) error { return fmt.Errorf("%w: %s", errProtocol, message) }
func IsProtocolError(err error) bool     { return errors.Is(err, errProtocol) }

// ParseCommand rejects malformed, version-mismatched, unknown-op,
// missing, and extra fields, keeping v3 a closed wire contract.
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
		return parseAllow(line, raw)
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

type upWire struct {
	V             int             `json:"v"`
	Op            string          `json:"op"`
	KeyFile       string          `json:"keyFile"`
	Allow         []string        `json:"allow"`
	AllowAny      bool            `json:"allowAny"`
	InboundTarget string          `json:"inboundTarget,omitempty"`
	PeerPort      int             `json:"peerPort,omitempty"`
	AddressFile   string          `json:"addressFile"`
	DERP          json.RawMessage `json:"derp,omitempty"`
}

func parseUp(line []byte, raw map[string]json.RawMessage) (Command, error) {
	if err := onlyKeys(
		raw, "v", "op", "keyFile", "allow", "allowAny",
		"inboundTarget", "peerPort", "addressFile", "derp",
	); err != nil {
		return Command{}, err
	}
	for _, key := range []string{"keyFile", "allow", "allowAny", "addressFile"} {
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
	for _, entry := range wire.Allow {
		if !strings.HasPrefix(entry, "nodekey:") {
			return Command{}, ProtocolError("allow entries must be nodekey strings")
		}
	}
	if wire.PeerPort < 0 || wire.PeerPort > 65535 {
		return Command{}, ProtocolError("peerPort must be a valid TCP port")
	}
	if wire.PeerPort == 0 {
		wire.PeerPort = DefaultPeerPort
	}
	if wire.InboundTarget != "" {
		if err := validateTCPTarget(wire.InboundTarget); err != nil {
			return Command{}, err
		}
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
	return Command{Op: "up", Up: &UpRequest{
		KeyFile: wire.KeyFile, Allow: wire.Allow, AllowAny: wire.AllowAny,
		InboundTarget: wire.InboundTarget, PeerPort: wire.PeerPort,
		AddressFile: wire.AddressFile, DERP: derp,
	}}, nil
}

func parseMapPeer(line []byte, raw map[string]json.RawMessage) (Command, error) {
	if err := onlyKeys(raw, "v", "op", "requestId", "peer", "address", "port"); err != nil {
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
	if wire.Port < 0 || wire.Port > 65535 {
		return Command{}, ProtocolError("port must be a valid TCP port")
	}
	if wire.Port == 0 {
		wire.Port = DefaultPeerPort
	}
	requestID, err := optionalRequestID(raw)
	return Command{Op: "map-peer", RequestID: requestID, Peer: wire.Peer, Address: wire.Address, Port: wire.Port}, err
}

func parseAllow(line []byte, raw map[string]json.RawMessage) (Command, error) {
	if err := onlyKeys(raw, "v", "op", "requestId", "keys", "allowAny"); err != nil {
		return Command{}, err
	}
	for _, key := range []string{"keys", "allowAny"} {
		if _, ok := raw[key]; !ok {
			return Command{}, ProtocolError("missing " + key)
		}
	}
	var wire struct {
		V         int      `json:"v"`
		Op        string   `json:"op"`
		RequestID string   `json:"requestId,omitempty"`
		Keys      []string `json:"keys"`
		AllowAny  bool     `json:"allowAny"`
	}
	if err := decodeExact(line, &wire); err != nil {
		return Command{}, ProtocolError("invalid allow fields")
	}
	for _, entry := range wire.Keys {
		if !strings.HasPrefix(entry, "nodekey:") {
			return Command{}, ProtocolError("keys entries must be nodekey strings")
		}
	}
	requestID, err := optionalRequestID(raw)
	return Command{Op: "allow", RequestID: requestID, Keys: wire.Keys, AllowAny: wire.AllowAny}, err
}

func parseExpose(line []byte, raw map[string]json.RawMessage) (Command, error) {
	if err := onlyKeys(raw, "v", "op", "requestId", "port", "target", "proxyProtocol"); err != nil {
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
	}
	if err := decodeExact(line, &wire); err != nil {
		return Command{}, ProtocolError("invalid expose fields")
	}
	if err := ValidateExposure(wire.Port, wire.Target, wire.ProxyProtocol); err != nil {
		return Command{}, err
	}
	requestID, err := optionalRequestID(raw)
	return Command{Op: "expose", RequestID: requestID, Port: wire.Port, Target: wire.Target, ProxyProtocol: wire.ProxyProtocol}, err
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

// ValidateExposure checks a port/target/proxyProtocol triple.
func ValidateExposure(port int, target, proxyProtocol string) error {
	if port <= 0 || port > 65535 {
		return ProtocolError("port must be a valid TCP port")
	}
	if proxyProtocol != "" && proxyProtocol != "v2" {
		return ProtocolError("proxyProtocol must be empty or v2")
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
	"hello":            {"sidecar", "tailcat", "capabilities"},
	"state":            {"state", "serverPublic", "clientPublic", "peers", "requestId"},
	"peer-mapped":      {"peer", "localPort", "requestId"},
	"peer-unmapped":    {"peer", "requestId"},
	"allowed":          {"count", "allowAny", "requestId"},
	"exposed":          {"port", "target", "proxyProtocol", "requestId"},
	"unexposed":        {"port", "requestId"},
	"peer-key":         {"addr", "found", "key", "requestId"},
	"service-mapped":   {"requestId", "service"},
	"service-unmapped": {"requestId", "name", "removed"},
	"service-status":   {"requestId", "services"},
	"service-failed":   {"requestId", "op", "name", "code", "message"},
	"error":            {"code", "message", "requestId"},
	"exited":           {},
}

// ParseEvent validates one protocol-v3 event line: version 3, a known
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
