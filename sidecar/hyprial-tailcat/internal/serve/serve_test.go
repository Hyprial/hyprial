package serve

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/exposuregate"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/keys"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/labrelay"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/proto"
)

type session struct {
	cmdW   *io.PipeWriter
	events chan proto.Event
	raw    chan string
	diag   *bytes.Buffer
	done   chan error
}

func startSession(t *testing.T) *session {
	t.Helper()
	cmdR, cmdW := io.Pipe()
	eventR, eventW := io.Pipe()
	s := &session{cmdW: cmdW, events: make(chan proto.Event, 128), raw: make(chan string, 128), diag: &bytes.Buffer{}, done: make(chan error, 1)}
	go func() { s.done <- Run(context.Background(), cmdR, eventW, s.diag); eventW.Close() }()
	go func() {
		defer close(s.events)
		scanner := bufio.NewScanner(eventR)
		scanner.Buffer(make([]byte, 4096), 2*1024*1024)
		for scanner.Scan() {
			line := scanner.Text()
			s.raw <- line
			e, err := proto.ParseEvent([]byte(line))
			if err != nil {
				t.Errorf("unparseable event: %v", err)
				continue
			}
			s.events <- e
		}
	}()
	t.Cleanup(func() { cmdW.Close(); eventR.Close() })
	return s
}
func (s *session) send(t *testing.T, line string) {
	t.Helper()
	if _, err := io.WriteString(s.cmdW, line+"\n"); err != nil {
		t.Fatal(err)
	}
}
func (s *session) expect(t *testing.T, name string) proto.Event {
	t.Helper()
	select {
	case e := <-s.events:
		if e.Event != name {
			t.Fatalf("event %s want %s: code %s", e.Event, name, e.Code)
		}
		return e
	case <-time.After(45 * time.Second):
		t.Fatal("event timeout")
	}
	return proto.Event{}
}
func (s *session) expectError(t *testing.T, code string) {
	t.Helper()
	if e := s.expect(t, "error"); e.Code != code {
		t.Fatalf("code %s want %s", e.Code, code)
	}
}
func (s *session) finish(t *testing.T) {
	t.Helper()
	s.expect(t, "exited")
	select {
	case err := <-s.done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("shutdown timeout")
	}
}
func upFields(dir, keyFile string) map[string]any {
	_ = os.Chmod(dir, 0700)
	return map[string]any{"v": 7, "op": "up", "requestId": "up", "keyFile": keyFile, "addressFile": filepath.Join(dir, "dev.addr"), "membershipStore": filepath.Join(dir, "tailnet", "membership.json"), "owner": "alice", "deviceId": "device", "inboundTarget": "unix:" + filepath.Join(dir, "zenoh.sock")}
}
func sendMap(t *testing.T, s *session, p map[string]any) {
	t.Helper()
	b, _ := json.Marshal(p)
	s.send(t, string(b))
}
func TestLifecycle(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	keyFile := filepath.Join(dir, "dev.key")
	if _, err := keys.Generate(keyFile, false); err != nil {
		t.Fatal(err)
	}
	s := startSession(t)
	hello := s.expect(t, "hello")
	if hello.Sidecar == "" || hello.Tailcat == "" || len(hello.Capabilities) != 14 {
		t.Fatal("bad hello")
	}
	derp := relay.Config()
	up := upFields(dir, keyFile)
	up["derp"] = map[string]any{"host": derp.Host, "derpPort": derp.DERPPort, "stunPort": derp.STUNPort, "insecureForTests": true}
	sendMap(t, s, up)
	s.expect(t, "state")
	running := s.expect(t, "state")
	if running.State != "Running" || running.RequestID != "up" || running.Readiness == nil || !running.Readiness.Sidecar || !running.Readiness.Control || running.Readiness.Member {
		t.Fatal("incorrect staged readiness")
	}
	if _, err := os.Stat(filepath.Join(dir, "zenoh.sock")); !os.IsNotExist(err) {
		t.Fatal("startup required/created Zenoh backend")
	}
	if !strings.HasPrefix(running.ServerPublic, "nodekey:") || !strings.HasPrefix(running.ClientPublic, "nodekey:") || running.Peers == nil || len(*running.Peers) != 0 || running.DERPMapSource != "override" {
		t.Fatal("bad running projection")
	}
	sendMap(t, s, map[string]any{"v": 7, "op": "derive-client-key", "requestId": "derive-1", "serverPublic": running.ServerPublic})
	derived := s.expect(t, "client-key-derived")
	if derived.RequestID != "derive-1" || derived.ServerPublic != running.ServerPublic || derived.ClientPublic == running.ClientPublic {
		t.Fatal("bad derived key")
	}
	s.send(t, `{"v":7,"op":"status"}`)
	if status := s.expect(t, "state"); status.State != "Running" || status.ConnectionTelemetry == nil {
		t.Fatal("bad status")
	}
	s.send(t, `{"v":7,"op":"membership-status","requestId":"members","params":{}}`)
	if e := s.expect(t, "membership-result"); e.RequestID != "members" {
		t.Fatal("lost correlation")
	}
	s.send(t, `{"v":7,"op":"down"}`)
	s.finish(t)
	address, err := os.ReadFile(filepath.Join(dir, "dev.addr"))
	if err != nil {
		t.Fatal(err)
	}
	info, _ := os.Stat(filepath.Join(dir, "dev.addr"))
	if info.Mode().Perm() != 0600 {
		t.Fatal("address not private")
	}
	secrets := []string{"privkey:", "psk:", strings.TrimSpace(string(address))}
	for _, secret := range secrets {
		if strings.Contains(s.diag.String(), secret) {
			t.Fatal("diagnostic leak")
		}
	}
	for {
		select {
		case raw := <-s.raw:
			for _, secret := range secrets {
				if strings.Contains(raw, secret) {
					t.Fatal("event leak")
				}
			}
		default:
			return
		}
	}
}
func TestBadRequests(t *testing.T) {
	s := startSession(t)
	s.expect(t, "hello")
	for _, line := range []string{`{"v":7,"op":"unknown"}`, `{"v":7,"op":"status","mystery":1}`, `not json`} {
		s.send(t, line)
		s.expectError(t, proto.CodeBadRequest)
	}
	for _, line := range []string{`{"v":2,"op":"status"}`, `{"v":3,"op":"status"}`, `{"v":5,"op":"status"}`, `{"v":6,"op":"status"}`} {
		s.send(t, line)
		s.expectError(t, "SIDECAR_PROTOCOL_MISMATCH")
	}
	s.send(t, `{"v":7,"op":"status"}`)
	if s.expect(t, "state").State != "Down" {
		t.Fatal("not down")
	}
	s.cmdW.Close()
	s.finish(t)
}
func TestParseErrorEchoesOnlyValidRequestID(t *testing.T) {
	s := startSession(t)
	s.expect(t, "hello")
	for _, op := range []string{"allow", "map-service", "member-revoke"} {
		sendMap(t, s, map[string]any{"v": 7, "op": op, "requestId": "bad"})
		e := s.expect(t, "error")
		if e.RequestID != "bad" || e.Code != proto.CodeBadRequest {
			t.Fatal("lost error correlation")
		}
	}
	sendMap(t, s, map[string]any{"v": 7, "op": "allow", "requestId": strings.Repeat("x", 129)})
	if s.expect(t, "error").RequestID != "" {
		t.Fatal("oversized correlation echoed")
	}
	s.send(t, `{"v":7,"op":"down"}`)
	s.finish(t)
}
func TestNotUpErrors(t *testing.T) {
	s := startSession(t)
	s.expect(t, "hello")
	for _, line := range []string{`{"v":7,"op":"map-peer","peer":"a","address":"tcPlaceholder","slot":"/a/s0"}`, `{"v":7,"op":"unmap-peer","peer":"a"}`, `{"v":7,"op":"expose","port":8447,"target":"tcp:127.0.0.1:80","proxyProtocol":"","admission":"allow"}`, `{"v":7,"op":"peer-key","addr":"[fd7a::1]:8447"}`, `{"v":7,"op":"membership-status","requestId":"r","params":{}}`} {
		s.send(t, line)
		s.expectError(t, proto.CodeNotUp)
	}
	for _, op := range []string{"unmap-service", "service-status"} {
		p := map[string]any{"v": 7, "op": op, "requestId": "r"}
		if op == "unmap-service" {
			p["name"] = "api"
		}
		sendMap(t, s, p)
		if e := s.expect(t, "service-failed"); e.RequestID != "r" || e.Code != proto.CodeNotUp {
			t.Fatal("bad service error")
		}
	}
	s.send(t, `{"v":7,"op":"map-peer","requestId":"map","peer":"a","address":"tcPlaceholder","slot":"/a/s0"}`)
	if e := s.expect(t, "error"); e.RequestID != "map" || e.Code != proto.CodeNotUp {
		t.Fatal("bad map error")
	}
	s.send(t, `{"v":7,"op":"down"}`)
	s.finish(t)
}
func TestKeyFileInvalid(t *testing.T) {
	dir := t.TempDir()
	s := startSession(t)
	s.expect(t, "hello")
	sendMap(t, s, upFields(dir, filepath.Join(dir, "absent.key")))
	s.expect(t, "state")
	s.expectError(t, proto.CodeKeyFileInvalid)
	s.send(t, `{"v":7,"op":"status"}`)
	if s.expect(t, "state").State != "Down" {
		t.Fatal("not down")
	}
	s.send(t, `{"v":7,"op":"down"}`)
	s.finish(t)
}

