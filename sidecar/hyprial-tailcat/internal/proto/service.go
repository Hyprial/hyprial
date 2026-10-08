package proto

import (
	"bytes"
	"encoding/json"
	"math"
	"regexp"
	"strings"
)

var serviceIdentifier = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)
var nodePublic = regexp.MustCompile(`^nodekey:[0-9a-f]{64}$`)
var privateAddressText = regexp.MustCompile(`(?:^|[^a-zA-Z0-9])tc[A-Za-z0-9_-]{8,}`)

var serviceFailureCodes = map[string]struct{}{
	CodeBadRequest: {}, CodeNotUp: {}, CodeAlreadyUp: {},
	CodeKeyFileInvalid: {}, CodeStartFailed: {}, CodePeerUnknown: {},
	CodeDialFailed: {}, CodePortInUse: {}, CodeInternal: {},
}

// ServiceMapping is the complete non-secret sidecar observation. Nullable
// fields deliberately have no omitempty tag: every successful mapping frame
// carries the same closed field set.
type ServiceMapping struct {
	Name              string   `json:"name"`
	DeviceID          string   `json:"deviceId"`
	RemotePort        int      `json:"remotePort"`
	RecordGeneration  int      `json:"recordGeneration"`
	LocalPort         int      `json:"localPort"`
	ActiveConnections int      `json:"activeConnections"`
	Path              string   `json:"path"`
	PathDetail        *string  `json:"pathDetail"`
	ObservedAtMs      *int64   `json:"observedAtMs"`
	LastDialState     string   `json:"lastDialState"`
	LastDialMs        *float64 `json:"lastDialMs"`
	LastError         *string  `json:"lastError"`
	LastErrorAtMs     *int64   `json:"lastErrorAtMs"`
}

func ServiceMapped(requestID string, service ServiceMapping) Event {
	return Event{V: Version, Event: "service-mapped", RequestID: requestID, Service: &service}
}

func ServiceUnmapped(requestID, name string, removed bool) Event {
	return Event{
		V: Version, Event: "service-unmapped", RequestID: requestID,
		Name: name, Removed: &removed,
	}
}

func ServiceStatus(requestID string, services []ServiceMapping) Event {
	if services == nil {
		services = []ServiceMapping{}
	}
	return Event{V: Version, Event: "service-status", RequestID: requestID, Services: &services}
}

func ServiceFailed(requestID, op string, name *string, code, message string) Event {
	var encodedName any = (*string)(nil)
	if name != nil {
		encodedName = *name
	}
	return Event{
		V: Version, Event: "service-failed", RequestID: requestID,
		Op: op, Name: encodedName, Code: code, Message: message,
	}
}

type mapServiceWire struct {
	V                int    `json:"v"`
	Op               string `json:"op"`
	RequestID        string `json:"requestId"`
	Name             string `json:"name"`
	DeviceID         string `json:"deviceId"`
	Address          string `json:"address"`
	ServerPublic     string `json:"serverPublic"`
	RemotePort       int    `json:"remotePort"`
	RecordGeneration int    `json:"recordGeneration"`
	LocalPort        int    `json:"localPort"`
}

func parseMapService(line []byte, raw map[string]json.RawMessage) (Command, error) {
	fields := []string{
		"v", "op", "requestId", "name", "deviceId", "address",
		"serverPublic", "remotePort", "recordGeneration", "localPort",
	}
	if err := requireExactKeys(raw, fields...); err != nil {
		return Command{}, err
	}
	var wire mapServiceWire
	if err := decodeExact(line, &wire); err != nil {
		return Command{}, ProtocolError("invalid map-service fields")
	}
	if err := validateServiceRequest(wire.RequestID, wire.Name); err != nil {
		return Command{}, err
	}
	if !serviceIdentifier.MatchString(wire.DeviceID) {
		return Command{}, ProtocolError("deviceId is invalid")
	}
	if !strings.HasPrefix(wire.Address, "tc") {
		return Command{}, ProtocolError("address must be a tailcat address")
	}
	if !nodePublic.MatchString(wire.ServerPublic) {
		return Command{}, ProtocolError("serverPublic must be a nodekey")
	}
	if wire.RemotePort < 1 || wire.RemotePort > 65535 {
		return Command{}, ProtocolError("remotePort must be a valid TCP port")
	}
	if wire.RecordGeneration < 1 {
		return Command{}, ProtocolError("recordGeneration must be positive")
	}
	if wire.LocalPort != 0 && (wire.LocalPort < 1024 || wire.LocalPort > 65535) {
		return Command{}, ProtocolError("localPort must be zero or an unprivileged TCP port")
	}
	return Command{
		Op: wire.Op, RequestID: wire.RequestID, Name: wire.Name,
		DeviceID: wire.DeviceID, Address: wire.Address,
		ServerPublic: wire.ServerPublic, RemotePort: wire.RemotePort,
		RecordGeneration: wire.RecordGeneration, LocalPort: wire.LocalPort,
	}, nil
}

func parseUnmapService(line []byte, raw map[string]json.RawMessage) (Command, error) {
	if err := requireExactKeys(raw, "v", "op", "requestId", "name"); err != nil {
		return Command{}, err
	}
	var wire struct {
		V         int    `json:"v"`
		Op        string `json:"op"`
		RequestID string `json:"requestId"`
		Name      string `json:"name"`
	}
	if err := decodeExact(line, &wire); err != nil {
		return Command{}, ProtocolError("invalid unmap-service fields")
	}
	if err := validateServiceRequest(wire.RequestID, wire.Name); err != nil {
		return Command{}, err
	}
	return Command{Op: wire.Op, RequestID: wire.RequestID, Name: wire.Name}, nil
}

