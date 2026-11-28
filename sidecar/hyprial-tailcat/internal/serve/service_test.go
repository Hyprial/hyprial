package serve

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/keys"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/labrelay"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/proto"
)

func TestServiceWireCorrelationAndNonfatalFailure(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	if err := os.Chmod(dir, 0700); err != nil {
		t.Fatal(err)
	}
	keyFile := filepath.Join(dir, "service-wire.key")
	public, err := keys.Generate(keyFile, false)
	if err != nil {
		t.Fatal(err)
	}
	addressFile := filepath.Join(dir, "service-wire.addr")
	s := startSession(t)
	hello := s.expect(t, "hello")
	found := false
	for _, capability := range hello.Capabilities {
		found = found || capability == "service-connect-v1"
	}
	if !found {
		t.Fatalf("hello lacks service-connect-v1: %+v", hello)
	}
	derp := relay.Config()
	up, _ := json.Marshal(map[string]any{
		"v": proto.Version, "op": "up", "requestId": "up", "keyFile": keyFile,
		"membershipStore": filepath.Join(dir, "tailnet", "membership.json"), "owner": "alice", "deviceId": "device", "inboundTarget": "unix:" + filepath.Join(dir, "zenoh.sock"), "addressFile": addressFile,
		"derp": map[string]any{
			"host": derp.Host, "derpPort": derp.DERPPort,
			"stunPort": derp.STUNPort, "insecureForTests": derp.InsecureForTests,
		},
	})
	s.send(t, string(up))
	s.expect(t, "state")
	s.expect(t, "state")

	bad, _ := json.Marshal(map[string]any{
		"v": proto.Version, "op": "map-service", "requestId": "request-bad-pin",
		"name": "api", "deviceId": "service-a", "address": readFile(t, addressFile),
		"serverPublic": public.ClientPublic, "remotePort": 18086,
		"recordGeneration": 1, "localPort": 0,
	})
	s.send(t, string(bad))
	failure := s.expect(t, "service-failed")
	if failure.RequestID != "request-bad-pin" || failure.Op != "map-service" ||
		failure.Code != proto.CodeBadRequest || strings.Contains(failure.Message, readFile(t, addressFile)) {
		t.Fatalf("bad correlated safe failure: %+v", failure)
	}

	// A service failure is ordinary and does not consume the forwarding
	// process: an existing peer command still succeeds afterward.
	s.send(t, `{"v":7,"op":"status"}`)
	if state := s.expect(t, "state"); state.State != "Running" {
		t.Fatalf("peer status after service failure = %+v", state)
	}
	s.send(t, `{"v":7,"op":"service-status","requestId":"request-status"}`)
	status := s.expect(t, "service-status")
	if status.RequestID != "request-status" || status.Services == nil || len(*status.Services) != 0 {
		t.Fatalf("bad status after failure: %+v", status)
	}
	s.send(t, `{"v":7,"op":"down"}`)
	s.finish(t)
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	value, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(string(value))
}
