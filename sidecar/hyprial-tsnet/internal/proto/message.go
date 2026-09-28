// Package proto implements protocol-v1 newline-delimited JSON messages.
package proto

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sync"
)

const Version = 1
const ForwardVersion = 2

// UpRequest is the exact protocol-v1 up payload. ControlURL is deliberately
// not parsed or normalized; the node package passes it byte-for-byte to tsnet.
type UpRequest struct {
	Version        int
	ControlURL     string
	Hostname       string
	Dir            string
	Ephemeral      bool
	Join           string
	AuthKey        *string
	Proxy          string
	TimeoutSeconds *int
	Resume         bool
	InboundTarget  string
	PeerPort       int
}

type Command struct {
	Op   string
	Up   *UpRequest
	Peer string
}

type PeerMapping struct {
	Peer      string `json:"peer"`
	LocalPort int    `json:"localPort"`
}

// Event is the common response envelope. ControlURL is a pointer so an empty
// official-control-plane value is still serialized on ready/status events.
// Peers and Tags follow the same rule: "no online peer yet" is a real answer,
// so the key must reach the wire as [] rather than being dropped by omitempty
// -- the daemon's reader requires it to be present, and a slice that is merely
// empty would vanish.
type Event struct {
	V                  int           `json:"v"`
	Event              string        `json:"event"`
	Sidecar            string        `json:"sidecar,omitempty"`
	Tailscale          string        `json:"tailscale,omitempty"`
	State              string        `json:"state,omitempty"`
	URL                string        `json:"url,omitempty"`
	IP4                string        `json:"ip4,omitempty"`
	IP6                string        `json:"ip6,omitempty"`
	Hostname           string        `json:"hostname,omitempty"`
	User               string        `json:"user,omitempty"`
	Tags               *[]string     `json:"tags,omitempty"`
	NodeKeyFingerprint string        `json:"nodeKeyFingerprint,omitempty"`
	ControlURL         *string       `json:"controlUrl,omitempty"`
	Code               string        `json:"code,omitempty"`
	Message            string        `json:"message,omitempty"`
	Peer               string        `json:"peer,omitempty"`
	LocalPort          int           `json:"localPort,omitempty"`
	Peers              *[]string     `json:"peers,omitempty"`
	Mappings           []PeerMapping `json:"mappings,omitempty"`
}

func Hello(sidecar, tailscale string) Event {
	return Event{V: Version, Event: "hello", Sidecar: sidecar, Tailscale: tailscale}
}

func State(state string) Event { return Event{V: Version, Event: "state", State: state} }
func BrowseToURL(url string) Event {
	return Event{V: Version, Event: "browse_to_url", URL: url}
}
func Exited() Event { return Event{V: Version, Event: "exited"} }
func Error(code, message string) Event {
	return Event{V: Version, Event: "error", Code: code, Message: message}
}

func ForwardHello(sidecar, tailscale string) Event {
	return Event{V: ForwardVersion, Event: "hello", Sidecar: sidecar, Tailscale: tailscale}
}

func ForwardState(state string) Event {
	return Event{V: ForwardVersion, Event: "state", State: state}
}

func ForwardError(code, message string) Event {
	return Event{V: ForwardVersion, Event: "error", Code: code, Message: message}
}

func ForwardExited() Event { return Event{V: ForwardVersion, Event: "exited"} }

func PeerMapped(peer string, localPort int) Event {
	return Event{V: ForwardVersion, Event: "peer-mapped", Peer: peer, LocalPort: localPort}
}

func PeerUnmapped(peer string) Event {
	return Event{V: ForwardVersion, Event: "peer-unmapped", Peer: peer}
}

// Writer serializes whole JSON lines even when node notifications and stdin
// requests are handled concurrently.
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

