package engine

import (
	"context"
	"errors"
	"net"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/labrelay"
)

type temporaryAcceptError struct{}

func (temporaryAcceptError) Error() string   { return "temporary accept failure" }
func (temporaryAcceptError) Timeout() bool   { return false }
func (temporaryAcceptError) Temporary() bool { return true }

type acceptResult struct {
	conn net.Conn
	err  error
}

type scriptedServiceListener struct {
	results chan acceptResult
	closed  chan struct{}
	once    sync.Once
}

func newScriptedServiceListener(capacity int) *scriptedServiceListener {
	return &scriptedServiceListener{
		results: make(chan acceptResult, capacity),
		closed:  make(chan struct{}),
	}
}

func (l *scriptedServiceListener) Accept() (net.Conn, error) {
	select {
	case result := <-l.results:
		return result.conn, result.err
	case <-l.closed:
		return nil, net.ErrClosed
	}
}

func (l *scriptedServiceListener) Close() error {
	l.once.Do(func() { close(l.closed) })
	return nil
}

func (l *scriptedServiceListener) Addr() net.Addr {
	return &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1)}
}

func serviceAcceptFixture(listener net.Listener) (*Engine, *mappedService, context.Context, context.CancelFunc) {
	ctx, cancel := context.WithCancel(context.Background())
	service := &mappedService{
		request:  ServiceRequest{Name: "capped", RemotePort: 18080},
		mapping:  ServiceMapping{Name: "capped", LastDialState: "unknown"},
		listener: listener,
		pairs:    make(map[*connPair]struct{}),
	}
	engine := &Engine{up: true, services: map[string]*mappedService{"capped": service}}
	return engine, service, ctx, cancel
}

func TestServiceAcceptRetriesTemporaryError(t *testing.T) {
	listener := newScriptedServiceListener(2)
	listener.results <- acceptResult{err: temporaryAcceptError{}}
	local, remote := net.Pipe()
	defer remote.Close()
	listener.results <- acceptResult{conn: local}
	client, service, ctx, cancel := serviceAcceptFixture(listener)
	started := make(chan struct{}, 1)
	client.serviceDialStartedForTest = func(string) { started <- struct{}{} }
	client.wg.Add(1)
	go client.acceptService(ctx, service)
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("temporary accept error stopped the service listener")
	}
	cancel()
	_ = listener.Close()
	client.wg.Wait()
}

func TestServiceAcceptFatalErrorMarksStatusFailed(t *testing.T) {
	listener := newScriptedServiceListener(1)
	listener.results <- acceptResult{err: errors.New("fatal accept failure")}
	client, service, ctx, cancel := serviceAcceptFixture(listener)
	defer cancel()
	client.wg.Add(1)
	client.acceptService(ctx, service)

	status, err := client.ServiceStatus()
	if err != nil {
		t.Fatal(err)
	}
	if len(status) != 1 || status[0].LastDialState != "error" ||
		status[0].LastError == nil || *status[0].LastError != "service listener failed" {
		t.Fatalf("fatal accept error was not reflected in status: %+v", status)
	}
}

func TestServiceConcurrentConnectionCap(t *testing.T) {
	const expectedCap = 64
	listener := newScriptedServiceListener(expectedCap + 1)
	remotes := make([]net.Conn, 0, expectedCap+1)
	for range expectedCap + 1 {
		local, remote := net.Pipe()
		listener.results <- acceptResult{conn: local}
		remotes = append(remotes, remote)
	}
	client, service, ctx, cancel := serviceAcceptFixture(listener)
	started := make(chan struct{}, expectedCap+1)
	release := make(chan struct{})
	client.serviceDialStartedForTest = func(string) {
		started <- struct{}{}
		<-release
	}
	client.wg.Add(1)
	go client.acceptService(ctx, service)

	count := 0
	deadline := time.After(250 * time.Millisecond)
collect:
	for {
		select {
		case <-started:
			count++
			if count > expectedCap {
				break collect
			}
		case <-deadline:
			break collect
		}
	}
	if count != expectedCap {
		t.Fatalf("concurrent handlers = %d, want documented cap %d", count, expectedCap)
	}

	cancel()
	_ = listener.Close()
	close(release)
	for _, remote := range remotes {
		_ = remote.Close()
	}
	client.wg.Wait()
}

