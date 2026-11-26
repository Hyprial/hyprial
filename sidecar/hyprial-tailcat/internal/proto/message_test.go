package proto

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func upFixture() map[string]any {
	return map[string]any{"v": 7, "op": "up", "requestId": "up", "keyFile": "/a/dev.key", "addressFile": "/a/dev.addr", "membershipStore": "/a/membership.json", "owner": "alice", "deviceId": "laptop", "inboundTarget": "unix:/a/zenoh.sock"}
}
func TestParseUpV7(t *testing.T) {
	cases := []map[string]any{{}, {"inboundTarget": "tcp:127.0.0.1:7443"}, {"slots": []string{"/a/s0", "/a/s1"}, "slotHoldMs": 10000}, {"slots": []string{"tcp:127.0.0.1:0", "tcp:127.0.0.1:0"}}, {"derp": map[string]any{"host": "127.0.0.1", "derpPort": 3340, "stunPort": 3341, "insecureForTests": true}}, {"derpMapURL": "https://derpmap.hyprial.ai/map.json", "derpMapCacheDir": "/a/cache"}}
	for _, extra := range cases {
		p := upFixture()
		for k, v := range extra {
			p[k] = v
		}
		b, _ := json.Marshal(p)
		c, err := ParseCommand(b)
		if err != nil {
			t.Fatal(err)
		}
		if c.RequestID != "up" || c.Up.ControlPort != 7448 || c.Up.PeerPort != 7447 || c.Up.MembershipStore != "/a/membership.json" {
			t.Fatal("lost up fields")
		}
	}
	invalid := []map[string]any{{"allow": []string{}}, {"allowAny": false}, {"keyFile": "relative"}, {"addressFile": "relative"}, {"membershipStore": "relative"}, {"owner": ""}, {"deviceId": ""}, {"requestId": ""}, {"requestId": strings.Repeat("r", 129)}, {"inboundTarget": "127.0.0.1:7443"}, {"inboundTarget": "tcp:0.0.0.0:7443"}, {"inboundTarget": "tcp:192.168.1.2:7443"}, {"inboundTarget": "tcp:127.0.0.2:7443"}, {"inboundTarget": "tcp:[::1]:7443"}, {"inboundTarget": "tcp:localhost:7443"}, {"inboundTarget": "tcp:127.0.0.1:0"}, {"inboundTarget": "tcp:127.0.0.1:70000"}, {"inboundTarget": "tcp/127.0.0.1:7443"}, {"inboundTarget": "unix:relative"}, {"peerPort": 70000}, {"controlPort": 7447}, {"controlPort": 70000}, {"derp": map[string]any{"host": "127.0.0.1"}}, {"derpMapURL": "http://bad"}, {"slots": []string{"relative"}}, {"slots": []string{"/a/s0", "/a/s0"}}, {"slots": []string{"tcp:127.0.0.1:51230", "tcp:127.0.0.1:51230"}}, {"slots": []string{"tcp:0.0.0.0:0"}}, {"slots": []string{"/a/s0"}, "slotHoldMs": -1}, {"slotHoldMs": 1000}}
	for _, extra := range invalid {
		p := upFixture()
		for k, v := range extra {
			p[k] = v
		}
		b, _ := json.Marshal(p)
		if _, err := ParseCommand(b); err == nil {
			t.Fatalf("accepted invalid up field %v", extra)
		}
	}
	for _, field := range []string{"keyFile", "addressFile", "membershipStore", "owner", "deviceId", "requestId", "inboundTarget"} {
		p := upFixture()
		delete(p, field)
		b, _ := json.Marshal(p)
		if _, err := ParseCommand(b); err == nil {
			t.Fatal("accepted missing", field)
		}
	}
}
func TestParseCommandPositive(t *testing.T) {
	dir := t.TempDir()
	if err := os.Chmod(dir, 0700); err != nil {
		t.Fatal(err)
	}
	target, err := json.Marshal("unix:" + filepath.Join(dir, "backend"))
	if err != nil {
		t.Fatal(err)
	}
	lines := []string{
		`{"v":7,"op":"map-peer","peer":"a","address":"tcPlaceholder","slot":"/a/slot-00"}`,
		`{"v":7,"op":"map-peer","peer":"a","address":"tcPlaceholder","slot":"tcp:127.0.0.1:0","port":8447}`,
		`{"v":7,"op":"unmap-peer","peer":"a","requestId":"r"}`,
		`{"v":7,"op":"expose","port":8447,"target":"tcp:127.0.0.1:8080","proxyProtocol":""}`,
		`{"v":7,"op":"expose","port":8447,"target":` + string(target) + `,"proxyProtocol":"v2","admission":"allow"}`,
		`{"v":7,"op":"unexpose","port":8447}`,
		`{"v":7,"op":"peer-key","addr":"[fd7a::1]:8447"}`,
		`{"v":7,"op":"derive-client-key","requestId":"r","serverPublic":"nodekey:abc"}`,
		`{"v":7,"op":"status"}`, `{"v":7,"op":"down"}`,
	}
	for _, line := range lines {
		c, err := ParseCommand([]byte(line))
		if err != nil {
			t.Fatal(err)
		}
		if c.Op == "map-peer" && (c.Peer != "a" || c.Slot == "" || c.Port == 0) {
			t.Fatal("lost map fields")
		}
		if c.Op == "expose" && c.Admission != AdmissionAllow {
			t.Fatal("open default")
		}
	}
}
func TestParseCommandNegative(t *testing.T) {
	lines := []string{`no json`, `{"op":"status"}`, `{"v":2,"op":"status"}`, `{"v":3,"op":"status"}`, `{"v":4,"op":"status"}`, `{"v":5,"op":"status"}`, `{"v":6,"op":"status"}`, `{"v":"7","op":"status"}`, `{"v":7}`, `{"v":7,"op":"unknown"}`, `{"v":7,"op":"status","extra":1}`, `{"v":7,"op":"status","peer":"a"}`, `{"v":7,"op":"map-peer","peer":"a"}`, `{"v":7,"op":"map-peer","peer":"a","address":"tcPlaceholder"}`, `{"v":7,"op":"map-peer","peer":"a","address":"tcPlaceholder","slot":"relative"}`, `{"v":7,"op":"map-peer","peer":"a","address":"tcPlaceholder","slot":"tcp:10.0.0.9:443"}`, `{"v":7,"op":"map-peer","peer":"","address":"tcPlaceholder","slot":"/a/s0"}`, `{"v":7,"op":"map-peer","peer":"a","address":"http://bad","slot":"/a/s0"}`, `{"v":7,"op":"unmap-peer","peer":"a","port":1}`, `{"v":7,"op":"allow","keys":[],"allowAny":false}`, `{"v":7,"op":"expose","port":1,"target":"tcp:127.0.0.1:80","proxyProtocol":"v1"}`, `{"v":7,"op":"expose","port":1,"target":"tcp:127.0.0.1:80","proxyProtocol":"","admission":"any"}`, `{"v":7,"op":"expose","port":1,"target":"tcp:192.168.1.1:80","proxyProtocol":""}`, `{"v":7,"op":"expose","port":1,"target":"unix:relative","proxyProtocol":""}`, `{"v":7,"op":"expose","port":1,"proxyProtocol":""}`, `{"v":7,"op":"unexpose","port":0}`, `{"v":7,"op":"peer-key","addr":"bad"}`, `{"v":7,"op":"status"} {"v":7}`}
	for _, line := range lines {
		if _, err := ParseCommand([]byte(line)); err == nil {
			t.Fatalf("accepted %s", line)
		}
	}
}
func TestParseExposeKeys(t *testing.T) {
	keyA := "nodekey:0000000000000000000000000000000000000000000000000000000000000001"
	keyB := "nodekey:0000000000000000000000000000000000000000000000000000000000000002"
	positive := map[string]struct {
		keys []string
		want int
	}{
		"one key":   {[]string{keyA}, 1},
		"two keys":  {[]string{keyA, keyB}, 2},
		"empty set": {[]string{}, 0},
	}
	for name, tc := range positive {
		body, err := json.Marshal(map[string]any{
			"v": 7, "op": "expose", "requestId": "r", "port": 8447,
			"target": "tcp:127.0.0.1:8080", "proxyProtocol": "",
			"admission": "keys", "keys": tc.keys,
		})
		if err != nil {
			t.Fatal(err)
		}
		command, err := ParseCommand(body)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if command.Admission != AdmissionKeys || len(command.Keys) != tc.want {
			t.Fatalf("%s: admission %q keys %v", name, command.Admission, command.Keys)
		}
		for i, want := range tc.keys {
			if command.Keys[i] != want {
				t.Fatalf("%s: key %d = %q, want %q", name, i, command.Keys[i], want)
			}
		}
	}
	negative := map[string]string{
		"keys mode without keys": `{"v":7,"op":"expose","requestId":"r","port":8447,"target":"tcp:127.0.0.1:8080","proxyProtocol":"","admission":"keys"}`,
		"keys mode with null":    `{"v":7,"op":"expose","requestId":"r","port":8447,"target":"tcp:127.0.0.1:8080","proxyProtocol":"","admission":"keys","keys":null}`,
		"keys on allow":          `{"v":7,"op":"expose","requestId":"r","port":8447,"target":"tcp:127.0.0.1:8080","proxyProtocol":"","admission":"allow","keys":[]}`,
		"keys on any":            `{"v":7,"op":"expose","requestId":"r","port":8447,"target":"tcp:127.0.0.1:8080","proxyProtocol":"","admission":"any","keys":[]}`,
		"keys without admission": `{"v":7,"op":"expose","requestId":"r","port":8447,"target":"tcp:127.0.0.1:8080","proxyProtocol":"","keys":[]}`,
		"keys not an array":      `{"v":7,"op":"expose","requestId":"r","port":8447,"target":"tcp:127.0.0.1:8080","proxyProtocol":"","admission":"keys","keys":"nodekey:abcd"}`,
		"key not a string":       `{"v":7,"op":"expose","requestId":"r","port":8447,"target":"tcp:127.0.0.1:8080","proxyProtocol":"","admission":"keys","keys":[1]}`,
		"null key in array":      `{"v":7,"op":"expose","requestId":"r","port":8447,"target":"tcp:127.0.0.1:8080","proxyProtocol":"","admission":"keys","keys":[null]}`,
		"key not a nodekey":      `{"v":7,"op":"expose","requestId":"r","port":8447,"target":"tcp:127.0.0.1:8080","proxyProtocol":"","admission":"keys","keys":["not-a-key"]}`,
		"key too short":          `{"v":7,"op":"expose","requestId":"r","port":8447,"target":"tcp:127.0.0.1:8080","proxyProtocol":"","admission":"keys","keys":["nodekey:abcd"]}`,
		"key not hex":            `{"v":7,"op":"expose","requestId":"r","port":8447,"target":"tcp:127.0.0.1:8080","proxyProtocol":"","admission":"keys","keys":["nodekey:zz0000000000000000000000000000000000000000000000000000000000000000"]}`,
		"zero key":               `{"v":7,"op":"expose","requestId":"r","port":8447,"target":"tcp:127.0.0.1:8080","proxyProtocol":"","admission":"keys","keys":["nodekey:0000000000000000000000000000000000000000000000000000000000000000"]}`,
		"one bad key":            `{"v":7,"op":"expose","requestId":"r","port":8447,"target":"tcp:127.0.0.1:8080","proxyProtocol":"","admission":"keys","keys":["` + keyA + `","nodekey:abcd"]}`,
	}
	for name, line := range negative {
		if _, err := ParseCommand([]byte(line)); err == nil {
			t.Fatalf("%s was accepted", name)
		}
	}
}