// ParseCommand rejects malformed, version-mismatched, unknown-op, missing,
// and extra fields. That keeps version 1 a closed wire contract.
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
	case "status", "down":
		if err := onlyKeys(raw, "v", "op"); err != nil {
			return Command{}, err
		}
		return Command{Op: op}, nil
	default:
		return Command{}, ProtocolError("unknown op")
	}
}

// ParseForwardCommand parses the closed protocol-v2 command set used only by
// `hyprial-tsnet forward`. Login's `serve` subcommand remains protocol v1.
func ParseForwardCommand(line []byte) (Command, error) {
	var raw map[string]json.RawMessage
	if err := decodeExact(line, &raw); err != nil {
		return Command{}, ProtocolError("request must be one JSON object")
	}
	var version int
	if value, ok := raw["v"]; !ok || json.Unmarshal(value, &version) != nil || version != ForwardVersion {
		return Command{}, ProtocolError("unsupported forwarding protocol version")
	}
	var op string
	if value, ok := raw["op"]; !ok || json.Unmarshal(value, &op) != nil {
		return Command{}, ProtocolError("op must be a string")
	}
	switch op {
	case "up":
		return parseForwardUp(line, raw)
	case "map-peer", "unmap-peer":
		if err := onlyKeys(raw, "v", "op", "peer"); err != nil {
			return Command{}, err
		}
		var wire struct {
			V    int    `json:"v"`
			Op   string `json:"op"`
			Peer string `json:"peer"`
		}
		if err := decodeExact(line, &wire); err != nil || wire.Peer == "" {
			return Command{}, ProtocolError("peer must be a non-empty string")
		}
		return Command{Op: op, Peer: wire.Peer}, nil
	case "status", "down":
		if err := onlyKeys(raw, "v", "op"); err != nil {
			return Command{}, err
		}
		return Command{Op: op}, nil
	default:
		return Command{}, ProtocolError("unknown forwarding op")
	}
}

type upWire struct {
	V              int             `json:"v"`
	Op             string          `json:"op"`
	ControlURL     string          `json:"controlUrl"`
	Hostname       string          `json:"hostname"`
	Dir            string          `json:"dir"`
	Ephemeral      bool            `json:"ephemeral"`
	Join           string          `json:"join"`
	AuthKey        json.RawMessage `json:"authKey"`
	Proxy          string          `json:"proxy,omitempty"`
	TimeoutSeconds *int            `json:"timeoutSeconds,omitempty"`
}

func parseUp(line []byte, raw map[string]json.RawMessage) (Command, error) {
	if err := onlyKeys(raw, "v", "op", "controlUrl", "hostname", "dir", "ephemeral", "join", "authKey", "proxy", "timeoutSeconds"); err != nil {
		return Command{}, err
	}
	for _, key := range []string{"controlUrl", "hostname", "dir", "ephemeral", "join", "authKey"} {
		if _, ok := raw[key]; !ok {
			return Command{}, ProtocolError("missing " + key)
		}
	}
	var wire upWire
	if err := decodeExact(line, &wire); err != nil {
		return Command{}, ProtocolError("invalid up fields")
	}
	if wire.Hostname == "" {
		return Command{}, ProtocolError("hostname must not be empty")
	}
	if wire.Dir == "" {
		return Command{}, ProtocolError("dir must not be empty")
	}
	if wire.Join != "interactive" && wire.Join != "preauthkey" {
		return Command{}, ProtocolError("join must be interactive or preauthkey")
	}
	if wire.Proxy != "" && !(bytes.HasPrefix([]byte(wire.Proxy), []byte("http://")) || bytes.HasPrefix([]byte(wire.Proxy), []byte("https://"))) {
		return Command{}, ProtocolError("proxy must be an http(s) URL")
	}
	if wire.TimeoutSeconds != nil && *wire.TimeoutSeconds <= 0 {
		return Command{}, ProtocolError("timeoutSeconds must be positive")
	}
	var authKey *string
	if !bytes.Equal(bytes.TrimSpace(wire.AuthKey), []byte("null")) {
		var value string
		if err := json.Unmarshal(wire.AuthKey, &value); err != nil {
			return Command{}, ProtocolError("authKey must be null or a string")
		}
		authKey = &value
	}
	return Command{Op: "up", Up: &UpRequest{
		Version:    Version,
		ControlURL: wire.ControlURL, Hostname: wire.Hostname, Dir: wire.Dir,
		Ephemeral: wire.Ephemeral, Join: wire.Join, AuthKey: authKey, Proxy: wire.Proxy,
		TimeoutSeconds: wire.TimeoutSeconds,
	}}, nil
}

