package derpidentity_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"tailscale.com/derp/derpserver"
	"tailscale.com/envknob"
	"tailscale.com/net/stun"
	"tailscale.com/types/key"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/engine"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/keys"
)

const (
	remotePort    = 18091
	gatedPort     = 18092
	probeWaves    = 12
	controlWaves  = 4
	streamRounds  = 1000
	probeTimeout  = 5 * time.Second
	streamTimeout = 8 * time.Second
)

type localRelay struct {
	derp      *derpserver.Server
	tls       *httptest.Server
	stun      *net.UDPConn
	logsMu    sync.Mutex
	logs      []string
	materials map[string]keys.Material

	monitorStarted time.Time
	monitorStop    chan struct{}
	monitorDone    chan struct{}
	duplicateMu    sync.Mutex
	firstDuplicate *duplicateSample
}

type duplicateSample struct {
	at    time.Time
	keys  int
	conns int
}

func startRelay(t *testing.T) *localRelay {
	t.Helper()
	envknob.SetenvForTest(t, "IN_TS_TEST", "true")
	envknob.SetenvForTest(t, "TS_DEBUG_ALWAYS_USE_DERP", "true")
	envknob.SetenvForTest(t, "DERP_DEBUG_LOGS", "true")

	r := &localRelay{
		materials:   make(map[string]keys.Material),
		monitorStop: make(chan struct{}), monitorDone: make(chan struct{}),
	}
	r.derp = derpserver.New(key.NewNode(), func(format string, args ...any) {
		line := fmt.Sprintf(format, args...)
		if !strings.Contains(line, "duplicate client") {
			return
		}
		r.logsMu.Lock()
		r.logs = append(r.logs, line)
		r.logsMu.Unlock()
	})
	r.tls = httptest.NewUnstartedServer(derpserver.Handler(r.derp))
	r.tls.StartTLS()
	udp, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.ParseIP("127.0.0.1")})
	if err != nil {
		t.Fatal(err)
	}
	r.stun = udp
	go func() {
		buf := make([]byte, 65536)
		for {
			n, src, err := udp.ReadFromUDPAddrPort(buf)
			if err != nil {
				return
			}
			if tx, err := stun.ParseBindingRequest(buf[:n]); err == nil {
				_, _ = udp.WriteToUDPAddrPort(stun.Response(tx, src), src)
			}
		}
	}()
	r.monitorStarted = time.Now()
	go r.monitorDuplicates()
	t.Cleanup(func() {
		close(r.monitorStop)
		<-r.monitorDone
		r.tls.Close()
		r.derp.Close()
		_ = r.stun.Close()
	})
	return r
}

func (r *localRelay) config() *engine.DERPOverride {
	return &engine.DERPOverride{
		Host:             "127.0.0.1",
		DERPPort:         r.tls.Listener.Addr().(*net.TCPAddr).Port,
		STUNPort:         r.stun.LocalAddr().(*net.UDPAddr).Port,
		InsecureForTests: true,
	}
}

func (r *localRelay) metrics(t *testing.T) map[string]any {
	t.Helper()
	var got map[string]any
	if err := json.Unmarshal([]byte(r.derp.ExpVar(false).String()), &got); err != nil {
		t.Fatalf("decode DERP metrics: %v", err)
	}
	return got
}

func (r *localRelay) duplicateCounts(t *testing.T) (keys, conns int) {
	t.Helper()
	metrics := r.metrics(t)
	keys = int(metrics["gauge_current_dup_client_keys"].(float64))
	conns = int(metrics["gauge_current_dup_client_conns"].(float64))
	r.recordDuplicate(keys, conns, time.Now())
	r.duplicateMu.Lock()
	defer r.duplicateMu.Unlock()
	if sample := r.firstDuplicate; sample != nil {
		return sample.keys, sample.conns
	}
	return keys, conns
}

func (r *localRelay) monitorDuplicates() {
	defer close(r.monitorDone)
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case at := <-ticker.C:
			var metrics map[string]any
			if json.Unmarshal([]byte(r.derp.ExpVar(false).String()), &metrics) != nil {
				continue
			}
			keys, keysOK := metrics["gauge_current_dup_client_keys"].(float64)
			conns, connsOK := metrics["gauge_current_dup_client_conns"].(float64)
			if keysOK && connsOK {
				r.recordDuplicate(int(keys), int(conns), at)
			}
		case <-r.monitorStop:
			return
		}
	}
}