func TestEventsRoundTrip(t *testing.T) {
	events := []Event{Hello("0.1.0", "tailcat"), State("Starting"), RunningState("nodekey:a", "nodekey:b", nil, nil), RunningState("nodekey:a", "nodekey:b", []PeerMapping{{Peer: "p", Slot: "/a/s0"}}, []string{"/a/s0"}), SlotMapped("p", "/a/s0"), PeerUnmapped("p"), Exposed(8447, "unix:/a/backend", "v2", AdmissionAllow), Exposed(8448, "tcp:127.0.0.1:8080", "", AdmissionKeys), Unexposed(8447), PeerKey("[fd7a::1]:8447", true, "nodekey:a"), PeerKey("[fd7a::1]:8447", false, ""), Error(CodeBadRequest, "nope"), Exited()}
	for _, event := range events {
		b, _ := json.Marshal(event)
		parsed, err := ParseEvent(b)
		if err != nil || parsed.Event != event.Event {
			t.Fatal(err)
		}
		if event.Event == "state" && !strings.Contains(string(b), `"peers":`) {
			t.Fatal("missing peers")
		}
	}
}
func TestParseStateConnectionTelemetry(t *testing.T) {
	line := []byte(`{"v":7,"event":"state","state":"Running","peers":[],"connectionTelemetry":{"ringCapacity":16,"slots":[{"peer":"a","slot":"tcp:127.0.0.1:41001","mapPort":41001,"dialFailureCount":2,"lastDialError":"refused","closedCount":1,"errorCounts":{"eof":1},"recent":[{"peer":"a","slot":"tcp:127.0.0.1:41001","mapPort":41001,"bytesIn":17,"bytesOut":19,"durationMs":23,"closeSide":"peer","errors":{"in":"reset","out":"eof"}}]}]}}`)
	event, err := ParseEvent(line)
	if err != nil {
		t.Fatal(err)
	}
	slot := event.ConnectionTelemetry.Slots[0]
	if slot.DialFailureCount != 2 || slot.LastDialError != "refused" || slot.Recent[0].BytesOut != 19 {
		t.Fatal("lost telemetry")
	}
}
func TestParseEventNegative(t *testing.T) {
	for _, line := range []string{`{"v":5,"event":"state","state":"Down","peers":[]}`, `{"v":6,"event":"state","state":"Down","peers":[]}`, `{"v":7,"event":"unknown"}`, `{"v":7,"event":"exited","extra":1}`, `{"v":7,"event":"exposed","port":8447,"target":"tcp:127.0.0.1:8080","proxyProtocol":"","admission":"keys","keys":[]}`, `{"v":7,"event":"client-key-derived","serverPublic":"nodekey:a","clientPublic":"nodekey:b"}`} {
		if _, err := ParseEvent([]byte(line)); err == nil {
			t.Fatal("invalid event accepted")
		}
	}
}