func parseServiceStatus(line []byte, raw map[string]json.RawMessage) (Command, error) {
	if err := requireExactKeys(raw, "v", "op", "requestId"); err != nil {
		return Command{}, err
	}
	var wire struct {
		V         int    `json:"v"`
		Op        string `json:"op"`
		RequestID string `json:"requestId"`
	}
	if err := decodeExact(line, &wire); err != nil || wire.RequestID == "" || len(wire.RequestID) > MaxRequestIDLength {
		return Command{}, ProtocolError("requestId must be a non-empty string")
	}
	return Command{Op: wire.Op, RequestID: wire.RequestID}, nil
}

func validateServiceRequest(requestID, name string) error {
	if requestID == "" || len(requestID) > MaxRequestIDLength {
		return ProtocolError("requestId must be a non-empty string")
	}
	if !serviceIdentifier.MatchString(name) {
		return ProtocolError("name is invalid")
	}
	return nil
}

func requireExactKeys(raw map[string]json.RawMessage, fields ...string) error {
	if err := onlyKeys(raw, fields...); err != nil {
		return err
	}
	for _, field := range fields {
		if _, ok := raw[field]; !ok {
			return ProtocolError("missing " + field)
		}
	}
	return nil
}

var mappingFields = []string{
	"name", "deviceId", "remotePort", "recordGeneration", "localPort",
	"activeConnections", "path", "pathDetail", "observedAtMs",
	"lastDialState", "lastDialMs", "lastError", "lastErrorAtMs",
}

func validateServiceEvent(raw map[string]json.RawMessage, event Event) error {
	if event.RequestID == "" || len(event.RequestID) > MaxRequestIDLength {
		return ProtocolError("service event requestId is invalid")
	}
	switch event.Event {
	case "service-mapped":
		return validateMappingRaw(raw["service"], event.Service)
	case "service-status":
		if bytes.Equal(bytes.TrimSpace(raw["services"]), []byte("null")) || event.Services == nil {
			return ProtocolError("services must be an array")
		}
		var items []json.RawMessage
		if err := json.Unmarshal(raw["services"], &items); err != nil {
			return ProtocolError("services must be an array")
		}
		for i := range items {
			if err := validateMappingRaw(items[i], &(*event.Services)[i]); err != nil {
				return err
			}
		}
	case "service-unmapped":
		name, ok := event.Name.(string)
		if !ok || !serviceIdentifier.MatchString(name) || event.Removed == nil {
			return ProtocolError("invalid service-unmapped fields")
		}
	case "service-failed":
		if _, ok := serviceFailureCodes[event.Code]; !ok || event.Message == "" ||
			privateAddressText.MatchString(event.Message) {
			return ProtocolError("invalid service failure")
		}
		if event.Op != "map-service" && event.Op != "unmap-service" && event.Op != "service-status" {
			return ProtocolError("invalid failed operation")
		}
		nameRaw := bytes.TrimSpace(raw["name"])
		if event.Op == "service-status" {
			if !bytes.Equal(nameRaw, []byte("null")) {
				return ProtocolError("service-status failure name must be null")
			}
		} else {
			var name string
			if err := json.Unmarshal(nameRaw, &name); err != nil || !serviceIdentifier.MatchString(name) {
				return ProtocolError("service failure name is invalid")
			}
		}
	}
	return nil
}

func validateMappingRaw(raw json.RawMessage, mapping *ServiceMapping) error {
	if mapping == nil {
		return ProtocolError("service mapping must be an object")
	}
	var fields map[string]json.RawMessage
	if err := decodeExact(raw, &fields); err != nil {
		return ProtocolError("service mapping must be an object")
	}
	if err := requireExactKeys(fields, mappingFields...); err != nil {
		return err
	}
	if !serviceIdentifier.MatchString(mapping.Name) || !serviceIdentifier.MatchString(mapping.DeviceID) ||
		mapping.RemotePort < 1 || mapping.RemotePort > 65535 || mapping.RecordGeneration < 1 ||
		mapping.LocalPort < 1024 || mapping.LocalPort > 65535 || mapping.ActiveConnections < 0 {
		return ProtocolError("invalid service mapping identity")
	}
	if mapping.Path != "direct" && mapping.Path != "relay" && mapping.Path != "unknown" {
		return ProtocolError("invalid service path")
	}
	if mapping.LastDialState != "unknown" && mapping.LastDialState != "ok" && mapping.LastDialState != "error" {
		return ProtocolError("invalid service dial state")
	}
	if mapping.ObservedAtMs != nil && *mapping.ObservedAtMs < 0 ||
		mapping.LastErrorAtMs != nil && *mapping.LastErrorAtMs < 0 ||
		mapping.LastDialMs != nil && (*mapping.LastDialMs < 0 || math.IsNaN(*mapping.LastDialMs) || math.IsInf(*mapping.LastDialMs, 0)) {
		return ProtocolError("invalid service observation number")
	}
	for _, value := range []*string{mapping.PathDetail, mapping.LastError} {
		if value != nil && privateAddressText.MatchString(*value) {
			return ProtocolError("service observation contains private address material")
		}
	}
	return nil
}
