package engine

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/tailscale/tailcat"
	"tailscale.com/tailcfg"
	"tailscale.com/types/key"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/keys"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/labrelay"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/outbound"
)

func acquireClientLeaseForTest(engine *Engine, address string) *clientLease {
	entry := engine.clients[address]
	if entry == nil {
		entry = &pooledClient{
			poolKey: address, address: address,
			client: outbound.NewClient(key.NodePrivate{}, "", tailcat.ConnInfo{}, &outbound.Diagnostics{}),
			leases: make(map[*clientLease]struct{}),
		}
		engine.clients[address] = entry
	}
	lease := &clientLease{entry: entry}
	entry.refs++
	entry.leases[lease] = struct{}{}
	return lease
}

func TestReleaseUnusedClientIsNilSafeAndIdempotent(t *testing.T) {
	engine := &Engine{clients: make(map[string]*pooledClient)}
	engine.mu.Lock()
	lease := acquireClientLeaseForTest(engine, "unused")
	engine.releaseClientLocked(lease)
	engine.releaseClientLocked(lease)
	engine.mu.Unlock()
	engine.wg.Wait()

	if got := engine.clientLeaseCount("unused"); got != 0 {
		t.Fatalf("lease count = %d, want 0", got)
	}
}

func TestFailedClientDialKeepsGenerationUntilLastLeaseReleases(t *testing.T) {
	engine := &Engine{clients: make(map[string]*pooledClient)}
	engine.mu.Lock()
	failedLease := acquireClientLeaseForTest(engine, "failed")
	failedEntry := failedLease.entry
	failedEntry.client = nil
	var closed atomic.Int32
	failedEntry.closeForTest = func() { closed.Add(1) }
	engine.mu.Unlock()

	if _, err := engine.dialTCPPort(context.Background(), failedLease, 1); err == nil {
		t.Fatal("nil client dial succeeded")
	}

	engine.mu.Lock()
	sharedLease := acquireClientLeaseForTest(engine, "failed")
	if sharedLease.entry != failedEntry {
		engine.mu.Unlock()
		t.Fatal("failed stream dial replaced a client with a live lease")
	}
	engine.releaseClientLocked(failedLease)
	engine.releaseClientLocked(failedLease)
	if engine.clients["failed"] != sharedLease.entry {
		engine.mu.Unlock()
		t.Fatal("releasing one lease removed the shared generation")
	}
	engine.releaseClientLocked(sharedLease)
	engine.mu.Unlock()
	engine.wg.Wait()

	if got := closed.Load(); got != 1 {
		t.Fatalf("failed client closed %d times, want 1", got)
	}
}