func TestServiceFixedPortConflictAndPeerNamespace(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	keyA, materialA := writeKeyFile(t, dir, "namespace-a.key")
	keyB, materialB := writeKeyFile(t, dir, "namespace-b.key")
	server := &Engine{}
	if _, _, err := server.Up(context.Background(), upConfig(
		keyA, filepath.Join(dir, "namespace-a.addr"), relay,
		[]string{derivedClientPublic(t, materialB, materialA)}, false, "tcp:"+echoServer(t),
	)); err != nil {
		t.Fatal(err)
	}
	defer server.Down()
	client := &Engine{}
	if _, _, err := client.Up(context.Background(), upConfig(
		keyB, filepath.Join(dir, "namespace-b.addr"), relay, nil, false, "",
	)); err != nil {
		t.Fatal(err)
	}
	defer client.Down()
	address := readAddress(t, filepath.Join(dir, "namespace-a.addr"))
	if _, err := client.MapPeer("same-name", address, 7447, "tcp:127.0.0.1:0"); err != nil {
		t.Fatal(err)
	}

	reserved, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := reserved.Addr().(*net.TCPAddr).Port
	request := ServiceRequest{
		Name: "same-name", DeviceID: "namespace-a", Address: address,
		ServerPublic: materialA.ServerPublic().String(), RemotePort: 7447,
		RecordGeneration: 1, LocalPort: port,
	}
	if _, err := client.MapService(request); !errors.Is(err, ErrPortInUse) {
		t.Fatalf("fixed port conflict = %v, want ErrPortInUse", err)
	}
	_ = reserved.Close()
	mapping, err := client.MapService(request)
	if err != nil || mapping.LocalPort != port {
		t.Fatalf("fixed map = %+v, %v", mapping, err)
	}
	_, _, _, peers, _ := client.Status()
	services, statusErr := client.ServiceStatus()
	if statusErr != nil || len(peers) != 1 || len(services) != 1 {
		t.Fatalf("peer/service namespaces mixed: peers=%+v services=%+v error=%v", peers, services, statusErr)
	}
}