func TestExposeKeysThroughServe(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	keyFile := filepath.Join(dir, "dev.key")
	if _, err := keys.Generate(keyFile, false); err != nil {
		t.Fatal(err)
	}
	s := startSession(t)
	s.expect(t, "hello")
	derp := relay.Config()
	up := upFields(dir, keyFile)
	up["v"] = 7
	up["derp"] = map[string]any{"host": derp.Host, "derpPort": derp.DERPPort, "stunPort": derp.STUNPort, "insecureForTests": true}
	sendMap(t, s, up)
	s.expect(t, "state")
	s.expect(t, "state")

	keyA := "nodekey:0000000000000000000000000000000000000000000000000000000000000001"
	for len(s.raw) > 0 {
		<-s.raw
	}
	sendMap(t, s, map[string]any{
		"v": 7, "op": "expose", "requestId": "exp-1",
		"port": 8447, "target": "tcp:127.0.0.1:8080", "proxyProtocol": "",
		"admission": "keys", "keys": []string{keyA},
	})
	exposed := s.expect(t, "exposed")
	if exposed.RequestID != "exp-1" || exposed.Admission == nil || *exposed.Admission != proto.AdmissionKeys || exposed.KeyCount == nil || *exposed.KeyCount != 1 {
		t.Fatalf("unexpected exposed event: %+v", exposed)
	}
	rawExposed := <-s.raw
	if strings.Contains(rawExposed, `"keys":`) || strings.Contains(rawExposed, keyA) {
		t.Fatalf("exposed event echoed keys: %s", rawExposed)
	}

	sendMap(t, s, map[string]any{
		"v": 7, "op": "expose", "requestId": "exp-2",
		"port": 8447, "target": "tcp:127.0.0.1:8080", "proxyProtocol": "",
		"admission": "keys", "keys": []string{},
	})
	replaced := s.expect(t, "exposed")
	if replaced.RequestID != "exp-2" || replaced.Admission == nil || *replaced.Admission != proto.AdmissionKeys || replaced.KeyCount == nil || *replaced.KeyCount != 0 {
		t.Fatalf("unexpected replaced exposed event: %+v", replaced)
	}
	<-s.raw

	sendMap(t, s, map[string]any{
		"v": 7, "op": "expose", "requestId": "exp-allow",
		"port": 8447, "target": "tcp:127.0.0.1:8080", "proxyProtocol": "",
		"admission": "allow",
	})
	allowed := s.expect(t, "exposed")
	if allowed.RequestID != "exp-allow" || allowed.Admission == nil || *allowed.Admission != proto.AdmissionAllow || allowed.KeyCount != nil {
		t.Fatalf("unexpected allow exposed event: %+v", allowed)
	}
	rawAllowed := <-s.raw
	if strings.Contains(rawAllowed, `"keyCount"`) {
		t.Fatalf("allow exposed event echoed keyCount: %s", rawAllowed)
	}

	sendMap(t, s, map[string]any{
		"v": 7, "op": "expose", "requestId": "exp-no-admission",
		"port": 8447, "target": "tcp:127.0.0.1:8080", "proxyProtocol": "",
	})
	missingAdmission := s.expect(t, "error")
	if missingAdmission.Code != proto.CodeBadRequest || missingAdmission.RequestID != "exp-no-admission" {
		t.Fatalf("missing admission error = %+v", missingAdmission)
	}

	sendMap(t, s, map[string]any{
		"v": 7, "op": "expose", "requestId": "exp-bad",
		"port": 8447, "target": "tcp:127.0.0.1:8080", "proxyProtocol": "",
		"admission": "keys",
	})
	bad := s.expect(t, "error")
	if bad.Code != proto.CodeBadRequest || bad.RequestID != "exp-bad" {
		t.Fatalf("bad expose error = %+v", bad)
	}

	overCap := make([]string, exposuregate.MaxKeys+1)
	for i := range overCap {
		overCap[i] = fmt.Sprintf("nodekey:%064x", i+1)
	}
	sendMap(t, s, map[string]any{
		"v": 7, "op": "expose", "requestId": "exp-over-cap",
		"port": 8447, "target": "tcp:127.0.0.1:8080", "proxyProtocol": "",
		"admission": "keys", "keys": overCap,
	})
	tooMany := s.expect(t, "error")
	if tooMany.Code != proto.CodeBadRequest || tooMany.RequestID != "exp-over-cap" {
		t.Fatalf("over-cap expose error = %+v", tooMany)
	}
	s.send(t, `{"v":7,"op":"status","requestId":"still-alive"}`)
	alive := s.expect(t, "state")
	if alive.RequestID != "still-alive" || alive.State != "Running" {
		t.Fatalf("sidecar did not stay alive after over-cap expose: %+v", alive)
	}

	s.send(t, `{"v":7,"op":"down"}`)
	s.finish(t)
}