func TestSharedClientLeaseKeepsMeshAndServicesIndependent(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	keyA, materialA := writeKeyFile(t, dir, "lease-a.key")
	keyB, materialB := writeKeyFile(t, dir, "lease-b.key")
	server := &Engine{}
	if _, _, err := server.Up(context.Background(), upConfig(
		keyA, filepath.Join(dir, "lease-a.addr"), relay,
		[]string{derivedClientPublic(t, materialB, materialA)}, false, "tcp:"+echoServer(t),
	)); err != nil {
		t.Fatal(err)
	}
	defer server.Down()
	if _, err := server.Expose(context.Background(), Exposure{
		Port: 18083, Target: "tcp:" + echoServer(t), ProxyProtocol: "",
	}); err != nil {
		t.Fatal(err)
	}

	client := &Engine{}
	if _, _, err := client.Up(context.Background(), upConfig(
		keyB, filepath.Join(dir, "lease-b.addr"), relay, nil, false, "",
	)); err != nil {
		t.Fatal(err)
	}
	defer client.Down()
	address := readAddress(t, filepath.Join(dir, "lease-a.addr"))
	_, meshPort := growPeerSlot(t, client, "mesh-a", address, 7447)
	service, err := client.MapService(ServiceRequest{
		Name: "svc-a", DeviceID: "lease-a", Address: address,
		ServerPublic: materialA.ServerPublic().String(), RemotePort: 18083,
		RecordGeneration: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if got := client.clientLeaseCount(address); got != 2 {
		t.Fatalf("lease count = %d, want 2", got)
	}
	poll(t, 30*time.Second, "mesh and service shared transport", func() bool {
		return echoRoundTrip(meshPort, "mesh", 5*time.Second) == nil &&
			echoRoundTrip(service.LocalPort, "service", 5*time.Second) == nil
	})
	if _, err := client.UnmapService("svc-a"); err != nil {
		t.Fatal(err)
	}
	if got := client.clientLeaseCount(address); got != 1 {
		t.Fatalf("lease count after service unmap = %d, want 1", got)
	}
	if err := echoRoundTrip(meshPort, "mesh-survives", 5*time.Second); err != nil {
		t.Fatalf("service unmap closed mesh transport: %v", err)
	}
}

func TestClientPoolUsesServerIdentityAcrossAddressChanges(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	serverKey, _ := writeKeyFile(t, dir, "address-server.key")
	clientKey, _ := writeKeyFile(t, dir, "address-client.key")
	server := &Engine{}
	if _, _, err := server.Up(context.Background(), upConfig(
		serverKey, filepath.Join(dir, "address-server.addr"), relay, nil, false, "",
	)); err != nil {
		t.Fatal(err)
	}
	defer server.Down()
	client := &Engine{}
	if _, _, err := client.Up(context.Background(), upConfig(
		clientKey, filepath.Join(dir, "address-client.addr"), relay, nil, false, "",
	)); err != nil {
		t.Fatal(err)
	}
	defer client.Down()

	address := readAddress(t, filepath.Join(dir, "address-server.addr"))
	info, err := parseClientAddress(address)
	if err != nil {
		t.Fatal(err)
	}
	info.PresharedKey[0] ^= 0xff
	rotatedAddress := string(info.Addr())
	client.mu.Lock()
	first, firstErr := client.acquireClientLocked(address)
	second, secondErr := client.acquireClientLocked(rotatedAddress)
	client.mu.Unlock()
	if firstErr != nil || secondErr != nil {
		t.Fatalf("acquire addresses: first=%v second=%v", firstErr, secondErr)
	}
	if first.entry != second.entry {
		t.Fatal("one server public key created two client generations after address rotation")
	}
	client.mu.Lock()
	client.releaseClientLocked(first)
	client.releaseClientLocked(second)
	client.mu.Unlock()
}

func monitorRelayDuplicates(relay *labrelay.Relay) func() (int, int, error) {
	stop := make(chan struct{})
	done := make(chan struct{})
	observed := make(chan struct {
		keys, connections int
		err               error
	}, 1)
	go func() {
		defer close(done)
		ticker := time.NewTicker(20 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				keys, connections, err := relay.DuplicateClientCounts()
				if err != nil || keys != 0 || connections != 0 {
					select {
					case observed <- struct {
						keys, connections int
						err               error
					}{keys, connections, err}:
					default:
					}
				}
			case <-stop:
				return
			}
		}
	}()
	var once sync.Once
	var keys, connections int
	var err error
	return func() (int, int, error) {
		once.Do(func() {
			close(stop)
			<-done
			select {
			case sample := <-observed:
				keys, connections, err = sample.keys, sample.connections, sample.err
			default:
			}
		})
		return keys, connections, err
	}
}

func rotatePresharedKey(t *testing.T, keyFile string) keys.Material {
	t.Helper()
	encoded, err := os.ReadFile(keyFile)
	if err != nil {
		t.Fatal(err)
	}
	var file keys.File
	if err := json.Unmarshal(encoded, &file); err != nil {
		t.Fatal(err)
	}
	pskText, err := tailcat.NewPresharedKey().MarshalText()
	if err != nil {
		t.Fatal(err)
	}
	file.Server.PresharedKey = string(pskText)
	encoded, err = json.Marshal(file)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(keyFile, append(encoded, '\n'), 0o600); err != nil {
		t.Fatal(err)
	}
	material, err := keys.Load(keyFile)
	if err != nil {
		t.Fatal(err)
	}
	return material
}

func TestAddressRotationMigratesLiveLeasesWithoutDuplicateIdentity(t *testing.T) {
	relay := labrelay.Start(t)
	finishDuplicateMonitor := monitorRelayDuplicates(relay)
	defer finishDuplicateMonitor()
	dir := t.TempDir()
	serverKey, serverMaterial := writeKeyFile(t, dir, "rotation-server.key")
	clientKey, clientMaterial := writeKeyFile(t, dir, "rotation-client.key")
	meshTarget := echoServer(t)
	serviceTarget := echoServer(t)
	server := &Engine{}
	if _, _, err := server.Up(context.Background(), upConfig(
		serverKey, filepath.Join(dir, "rotation-server-old.addr"), relay,
		[]string{derivedClientPublic(t, clientMaterial, serverMaterial)}, false,
		"tcp:"+meshTarget,
	)); err != nil {
		t.Fatal(err)
	}
	defer server.Down()
	if _, err := server.Expose(context.Background(), Exposure{
		Port: 18086, Target: "tcp:" + serviceTarget,
	}); err != nil {
		t.Fatal(err)
	}

	client := &Engine{}
	if _, _, err := client.Up(context.Background(), upConfig(
		clientKey, filepath.Join(dir, "rotation-client.addr"), relay, nil, false, "",
	)); err != nil {
		t.Fatal(err)
	}
	defer client.Down()
	oldAddress := readAddress(t, filepath.Join(dir, "rotation-server-old.addr"))
	meshMapping, meshPort := growPeerSlot(t, client, "rotation-mesh", oldAddress, 7447)
	service, err := client.MapService(ServiceRequest{
		Name: "rotation-service", DeviceID: "rotation-server", Address: oldAddress,
		ServerPublic: serverMaterial.ServerPublic().String(), RemotePort: 18086,
		RecordGeneration: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	poll(t, 30*time.Second, "traffic before server credential rotation", func() bool {
		return echoRoundTrip(meshPort, "mesh-before", 5*time.Second) == nil &&
			echoRoundTrip(service.LocalPort, "service-before", 5*time.Second) == nil
	})

	server.Down()
	rotatedMaterial := rotatePresharedKey(t, serverKey)
	if rotatedMaterial.ServerPublic() != serverMaterial.ServerPublic() {
		t.Fatal("PSK rotation changed the server identity")
	}
	rotatedServer := &Engine{}
	if _, _, err := rotatedServer.Up(context.Background(), upConfig(
		serverKey, filepath.Join(dir, "rotation-server-new.addr"), relay,
		[]string{derivedClientPublic(t, clientMaterial, rotatedMaterial)}, false,
		"tcp:"+meshTarget,
	)); err != nil {
		t.Fatal(err)
	}
	defer rotatedServer.Down()
	if _, err := rotatedServer.Expose(context.Background(), Exposure{
		Port: 18086, Target: "tcp:" + serviceTarget,
	}); err != nil {
		t.Fatal(err)
	}
	newAddress := readAddress(t, filepath.Join(dir, "rotation-server-new.addr"))
	if newAddress == oldAddress {
		t.Fatal("PSK rotation did not change the Tailcat address")
	}
	if _, err := client.MapPeer(
		"rotation-mesh", newAddress, 7447, meshMapping.Slot,
	); err != nil {
		t.Fatal(err)
	}
	poll(t, 30*time.Second, "traffic after coordinated client generation handoff", func() bool {
		return echoRoundTrip(meshPort, "mesh-after", 5*time.Second) == nil &&
			echoRoundTrip(service.LocalPort, "service-after", 5*time.Second) == nil
	})
	duplicateKeys, duplicateConnections, monitorErr := finishDuplicateMonitor()
	if monitorErr != nil {
		t.Fatal(monitorErr)
	}
	if duplicateKeys != 0 || duplicateConnections != 0 {
		t.Fatalf("rotation registered duplicate clients: keys=%d conns=%d",
			duplicateKeys, duplicateConnections)
	}
}

func TestFailedServiceDialDoesNotSplitSameServerClientGeneration(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	serverKey, serverMaterial := writeKeyFile(t, dir, "generation-server.key")
	clientKey, clientMaterial := writeKeyFile(t, dir, "generation-client.key")
	server := &Engine{}
	if _, _, err := server.Up(context.Background(), upConfig(
		serverKey, filepath.Join(dir, "generation-server.addr"), relay,
		[]string{derivedClientPublic(t, clientMaterial, serverMaterial)}, false, "tcp:"+echoServer(t),
	)); err != nil {
		t.Fatal(err)
	}
	defer server.Down()
	if _, err := server.Expose(context.Background(), Exposure{
		Port: 18083, Target: "tcp:" + echoServer(t),
	}); err != nil {
		t.Fatal(err)
	}

	client := &Engine{}
	if _, _, err := client.Up(context.Background(), upConfig(
		clientKey, filepath.Join(dir, "generation-client.addr"), relay, nil, false, "",
	)); err != nil {
		t.Fatal(err)
	}
	defer client.Down()
	address := readAddress(t, filepath.Join(dir, "generation-server.addr"))
	_, meshPort := growPeerSlot(t, client, "generation-mesh", address, 7447)
	bad, err := client.MapService(ServiceRequest{
		Name: "generation-bad", DeviceID: "generation-server", Address: address,
		ServerPublic: serverMaterial.ServerPublic().String(), RemotePort: 18084,
		RecordGeneration: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := echoRoundTrip(meshPort, "mesh-before-failure", 5*time.Second); err != nil {
		t.Fatal(err)
	}
	if err := echoRoundTrip(bad.LocalPort, "must-fail", 8*time.Second); err == nil {
		t.Fatal("unexposed service port unexpectedly accepted traffic")
	}
	poll(t, 5*time.Second, "failed service dial status", func() bool {
		statuses, statusErr := client.ServiceStatus()
		return statusErr == nil && len(statuses) == 1 && statuses[0].LastDialState == "error"
	})

	good, err := client.MapService(ServiceRequest{
		Name: "generation-good", DeviceID: "generation-server", Address: address,
		ServerPublic: serverMaterial.ServerPublic().String(), RemotePort: 18083,
		RecordGeneration: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	client.mu.Lock()
	meshGeneration := client.mapped["generation-mesh"].lease.entry
	goodGeneration := client.services["generation-good"].lease.entry
	client.mu.Unlock()
	if meshGeneration != goodGeneration {
		t.Fatal("failed service dial split one server identity across live client generations")
	}
	poll(t, 30*time.Second, "mesh and replacement service traffic", func() bool {
		return echoRoundTrip(good.LocalPort, "good-service", 5*time.Second) == nil &&
			echoRoundTrip(meshPort, "mesh-after-failure", 5*time.Second) == nil
	})
}

func TestServerReregistrationRetainsSharedClientGeneration(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	serverKey, serverMaterial := writeKeyFile(t, dir, "restart-server.key")
	clientKey, clientMaterial := writeKeyFile(t, dir, "restart-client.key")
	server := &Engine{}
	cfg := upConfig(serverKey, filepath.Join(dir, "server.addr"), relay,
		[]string{derivedClientPublic(t, clientMaterial, serverMaterial)}, false, "tcp:"+echoServer(t))
	exposure := Exposure{Port: 18083, Target: "tcp:" + echoServer(t)}
	startServer := func() {
		if _, _, err := server.Up(context.Background(), cfg); err != nil {
			t.Fatal(err)
		}
		if _, err := server.Expose(context.Background(), exposure); err != nil {
			t.Fatal(err)
		}
	}
	startServer()
	defer server.Down()
	client := &Engine{}
	if _, _, err := client.Up(context.Background(), upConfig(
		clientKey, filepath.Join(dir, "client.addr"), relay, nil, false, "",
	)); err != nil {
		t.Fatal(err)
	}
	defer client.Down()
	address := readAddress(t, cfg.AddressFile)
	_, meshPort := growPeerSlot(t, client, "mesh", address, 7447)
	service, err := client.MapService(ServiceRequest{Name: "service", DeviceID: "server",
		Address: address, ServerPublic: serverMaterial.ServerPublic().String(),
		RemotePort: 18083, RecordGeneration: 1})
	if err != nil {
		t.Fatal(err)
	}
	for _, port := range []int{meshPort, service.LocalPort} {
		if err := echoRoundTrip(port, "before", 5*time.Second); err != nil {
			t.Fatal(err)
		}
	}
	client.mu.Lock()
	original := client.mapped["mesh"].lease.entry
	client.mu.Unlock()
	server.Down()
	startServer()
	poll(t, 30*time.Second, "same-generation mesh and service restart recovery", func() bool {
		return echoRoundTrip(meshPort, "mesh-after", 3*time.Second) == nil &&
			echoRoundTrip(service.LocalPort, "service-after", 3*time.Second) == nil
	})
	client.mu.Lock()
	defer client.mu.Unlock()
	if len(client.clients) != 1 || client.mapped["mesh"].lease.entry != original ||
		client.services["service"].lease.entry != original {
		t.Fatal("re-registration replaced or split the shared DERP client generation")
	}
}

func TestTCPSlotExplicitPortFallsBackWhenOccupied(t *testing.T) {
	occupied, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer occupied.Close()
	spec := fmt.Sprintf("tcp:127.0.0.1:%d", occupied.Addr().(*net.TCPAddr).Port)

	engine := &Engine{up: true, slots: make(map[string]*slotPort)}
	if err := engine.listenSlots([]string{spec}, defaultSlotHold); err != nil {
		t.Fatalf("listenSlots on occupied persisted port: %v", err)
	}
	t.Cleanup(func() {
		engine.mu.Lock()
		engine.closeSlotsLocked()
		engine.mu.Unlock()
		engine.wg.Wait()
	})
	ids := engine.SlotIDs()
	if len(ids) != 1 || ids[0] == spec {
		t.Fatalf("slot ids = %v, want one fallback distinct from %s", ids, spec)
	}
}

func TestSameCallerKeyTwoServersSameDERPCoexistence(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	clientKey, clientMaterial := writeKeyFile(t, dir, "coexist-client.key")

	type host struct {
		engine   *Engine
		address  string
		public   string
		keyFile  string
		addrFile string
	}
	startHost := func(name string) host {
		keyFile, material := writeKeyFile(t, dir, name+".key")
		addrFile := filepath.Join(dir, name+".addr")
		eng := &Engine{}
		if _, _, err := eng.Up(context.Background(), upConfig(
			keyFile, addrFile, relay,
			[]string{derivedClientPublic(t, clientMaterial, material)}, false, "tcp:"+echoServer(t),
		)); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(eng.Down)
		for _, port := range []int{18084, 18085} {
			if _, err := eng.Expose(context.Background(), Exposure{
				Port: port, Target: "tcp:" + echoServer(t), ProxyProtocol: "",
			}); err != nil {
				t.Fatal(err)
			}
		}
		return host{eng, readAddress(t, addrFile), material.ServerPublic().String(), keyFile, addrFile}
	}
	hostA := startHost("coexist-a")
	hostC := startHost("coexist-c")

	client := &Engine{}
	if _, _, err := client.Up(context.Background(), upConfig(
		clientKey, filepath.Join(dir, "coexist-client.addr"), relay, nil, false, "",
	)); err != nil {
		t.Fatal(err)
	}
	defer client.Down()
	_, meshPort := growPeerSlot(t, client, "mesh-a", hostA.address, 7447)
	mapService := func(name string, host host, port int) ServiceMapping {
		mapping, err := client.MapService(ServiceRequest{
			Name: name, DeviceID: name, Address: host.address,
			ServerPublic: host.public, RemotePort: port, RecordGeneration: 1,
		})
		if err != nil {
			t.Fatal(err)
		}
		return mapping
	}
	a1 := mapService("a-one", hostA, 18084)
	a2 := mapService("a-two", hostA, 18085)
	c1 := mapService("c-one", hostC, 18084)

	ports := []int{meshPort, a1.LocalPort, a2.LocalPort, c1.LocalPort}
	poll(t, 45*time.Second, "concurrent same-key traffic to both servers", func() bool {
		var wg sync.WaitGroup
		failures := make(chan error, len(ports))
		for i, port := range ports {
			wg.Add(1)
			go func(i, port int) {
				defer wg.Done()
				failures <- echoRoundTrip(port, "coexist-"+itoa(i), 5*time.Second)
			}(i, port)
		}
		wg.Wait()
		close(failures)
		for err := range failures {
			if err != nil {
				return false
			}
		}
		return true
	})

	if _, err := client.UnmapService("a-one"); err != nil {
		t.Fatal(err)
	}
	if err := echoRoundTrip(meshPort, "mesh-after-unmap", 5*time.Second); err != nil {
		t.Fatal(err)
	}
	if err := echoRoundTrip(a2.LocalPort, "a2-after-unmap", 5*time.Second); err != nil {
		t.Fatal(err)
	}
	if err := echoRoundTrip(c1.LocalPort, "c1-after-unmap", 5*time.Second); err != nil {
		t.Fatal(err)
	}

	if _, err := hostA.engine.Allow(nil, false); err != nil {
		t.Fatal(err)
	}
	if err := echoRoundTrip(a2.LocalPort, "revoked", 8*time.Second); err == nil {
		t.Fatal("host A revocation did not stop existing caller")
	}
	if err := echoRoundTrip(c1.LocalPort, "other-host-survives", 8*time.Second); err != nil {
		t.Fatalf("host A revocation broke host C: %v", err)
	}
	hostAMaterial, err := keys.Load(hostA.keyFile)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := hostA.engine.Allow(
		[]string{derivedClientPublic(t, clientMaterial, hostAMaterial)}, false,
	); err != nil {
		t.Fatal(err)
	}
	if _, err := client.UnmapService("a-two"); err != nil {
		t.Fatal(err)
	}
	a2 = mapService("a-two", hostA, 18085)
	poll(t, 30*time.Second, "recovery after readmission", func() bool {
		return echoRoundTrip(a2.LocalPort, "recovered", 5*time.Second) == nil
	})
}

type timeoutCopyError struct{}

func (timeoutCopyError) Error() string   { return "copy deadline exceeded" }
func (timeoutCopyError) Timeout() bool   { return true }
func (timeoutCopyError) Temporary() bool { return true }

func TestCollectSpliceCapturesBothCopyOutcomes(t *testing.T) {
	cases := []struct {
		name       string
		outcomeIn  copyOutcome
		outcomeOut copyOutcome
		wantIn     string
		wantOut    string
		wantSide   string
	}{
		{"local EOF", copyOutcome{bytes: 13, err: net.ErrClosed}, copyOutcome{bytes: 11}, "canceled", "eof", "local"},
		{"peer reset", copyOutcome{bytes: 17, err: syscall.ECONNRESET}, copyOutcome{bytes: 19, err: net.ErrClosed}, "reset", "canceled", "peer"},
		{"peer timeout", copyOutcome{bytes: 23, err: timeoutCopyError{}}, copyOutcome{bytes: 29, err: net.ErrClosed}, "timeout", "canceled", "peer"},
		{"both EOF", copyOutcome{bytes: 31}, copyOutcome{bytes: 37}, "eof", "eof", "both"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			local, localPeer := net.Pipe()
			remote, remotePeer := net.Pipe()
			defer localPeer.Close()
			defer remotePeer.Close()
			pair := &connPair{left: local, right: remote}
			result := collectSplice(pair, func(_ io.Writer, source io.Reader) (int64, error) {
				if source == local {
					return tc.outcomeOut.bytes, tc.outcomeOut.err
				}
				return tc.outcomeIn.bytes, tc.outcomeIn.err
			})
			if result.bytesIn != tc.outcomeIn.bytes || result.bytesOut != tc.outcomeOut.bytes {
				t.Fatalf("bytes = in:%d out:%d", result.bytesIn, result.bytesOut)
			}
			if result.errorIn != tc.wantIn || result.errorOut != tc.wantOut || result.closeSide != tc.wantSide {
				t.Fatalf("outcome = in:%q out:%q side:%q", result.errorIn, result.errorOut, result.closeSide)
			}
		})
	}
}

func TestConnectionTelemetryIsBoundedPerMappedPort(t *testing.T) {
	mapPort := 41001
	history := newConnectionHistory("dev-a", "tcp:127.0.0.1:41001", &mapPort)
	for index := 0; index < connectionHistoryCapacity+3; index++ {
		history.record(spliceResult{
			bytesIn: int64(index), bytesOut: int64(index + 1),
			errorIn: "eof", errorOut: "reset", closeSide: "peer",
			durationMs: int64(index + 2),
		})
	}
	snapshot := history.snapshot()
	if snapshot.ClosedCount != connectionHistoryCapacity+3 {
		t.Fatalf("closed count = %d", snapshot.ClosedCount)
	}
	if len(snapshot.Recent) != connectionHistoryCapacity {
		t.Fatalf("recent length = %d", len(snapshot.Recent))
	}
	if snapshot.Recent[0].BytesIn != 3 || snapshot.Recent[len(snapshot.Recent)-1].BytesIn != connectionHistoryCapacity+2 {
		t.Fatalf("ring retained wrong closes: %+v", snapshot.Recent)
	}
	if snapshot.ErrorCounts["eof"] != connectionHistoryCapacity+3 || snapshot.ErrorCounts["reset"] != connectionHistoryCapacity+3 {
		t.Fatalf("error counters = %+v", snapshot.ErrorCounts)
	}
}

func TestConnectionTelemetryCountsDialFailuresWithoutErrorDetail(t *testing.T) {
	mapPort := 41001
	history := newConnectionHistory("dev-a", "tcp:127.0.0.1:41001", &mapPort)
	history.recordDialFailure(fmt.Errorf("dial tcSecretAddressMaterial: %w", syscall.ECONNREFUSED))
	history.recordDialFailure(context.DeadlineExceeded)

	snapshot := history.snapshot()
	if snapshot.DialAttempts != 2 || snapshot.DialFailures != 2 {
		t.Fatalf("dial attempts/failures = %d/%d", snapshot.DialAttempts, snapshot.DialFailures)
	}
	if snapshot.DialFailureReasons["refused"] != 1 || snapshot.DialFailureReasons["timeout"] != 1 {
		t.Fatalf("dial failure reasons = %+v", snapshot.DialFailureReasons)
	}
	if snapshot.SplicesEstablished != 0 || snapshot.ClosedCount != 0 {
		t.Fatalf("failing peer looked connected: %+v", snapshot)
	}
	if snapshot.LastDialAtMs <= 0 || snapshot.LastDialError != "timeout" {
		t.Fatalf("last dial = %d %q", snapshot.LastDialAtMs, snapshot.LastDialError)
	}
	encoded, err := json.Marshal(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), "tcSecretAddressMaterial") {
		t.Fatalf("telemetry leaked dial error detail: %s", encoded)
	}
}

func TestConnectionTelemetrySeparatesHealthySplicesFromNeverConnected(t *testing.T) {
	history := newConnectionHistory("dev-healthy", "/run/slot-00", nil)
	history.recordDialSuccess()
	history.record(spliceResult{errorIn: "eof", errorOut: "eof", closeSide: "both"})
	snapshot := history.snapshot()
	if snapshot.DialAttempts != 1 || snapshot.DialFailures != 0 || snapshot.SplicesEstablished != 1 {
		t.Fatalf("healthy peer telemetry = %+v", snapshot)
	}
	if snapshot.ClosedCount != 1 || snapshot.CloseReasons["eof"] != 2 {
		t.Fatalf("healthy close telemetry = %+v", snapshot)
	}
}

func TestValidateDERPMapRejectsInsecureForTests(t *testing.T) {
	dm := &tailcfg.DERPMap{Regions: map[tailcfg.DERPRegionID]*tailcfg.DERPRegion{
		900: {RegionID: 900, Nodes: []*tailcfg.DERPNode{{
			RegionID: 900, HostName: "relay.example", InsecureForTests: true,
		}}},
	}}
	if err := validateDERPMap(dm); !errors.Is(err, ErrInsecureDERP) {
		t.Fatalf("validateDERPMap = %v, want ErrInsecureDERP", err)
	}
}

func TestValidateDERPMapAcceptsAPinnedMap(t *testing.T) {
	dm := &tailcfg.DERPMap{Regions: map[tailcfg.DERPRegionID]*tailcfg.DERPRegion{
		900: {RegionID: 900, Nodes: []*tailcfg.DERPNode{{
			RegionID: 900, HostName: "relay.example", CertName: "sha256-raw:abcd",
		}}},
	}}
	if err := validateDERPMap(dm); err != nil {
		t.Fatalf("validateDERPMap = %v, want nil", err)
	}
}

func TestDiskDERPMapCacheWritesOwnerOnly(t *testing.T) {
	dir := t.TempDir() + "/cache"
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	dataPath, etagPath := diskDERPMapCache{dir: dir}.paths("https://example/map.json")
	if err := os.WriteFile(dataPath, []byte("old"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(etagPath, []byte("old"), 0o644); err != nil {
		t.Fatal(err)
	}
	cache := diskDERPMapCache{dir: dir}
	if err := cache.Put("https://example/map.json", []byte(`{"Regions":{}}`), "etag"); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(dir)
	if err != nil || info.Mode().Perm() != 0o700 {
		t.Fatalf("cache dir mode = %v, %v", info, err)
	}
	for _, path := range []string{dataPath, etagPath} {
		info, err := os.Stat(path)
		if err != nil || info.Mode().Perm() != 0o600 {
			t.Fatalf("%s mode = %v, %v", path, info, err)
		}
	}
	data, etag, _, ok := cache.Get("https://example/map.json")
	if !ok || etag != "etag" || string(data) != `{"Regions":{}}` {
		t.Fatalf("Get = %q, %q, %v", data, etag, ok)
	}
}

func TestDERPMapCacheFallsBackWhenFetchFails(t *testing.T) {
	dir := t.TempDir() + "/cache"
	cache := diskDERPMapCache{dir: dir}
	ok := true
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if !ok {
			http.Error(w, "down", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Etag", `"v1"`)
		_ = json.NewEncoder(w).Encode(&tailcfg.DERPMap{
			Regions: map[tailcfg.DERPRegionID]*tailcfg.DERPRegion{
				900: {RegionID: 900, RegionCode: "r900", Nodes: []*tailcfg.DERPNode{{
					Name: "n", RegionID: 900, HostName: "relay.example", DERPPort: 1279,
				}}},
			},
		})
	}))
	defer server.Close()
	mapURL := tailcat.DERPMapURL(server.URL + "/map.json")
	if _, err := tailcat.FetchDERPMap(context.Background(), mapURL, tailcat.ExpandForServer, cache); err != nil {
		t.Fatal(err)
	}
	dataPath, _ := cache.paths(string(mapURL))
	old := time.Now().Add(-2 * time.Hour)
	if err := os.Chtimes(dataPath, old, old); err != nil {
		t.Fatal(err)
	}
	ok = false
	dm, err := tailcat.FetchDERPMap(context.Background(), mapURL, tailcat.ExpandForServer, cache)
	if err != nil || dm == nil {
		t.Fatalf("cache fallback = %v, %v", dm, err)
	}
}

func TestDERPMapFetchWithoutCacheFails(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "down", http.StatusInternalServerError)
	}))
	defer server.Close()
	cache := diskDERPMapCache{dir: t.TempDir() + "/empty"}
	_, err := tailcat.FetchDERPMap(
		context.Background(), tailcat.DERPMapURL(server.URL+"/map.json"),
		tailcat.ExpandForServer, cache,
	)
	if err == nil {
		t.Fatal("expected fetch failure without a cache")
	}
}

func TestValidateDERPMapRejectsMalformedMaps(t *testing.T) {
	tests := map[string]*tailcfg.DERPMap{
		"nil-region": {Regions: map[tailcfg.DERPRegionID]*tailcfg.DERPRegion{900: nil}},
		"nil-node": {Regions: map[tailcfg.DERPRegionID]*tailcfg.DERPRegion{
			901: {RegionID: 901, Nodes: []*tailcfg.DERPNode{nil}},
		}},
		"empty-nodes": {Regions: map[tailcfg.DERPRegionID]*tailcfg.DERPRegion{
			901: {RegionID: 901, Nodes: []*tailcfg.DERPNode{}},
		}},
		"missing-hostname": {Regions: map[tailcfg.DERPRegionID]*tailcfg.DERPRegion{
			901: {RegionID: 901, Nodes: []*tailcfg.DERPNode{{RegionID: 901}}},
		}},
		"zero-regions": {Regions: map[tailcfg.DERPRegionID]*tailcfg.DERPRegion{}},
	}
	for name, dm := range tests {
		t.Run(name, func(t *testing.T) {
			if err := validateDERPMap(dm); err == nil {
				t.Fatal("validateDERPMap accepted a corrupt map")
			}
		})
	}
}

func TestFetchedNilDERPMapFailsValidationBeforeStart(t *testing.T) {
	payloads := []string{
		`{"Regions":{"900":null}}`,
		`{"Regions":{"900":{"RegionID":900,"Nodes":[null]}}}`,
	}
	for _, payload := range payloads {
		body := payload
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			_, _ = w.Write([]byte(body))
		}))
		dm, err := tailcat.FetchDERPMap(
			context.Background(), tailcat.DERPMapURL(server.URL+"/map.json"),
			tailcat.ExpandForServer, diskDERPMapCache{dir: t.TempDir() + "/cache"},
		)
		server.Close()
		if err != nil {
			continue
		}
		if err := validateDERPMap(dm); err == nil {
			t.Fatalf("fetched map %s passed validation", body)
		}
	}
}