func TestDownCancelsInflightServiceDial(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	keyA, materialA := writeKeyFile(t, dir, "cancel-a.key")
	keyB, materialB := writeKeyFile(t, dir, "cancel-b.key")
	server := &Engine{}
	if _, _, err := server.Up(context.Background(), upConfig(
		keyA, filepath.Join(dir, "cancel-a.addr"), relay,
		[]string{derivedClientPublic(t, materialB, materialA)}, false, "",
	)); err != nil {
		t.Fatal(err)
	}
	defer server.Down()
	client := &Engine{}
	started := make(chan struct{}, 1)
	client.serviceDialStartedForTest = func(string) { started <- struct{}{} }
	if _, _, err := client.Up(context.Background(), upConfig(
		keyB, filepath.Join(dir, "cancel-b.addr"), relay, nil, false, "",
	)); err != nil {
		t.Fatal(err)
	}
	mapping, err := client.MapService(ServiceRequest{
		Name: "cancel", DeviceID: "cancel-a",
		Address:      readAddress(t, filepath.Join(dir, "cancel-a.addr")),
		ServerPublic: materialA.ServerPublic().String(), RemotePort: 18087,
		RecordGeneration: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	conn, err := net.DialTimeout("tcp", "127.0.0.1:"+itoa(mapping.LocalPort), 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("service dial did not start")
	}
	done := make(chan struct{})
	go func() {
		client.Down()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("Down did not cancel the admitted dial within the bound")
	}
}

func TestServiceMapDataPlaneObservationAndUnmap(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	keyA, materialA := writeKeyFile(t, dir, "service-a.key")
	keyB, materialB := writeKeyFile(t, dir, "client-b.key")

	server := &Engine{}
	if _, _, err := server.Up(context.Background(), upConfig(
		keyA, filepath.Join(dir, "service-a.addr"), relay,
		[]string{derivedClientPublic(t, materialB, materialA)}, false, "",
	)); err != nil {
		t.Fatal(err)
	}
	defer server.Down()
	if _, err := server.Expose(context.Background(), Exposure{
		Port: 18081, Target: "tcp:" + echoServer(t), ProxyProtocol: "",
	}); err != nil {
		t.Fatal(err)
	}

	client := &Engine{}
	if _, _, err := client.Up(context.Background(), upConfig(
		keyB, filepath.Join(dir, "client-b.addr"), relay, nil, false, "",
	)); err != nil {
		t.Fatal(err)
	}
	defer client.Down()

	req := ServiceRequest{
		Name: "api", DeviceID: "service-a",
		Address:      readAddress(t, filepath.Join(dir, "service-a.addr")),
		ServerPublic: materialA.ServerPublic().String(), RemotePort: 18081,
		RecordGeneration: 3, LocalPort: 0,
	}
	mapping, err := client.MapService(req)
	if err != nil {
		t.Fatalf("map service: %v", err)
	}
	if mapping.LocalPort < 1024 || mapping.Path != "unknown" ||
		mapping.LastDialState != "unknown" || mapping.ActiveConnections != 0 {
		t.Fatalf("fresh mapping is not honest: %+v", mapping)
	}
	again, err := client.MapService(req)
	if err != nil || again.LocalPort != mapping.LocalPort {
		t.Fatalf("idempotent map = %+v, %v", again, err)
	}

	poll(t, 30*time.Second, "service echo", func() bool {
		return echoRoundTrip(mapping.LocalPort, "service-bytes\n\ndata: x\n", 5*time.Second) == nil
	})
	status, err := client.ServiceStatus()
	if err != nil {
		t.Fatal(err)
	}
	if len(status) != 1 || status[0].Name != "api" ||
		status[0].LastDialState != "ok" || status[0].LastDialMs == nil {
		t.Fatalf("service status after dial = %+v", status)
	}

	removed, err := client.UnmapService("api")
	if err != nil || !removed {
		t.Fatalf("unmap service = %v, %v", removed, err)
	}
	removed, err = client.UnmapService("api")
	if err != nil || removed {
		t.Fatalf("idempotent absent unmap = %v, %v", removed, err)
	}
	if err := echoRoundTrip(mapping.LocalPort, "closed", 500*time.Millisecond); err == nil {
		t.Fatal("service listener survived unmap")
	}
}

func TestServiceValidationConflictsAndRetainedDialError(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	keyA, materialA := writeKeyFile(t, dir, "service-a.key")
	keyB, materialB := writeKeyFile(t, dir, "client-b.key")

	server := &Engine{}
	if _, _, err := server.Up(context.Background(), upConfig(
		keyA, filepath.Join(dir, "service-a.addr"), relay,
		[]string{derivedClientPublic(t, materialB, materialA)}, false, "",
	)); err != nil {
		t.Fatal(err)
	}
	defer server.Down()
	client := &Engine{}
	if _, _, err := client.Up(context.Background(), upConfig(
		keyB, filepath.Join(dir, "client-b.addr"), relay, nil, false, "",
	)); err != nil {
		t.Fatal(err)
	}
	defer client.Down()

	request := ServiceRequest{
		Name: "api", DeviceID: "service-a",
		Address:      readAddress(t, filepath.Join(dir, "service-a.addr")),
		ServerPublic: materialA.ServerPublic().String(), RemotePort: 18082,
		RecordGeneration: 1, LocalPort: 0,
	}
	badPin := request
	badPin.ServerPublic = materialB.ServerPublic().String()
	if _, err := client.MapService(badPin); !errors.Is(err, ErrBadInput) || strings.Contains(err.Error(), badPin.Address) {
		t.Fatalf("key mismatch = %v; want fixed safe bad input", err)
	}
	mapped, err := client.MapService(request)
	if err != nil {
		t.Fatal(err)
	}
	changed := request
	changed.RemotePort++
	if _, err := client.MapService(changed); !errors.Is(err, ErrBadInput) {
		t.Fatalf("changed binding replaced without unmap: %v", err)
	}

	if err := echoRoundTrip(mapped.LocalPort, "upstream-down", 8*time.Second); err == nil {
		t.Fatal("unexpected dial success without remote exposure")
	}
	statuses, err := client.ServiceStatus()
	if err != nil {
		t.Fatal(err)
	}
	status := statuses[0]
	if status.LastDialState != "error" || status.LastError == nil ||
		status.LastErrorAtMs == nil || status.LastDialMs == nil {
		t.Fatalf("dial failure not retained: %+v", status)
	}
	if strings.Contains(*status.LastError, request.Address) {
		t.Fatal("status leaked private address")
	}

	if _, err := server.Expose(context.Background(), Exposure{
		Port: 18082, Target: "tcp:" + echoServer(t), ProxyProtocol: "",
	}); err != nil {
		t.Fatal(err)
	}
	poll(t, 30*time.Second, "service recovery", func() bool {
		return echoRoundTrip(mapped.LocalPort, "recovered", 5*time.Second) == nil
	})
	statuses, err = client.ServiceStatus()
	if err != nil {
		t.Fatal(err)
	}
	status = statuses[0]
	if status.LastDialState != "ok" || status.LastError == nil {
		t.Fatalf("success must retain historical failure: %+v", status)
	}
}

func freeTCPPort(t *testing.T) int {
	t.Helper()
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	_ = listener.Close()
	return port
}

func TestValidInboundTarget(t *testing.T) {
	for _, target := range []string{
		"", "unix:/a/zenoh.sock", "tcp:127.0.0.1:1", "tcp:127.0.0.1:65535",
	} {
		if !validInboundTarget(target) {
			t.Fatalf("refused valid inbound target %q", target)
		}
	}
	for _, target := range []string{
		"unix:relative.sock", "tcp:0.0.0.0:7443", "tcp:192.168.1.2:7443",
		"tcp:127.0.0.2:7443", "tcp:[::1]:7443", "tcp:localhost:7443",
		"tcp:127.0.0.1:0", "tcp:127.0.0.1:65536", "127.0.0.1:7443",
		"tcp/127.0.0.1:7443",
	} {
		if validInboundTarget(target) {
			t.Fatalf("accepted unsafe inbound target %q", target)
		}
	}
}

func TestMembershipUpAcceptsLoopbackTCPInbound(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	keyFile, _ := writeKeyFile(t, dir, "membership.key")
	echo := echoServer(t)
	cfg := upConfig(
		keyFile, filepath.Join(dir, "membership.addr"), relay,
		nil, false, "tcp:"+echo,
	)
	cfg.MembershipStore = filepath.Join(dir, "private", "membership.json")
	cfg.ControlPort = freeTCPPort(t)
	cfg.Owner = "tester"
	cfg.DeviceID = "node-a"

	engine := &Engine{}
	if _, _, err := engine.Up(context.Background(), cfg); err != nil {
		t.Fatalf("membership up with loopback TCP inbound: %v", err)
	}
	engine.Down()
}

func TestMembershipUpRejectsUnsafeInboundBeforeDeviceFiles(t *testing.T) {
	for _, target := range []string{
		"", "tcp:0.0.0.0:7443", "tcp:192.168.1.2:7443", "unix:relative.sock",
	} {
		cfg := UpConfig{
			KeyFile: "/absent.key", MembershipStore: "/absent-membership.json",
			InboundTarget: target, PeerPort: 7447, AddressFile: "/absent.addr",
		}
		if _, _, err := (&Engine{}).Up(context.Background(), cfg); !errors.Is(err, ErrBadInput) {
			t.Fatalf("inbound %q = %v, want ErrBadInput", target, err)
		}
	}
}