func (r *localRelay) recordDuplicate(keys, conns int, at time.Time) {
	if keys == 0 && conns == 0 {
		return
	}
	r.duplicateMu.Lock()
	defer r.duplicateMu.Unlock()
	if r.firstDuplicate == nil {
		r.firstDuplicate = &duplicateSample{at: at, keys: keys, conns: conns}
	}
}

func (r *localRelay) diagnostics() string {
	req := httptest.NewRequest("GET", "/debug/clients/?all=1&sort=conn", nil)
	rec := httptest.NewRecorder()
	r.derp.ServeDebugClients(rec, req)
	r.logsMu.Lock()
	logs := strings.Join(r.logs, "\n")
	r.logsMu.Unlock()
	r.duplicateMu.Lock()
	first := "none"
	if sample := r.firstDuplicate; sample != nil {
		first = fmt.Sprintf("after=%s keys=%d conns=%d", sample.at.Sub(r.monitorStarted), sample.keys, sample.conns)
	}
	r.duplicateMu.Unlock()
	return "DERP metrics=" + r.derp.ExpVar(false).String() + "\nDERP clients=" + rec.Body.String() +
		"\nDERP first duplicate=" + first + "\nDERP logs=" + logs
}

type runningEngine struct {
	engine   *engine.Engine
	material keys.Material
	address  string
}

func startEngine(t *testing.T, relay *localRelay, name string, allow []string) runningEngine {
	t.Helper()
	dir := t.TempDir()
	keyFile := filepath.Join(dir, name+".key")
	addressFile := filepath.Join(dir, name+".addr")
	if _, err := keys.Generate(keyFile, false); err != nil {
		t.Fatal(err)
	}
	material, err := keys.Load(keyFile)
	if err != nil {
		t.Fatal(err)
	}
	derivedAllow := make([]string, 0, len(allow))
	for _, masterPublic := range allow {
		client, ok := relay.materials[masterPublic]
		if !ok {
			t.Fatalf("admission setup has no client material for %s", masterPublic)
		}
		derived, deriveErr := keys.DeriveClientPrivate(
			client.ClientPrivate, material.ServerPublic(),
		)
		if deriveErr != nil {
			t.Fatal(deriveErr)
		}
		derivedAllow = append(derivedAllow, derived.Public().String())
	}
	eng := &engine.Engine{}
	if _, _, err := eng.Up(context.Background(), engine.UpConfig{
		KeyFile: keyFile, Allow: derivedAllow, AddressFile: addressFile, DERP: relay.config(),
	}); err != nil {
		t.Fatal(err)
	}
	relay.materials[material.ClientPublic().String()] = material
	t.Cleanup(eng.Down)
	encoded, err := os.ReadFile(addressFile)
	if err != nil {
		t.Fatal(err)
	}
	return runningEngine{engine: eng, material: material, address: strings.TrimSpace(string(encoded))}
}

func echoTarget(t *testing.T) string {
	t.Helper()
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				_, _ = io.Copy(conn, conn)
			}()
		}
	}()
	return listener.Addr().String()
}

func expose(t *testing.T, server runningEngine, port int, target string) {
	t.Helper()
	if _, err := server.engine.Expose(context.Background(), engine.Exposure{
		Port: port, Target: "tcp:" + target,
	}); err != nil {
		t.Fatal(err)
	}
}

type gatedEcho struct {
	address     string
	arrived     chan struct{}
	release     chan struct{}
	arrivalOnce sync.Once
	releaseOnce sync.Once
}

func gatedEchoTarget(t *testing.T) *gatedEcho {
	t.Helper()
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	gate := &gatedEcho{
		address: listener.Addr().String(), arrived: make(chan struct{}), release: make(chan struct{}),
	}
	t.Cleanup(func() {
		gate.releaseOnce.Do(func() { close(gate.release) })
		_ = listener.Close()
	})
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				payload := make([]byte, len("held-a"))
				if _, err := io.ReadFull(conn, payload); err != nil {
					return
				}
				gate.arrivalOnce.Do(func() { close(gate.arrived) })
				<-gate.release
				_, _ = conn.Write(payload)
			}()
		}
	}()
	return gate
}