type forwardUpWire struct {
	V              int             `json:"v"`
	Op             string          `json:"op"`
	ControlURL     string          `json:"controlUrl"`
	Hostname       string          `json:"hostname"`
	Dir            string          `json:"dir"`
	Ephemeral      bool            `json:"ephemeral"`
	Join           string          `json:"join"`
	AuthKey        json.RawMessage `json:"authKey"`
	Proxy          string          `json:"proxy,omitempty"`
	TimeoutSeconds *int            `json:"timeoutSeconds,omitempty"`
	Resume         bool            `json:"resume"`
	InboundTarget  string          `json:"inboundTarget"`
	PeerPort       int             `json:"peerPort"`
}

func parseForwardUp(line []byte, raw map[string]json.RawMessage) (Command, error) {
	if err := onlyKeys(
		raw, "v", "op", "controlUrl", "hostname", "dir", "ephemeral", "join",
		"authKey", "proxy", "timeoutSeconds", "resume", "inboundTarget", "peerPort",
	); err != nil {
		return Command{}, err
	}
	for _, key := range []string{
		"controlUrl", "hostname", "dir", "ephemeral", "join", "authKey",
		"resume", "inboundTarget", "peerPort",
	} {
		if _, ok := raw[key]; !ok {
			return Command{}, ProtocolError("missing " + key)
		}
	}
	var wire forwardUpWire
	if err := decodeExact(line, &wire); err != nil {
		return Command{}, ProtocolError("invalid forwarding up fields")
	}
	if wire.Hostname == "" || wire.Dir == "" || wire.InboundTarget == "" {
		return Command{}, ProtocolError("hostname, dir, and inboundTarget must not be empty")
	}
	if wire.Join != "interactive" && wire.Join != "preauthkey" {
		return Command{}, ProtocolError("join must be interactive or preauthkey")
	}
	if wire.PeerPort <= 0 || wire.PeerPort > 65535 {
		return Command{}, ProtocolError("peerPort must be a valid TCP port")
	}
	if wire.Proxy != "" && !(bytes.HasPrefix([]byte(wire.Proxy), []byte("http://")) || bytes.HasPrefix([]byte(wire.Proxy), []byte("https://"))) {
		return Command{}, ProtocolError("proxy must be an http(s) URL")
	}
	if wire.TimeoutSeconds != nil && *wire.TimeoutSeconds <= 0 {
		return Command{}, ProtocolError("timeoutSeconds must be positive")
	}
	var authKey *string
	if !bytes.Equal(bytes.TrimSpace(wire.AuthKey), []byte("null")) {
		var value string
		if err := json.Unmarshal(wire.AuthKey, &value); err != nil {
			return Command{}, ProtocolError("authKey must be null or a string")
		}
		authKey = &value
	}
	return Command{Op: "up", Up: &UpRequest{
		Version:    ForwardVersion,
		ControlURL: wire.ControlURL, Hostname: wire.Hostname, Dir: wire.Dir,
		Ephemeral: wire.Ephemeral, Join: wire.Join, AuthKey: authKey, Proxy: wire.Proxy,
		TimeoutSeconds: wire.TimeoutSeconds, Resume: wire.Resume,
		InboundTarget: wire.InboundTarget, PeerPort: wire.PeerPort,
	}}, nil
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
