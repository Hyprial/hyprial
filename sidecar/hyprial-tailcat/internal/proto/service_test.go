package proto

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestServiceCommandsClosedSchema(t *testing.T) {
	valid := []struct {
		line string
		op   string
	}{
		{`{"v":6,"op":"map-service","requestId":"req-map","name":"api","deviceId":"sub2api","address":"tcPlaceholderAddressNotASecret","serverPublic":"nodekey:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","remotePort":8080,"recordGeneration":7,"localPort":0}`, "map-service"},
		{`{"v":6,"op":"unmap-service","requestId":"req-unmap","name":"api"}`, "unmap-service"},
		{`{"v":6,"op":"service-status","requestId":"req-status"}`, "service-status"},
	}
	for _, tc := range valid {
		command, err := ParseCommand([]byte(tc.line))
		if err != nil {
			t.Fatalf("ParseCommand(%s): %v", tc.op, err)
		}
		if command.Op != tc.op || command.RequestID == "" {
			t.Fatalf("bad command: %+v", command)
		}
	}

	invalid := []string{
		`{"v":6,"op":"map-service","requestId":"req","name":"api","deviceId":"sub2api","address":"tcX","serverPublic":"nodekey:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","remotePort":8080,"recordGeneration":1,"localPort":80}`,
		`{"v":6,"op":"map-service","requestId":"req","name":"API","deviceId":"sub2api","address":"tcX","serverPublic":"nodekey:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","remotePort":8080,"recordGeneration":1,"localPort":18080}`,
		`{"v":6,"op":"map-service","requestId":"req","name":"api","deviceId":"sub2api","address":"tcX","serverPublic":"nodekey:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","remotePort":true,"recordGeneration":1,"localPort":18080}`,
		`{"v":6,"op":"map-service","requestId":"req","name":"api","deviceId":"sub2api","address":"tcX","serverPublic":"nodekey:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","remotePort":8080,"recordGeneration":0,"localPort":18080}`,
		`{"v":6,"op":"unmap-service","requestId":"","name":"api"}`,
		`{"v":6,"op":"service-status","requestId":"req","extra":1}`,
	}
	for _, line := range invalid {
		if _, err := ParseCommand([]byte(line)); err == nil || !IsProtocolError(err) {
			t.Fatalf("accepted invalid service command %s: %v", line, err)
		}
	}
}

func TestServiceEventsRoundTripAndRequiredNulls(t *testing.T) {
	mapping := ServiceMapping{
		Name: "api", DeviceID: "sub2api", RemotePort: 8080,
		RecordGeneration: 7, LocalPort: 18080, ActiveConnections: 0,
		Path: "unknown", LastDialState: "unknown",
	}
	events := []Event{
		ServiceMapped("req-map", mapping),
		ServiceUnmapped("req-unmap", "api", true),
		ServiceStatus("req-status", []ServiceMapping{}),
		ServiceFailed("req-fail", "service-status", nil, CodeInternal, "safe failure"),
	}
	for _, event := range events {
		encoded, err := json.Marshal(event)
		if err != nil {
			t.Fatal(err)
		}
		parsed, err := ParseEvent(encoded)
		if err != nil {
			t.Fatalf("ParseEvent(%s): %v", encoded, err)
		}
		if parsed.RequestID != event.RequestID {
			t.Fatalf("request correlation changed: %+v", parsed)
		}
		if event.Event == "service-status" && !reflect.DeepEqual(*parsed.Services, []ServiceMapping{}) {
			t.Fatalf("services must be present and empty: %s", encoded)
		}
		if event.Event == "service-failed" {
			var raw map[string]any
			_ = json.Unmarshal(encoded, &raw)
			if _, present := raw["name"]; !present || raw["name"] != nil {
				t.Fatalf("status failure must carry name:null: %s", encoded)
			}
		}
	}
}

func TestServiceEventRejectsMalformedMapping(t *testing.T) {
	base := `{"v":6,"event":"service-mapped","requestId":"req","service":{"name":"api","deviceId":"sub2api","remotePort":8080,"recordGeneration":1,"localPort":18080,"activeConnections":0,"path":"unknown","pathDetail":null,"observedAtMs":null,"lastDialState":"unknown","lastDialMs":null,"lastError":null,"lastErrorAtMs":null}}`
	if _, err := ParseEvent([]byte(base)); err != nil {
		t.Fatalf("valid mapping rejected: %v", err)
	}
	invalid := []string{
		`{"v":6,"event":"service-mapped","requestId":"req","service":{"name":"api","deviceId":"sub2api","remotePort":8080,"recordGeneration":1,"localPort":80,"activeConnections":0,"path":"unknown","pathDetail":null,"observedAtMs":null,"lastDialState":"unknown","lastDialMs":null,"lastError":null,"lastErrorAtMs":null}}`,
		`{"v":6,"event":"service-status","requestId":"req","services":null}`,
		`{"v":6,"event":"service-failed","requestId":"req","op":"map-service","name":"api","code":"SECRET_CODE","message":"nope"}`,
		`{"v":6,"event":"service-failed","requestId":"req","op":"map-service","name":"api","code":"INTERNAL","message":"dial tcSecretAddressMaterial123456789 failed"}`,
	}
	for _, line := range invalid {
		if _, err := ParseEvent([]byte(line)); err == nil {
			t.Fatalf("accepted malformed service event: %s", line)
		}
	}
}