func forceServerAResponseAfterBActivity(portA, portB int, gate *gatedEcho) error {
	conn, err := net.DialTimeout("tcp4", fmt.Sprintf("127.0.0.1:%d", portA), probeTimeout)
	if err != nil {
		return err
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(probeTimeout))
	if _, err := io.WriteString(conn, "held-a"); err != nil {
		return err
	}
	select {
	case <-gate.arrived:
	case <-time.After(probeTimeout):
		return errors.New("server A did not receive held probe")
	}
	if err := roundTrip(portB, "activate-b"); err != nil {
		return fmt.Errorf("activate server B path: %w", err)
	}
	gate.releaseOnce.Do(func() { close(gate.release) })
	got := make([]byte, len("held-a"))
	if _, err := io.ReadFull(conn, got); err != nil {
		return fmt.Errorf("read server A response after B activity: %w", err)
	}
	if string(got) != "held-a" {
		return errors.New("held echo mismatch")
	}
	return nil
}

func mapServer(t *testing.T, client *engine.Engine, name string, server runningEngine) int {
	t.Helper()
	mapping, err := client.MapService(engine.ServiceRequest{
		Name: name, DeviceID: name, Address: server.address,
		ServerPublic: server.material.ServerPublic().String(), RemotePort: remotePort,
		RecordGeneration: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	return mapping.LocalPort
}

func roundTrip(port int, payload string) error {
	conn, err := net.DialTimeout("tcp4", fmt.Sprintf("127.0.0.1:%d", port), probeTimeout)
	if err != nil {
		return err
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(probeTimeout))
	if _, err := io.WriteString(conn, payload); err != nil {
		return err
	}
	got := make([]byte, len(payload))
	if _, err := io.ReadFull(conn, got); err != nil {
		return err
	}
	if string(got) != payload {
		return errors.New("echo mismatch")
	}
	return nil
}

func runAlternatingProbes(ports []int, waves int) []error {
	var failures []error
	for wave := range waves {
		for index, port := range ports {
			if err := roundTrip(port, fmt.Sprintf("wave-%d-side-%d", wave, index)); err != nil {
				failures = append(failures, err)
			}
		}
	}
	return failures
}

func streamRoundTrip(port int, side string) error {
	conn, err := net.DialTimeout("tcp4", fmt.Sprintf("127.0.0.1:%d", port), probeTimeout)
	if err != nil {
		return err
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(streamTimeout))
	for round := range streamRounds {
		payload := strings.Repeat(fmt.Sprintf("%s-%04d/", side, round), 32)
		if _, err := io.WriteString(conn, payload); err != nil {
			return err
		}
		got := make([]byte, len(payload))
		if _, err := io.ReadFull(conn, got); err != nil {
			return err
		}
		if string(got) != payload {
			return errors.New("stream echo mismatch")
		}
	}
	return nil
}

func runParallelStreams(ports []int) []error {
	var wg sync.WaitGroup
	errs := make(chan error, len(ports))
	for side, port := range ports {
		wg.Add(1)
		go func() {
			defer wg.Done()
			errs <- streamRoundTrip(port, fmt.Sprintf("side-%d", side))
		}()
	}
	wg.Wait()
	close(errs)
	var failures []error
	for err := range errs {
		if err != nil {
			failures = append(failures, err)
		}
	}
	return failures
}

func TestOneSidecarClientIdentityCanReachTwoServersThroughDERP(t *testing.T) {
	t.Run("distinct-client-identities-control", func(t *testing.T) {
		relay := startRelay(t)
		clientA := startEngine(t, relay, "client-a", nil)
		clientB := startEngine(t, relay, "client-b", nil)
		serverA := startEngine(t, relay, "server-a", []string{clientA.material.ClientPublic().String()})
		serverB := startEngine(t, relay, "server-b", []string{clientB.material.ClientPublic().String()})
		expose(t, serverA, remotePort, echoTarget(t))
		expose(t, serverB, remotePort, echoTarget(t))
		ports := []int{
			mapServer(t, clientA.engine, "server-a", serverA),
			mapServer(t, clientB.engine, "server-b", serverB),
		}
		if failures := runAlternatingProbes(ports, probeWaves); len(failures) != 0 {
			t.Fatalf("distinct identities lost %d/%d alternating probes: %v\n%s",
				len(failures), len(ports)*probeWaves, failures[0], relay.diagnostics())
		}
		if failures := runParallelStreams(ports); len(failures) != 0 {
			t.Fatalf("distinct identities lost %d/%d parallel streams: %v\n%s",
				len(failures), len(ports), failures[0], relay.diagnostics())
		}
		if keys, conns := relay.duplicateCounts(t); keys != 0 || conns != 0 {
			t.Fatalf("distinct identities registered as duplicates: keys=%d conns=%d", keys, conns)
		}
	})

	t.Run("short-connections-same-identity-control", func(t *testing.T) {
		relay := startRelay(t)
		client := startEngine(t, relay, "shared-client", nil)
		allowed := []string{client.material.ClientPublic().String()}
		serverA := startEngine(t, relay, "server-a", allowed)
		serverB := startEngine(t, relay, "server-b", allowed)
		expose(t, serverA, remotePort, echoTarget(t))
		expose(t, serverB, remotePort, echoTarget(t))
		ports := []int{
			mapServer(t, client.engine, "server-a", serverA),
			mapServer(t, client.engine, "server-b", serverB),
		}
		if failures := runAlternatingProbes(ports, controlWaves); len(failures) != 0 {
			t.Fatalf("shared identity lost %d/%d short probes: %v\n%s",
				len(failures), len(ports)*controlWaves, failures[0], relay.diagnostics())
		}
	})

	t.Run("long-connections-same-identity", func(t *testing.T) {
		relay := startRelay(t)
		client := startEngine(t, relay, "shared-client", nil)
		allowed := []string{client.material.ClientPublic().String()}
		serverA := startEngine(t, relay, "server-a", allowed)
		serverB := startEngine(t, relay, "server-b", allowed)
		expose(t, serverA, remotePort, echoTarget(t))
		expose(t, serverB, remotePort, echoTarget(t))
		gate := gatedEchoTarget(t)
		expose(t, serverA, gatedPort, gate.address)
		ports := []int{
			mapServer(t, client.engine, "server-a", serverA),
			mapServer(t, client.engine, "server-b", serverB),
		}
		gatedMapping, err := client.engine.MapService(engine.ServiceRequest{
			Name: "server-a-gated", DeviceID: "server-a", Address: serverA.address,
			ServerPublic: serverA.material.ServerPublic().String(), RemotePort: gatedPort,
			RecordGeneration: 1,
		})
		if err != nil {
			t.Fatal(err)
		}

		for index, port := range ports {
			if err := roundTrip(port, fmt.Sprintf("warm-%d", index)); err != nil {
				t.Fatalf("warm server %d: %v", index, err)
			}
		}
		alternatingFailures := runAlternatingProbes(ports, probeWaves)
		parallelFailures := runParallelStreams(ports)
		misrouteErr := forceServerAResponseAfterBActivity(gatedMapping.LocalPort, ports[1], gate)
		dupKeys, dupConns := relay.duplicateCounts(t)
		if misrouteErr != nil || len(alternatingFailures)+len(parallelFailures) != 0 || dupKeys != 0 || dupConns != 0 {
			first := misrouteErr
			if first == nil {
				if failures := append(alternatingFailures, parallelFailures...); len(failures) != 0 {
					first = failures[0]
				}
			}
			t.Fatalf("shared identity split across DERP clients: duplicates=%d/%d forced-misroute=%v alternating-loss=%d/%d parallel-loss=%d/%d first=%v\n%s",
				dupKeys, dupConns, misrouteErr != nil, len(alternatingFailures), len(ports)*probeWaves,
				len(parallelFailures), len(ports), first, relay.diagnostics())
		}
	})
}
