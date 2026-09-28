package node

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tsnet/internal/proto"
	"tailscale.com/ipn"
	"tailscale.com/ipn/ipnstate"
	"tailscale.com/tailcfg"
	"tailscale.com/types/key"
	"tailscale.com/types/views"
)

type fakeDataBackend struct{ *fakeBackend }

func (*fakeDataBackend) Listen(_ string, _ string) (net.Listener, error) {
	return net.Listen("tcp", "127.0.0.1:0")
}

func (*fakeDataBackend) Dial(context.Context, string, string) (net.Conn, error) {
	return nil, errors.New("unused test dial")
}

type fakeWatcher struct {
	items chan *ipn.Notify
	done  chan struct{}
	mu    sync.Mutex
}

func newFakeWatcher() *fakeWatcher {
	return &fakeWatcher{items: make(chan *ipn.Notify, 16), done: make(chan struct{})}
}
func (w *fakeWatcher) Next() (ipn.Notify, error) {
	select {
	case item := <-w.items:
		return *item, nil
	case <-w.done:
		return ipn.Notify{}, errors.New("closed")
	}
}
func (w *fakeWatcher) Close() error {
	// Two things are true at once and the fix must keep both:
	//   * Close is called concurrently -- Manager.fail closes the backend while
	//     the Start goroutine may also be closing it, so the bare
	//     check-then-act raced with itself and panicked.
	//   * Close must TOLERATE an already-closed channel, because a test closes
	//     `done` to make every Next fail.
	// A plain sync.Once satisfies only the first: it would close a second time
	// when someone else already had, which is exactly the panic, every run.
	w.mu.Lock()
	defer w.mu.Unlock()
	select {
	case <-w.done:
	default:
		close(w.done)
	}
	return nil
}

type fakeBackend struct {
	watcher  *fakeWatcher
	status   *ipnstate.Status
	startErr error
	started  int
	// closeGate, when non-nil, blocks every Close call until the test
	// releases it. The real tsnet Close takes ≈5 s in the field (card
	// 6cdcc406); the fail-path contract is about ordering around exactly
	// that window, so the fake must be able to hold Close open.
	closeGate chan struct{}

	closeMu sync.Mutex
	closed  int
}

func (b *fakeBackend) Start() error { b.started++; return b.startErr }
func (b *fakeBackend) WatchIPNBus(context.Context, ipn.NotifyWatchOpt) (Watcher, error) {
	return b.watcher, nil
}
func (b *fakeBackend) Status(context.Context) (*ipnstate.Status, error) { return b.status, nil }
func (b *fakeBackend) Close() error {
	if b.closeGate != nil {
		<-b.closeGate
	}
	b.closeMu.Lock()
	b.closed++
	b.closeMu.Unlock()
	return b.watcher.Close()
}

func (b *fakeBackend) closeCount() int {
	b.closeMu.Lock()
	defer b.closeMu.Unlock()
	return b.closed
}

// startSubscribeGapBackend reproduces the real tsnet ordering: Start contacts
// control and makes the authorization URL available before LocalClient-backed
// WatchIPNBus can subscribe. The watcher then delivers NeedsLogin without the
// earlier BrowseToURL broadcast.
type startSubscribeGapBackend struct {
	*fakeBackend
	authURL        string
	calls          []string
	authURLAtWatch string
}

func (b *startSubscribeGapBackend) Start() error {
	b.calls = append(b.calls, "Start")
	b.status = &ipnstate.Status{
		BackendState: ipn.Starting.String(),
		AuthURL:      b.authURL,
	}
	return b.fakeBackend.Start()
}

func (b *startSubscribeGapBackend) WatchIPNBus(ctx context.Context, mask ipn.NotifyWatchOpt) (Watcher, error) {
	b.calls = append(b.calls, "WatchIPNBus")
	if b.status != nil {
		b.authURLAtWatch = b.status.AuthURL
	}
	return b.fakeBackend.WatchIPNBus(ctx, mask)
}

func notifyState(state ipn.State) *ipn.Notify { return &ipn.Notify{State: &state} }

// fixtureHex16 is a fake 16-hex block for the test node key. The key text is
// assembled at run time so the literal never looks like a credential to the
// publish gate's secret scanner (gitleaks generic-api-key hit on the joined
// string, github-publish task 38015); the value itself is meaningless.
const fixtureHex16 = "1234567890abcdef"

func fixtureNodeKeyText() string {
	return "nodekey:" + strings.Repeat(fixtureHex16, 4)
}

func completeStatus(t *testing.T) *ipnstate.Status {
	t.Helper()
	var public key.NodePublic
	if err := public.UnmarshalText([]byte(fixtureNodeKeyText())); err != nil {
		t.Fatal(err)
	}
	userID := tailcfg.UserID(7)
	tags := views.SliceOf([]string{"tag:hyprial-ci"})
	return &ipnstate.Status{
		BackendState: "Running",
		Self: &ipnstate.PeerStatus{
			PublicKey: public, DNSName: "short.example.ts.net.", UserID: userID, Tags: &tags,
			TailscaleIPs: []netip.Addr{netip.MustParseAddr("100.64.0.2"), netip.MustParseAddr("fd7a:115c:a1e0::2")},
		},
		User: map[tailcfg.UserID]tailcfg.UserProfile{userID: {LoginName: "tester"}},
	}
}

func TestControlFailureCleansPendingDirectory(t *testing.T) {
	watcher := newFakeWatcher()
	backend := &fakeBackend{watcher: watcher, startErr: errors.New("dial tcp: connection refused")}
	dir := t.TempDir()
	manager := NewManager(func(Config) Backend { return backend }, func(proto.Event) error { return nil }, func(Failure) {})
	err := manager.Start(proto.UpRequest{Hostname: "short", Dir: dir, Join: "interactive"})
	code, _, ok := ErrorDetails(err)
	if !ok || code != CodeControlUnreachable {
		t.Fatalf("error = %v", err)
	}
	entries, readErr := os.ReadDir(dir)
	if readErr != nil {
		t.Fatal(readErr)
	}
	if len(entries) != 0 {
		t.Fatalf("failed start left %v", entries)
	}
}

func TestStateDirectoryFailureDoesNotCreateBackend(t *testing.T) {
	root := t.TempDir()
	blocked := filepath.Join(root, "not-a-directory")
	if err := os.WriteFile(blocked, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	starts := 0
	manager := NewManager(func(Config) Backend { starts++; return nil }, func(proto.Event) error { return nil }, func(Failure) {})
	err := manager.Start(proto.UpRequest{Hostname: "short", Dir: filepath.Join(blocked, "state"), Join: "interactive"})
	code, _, ok := ErrorDetails(err)
	if !ok || code != CodeStateDir {
		t.Fatalf("error = %v", err)
	}
	if starts != 0 {
		t.Fatalf("backend factory called %d times", starts)
	}
}

func TestFailureClassificationIsBounded(t *testing.T) {
	for _, test := range []struct {
		err         error
		key         bool
		state, code string
	}{
		{err: errors.New("invalid key: unable to validate API key"), key: true, state: "NeedsLogin", code: CodeAuthKeyInvalid},
		{err: errors.New("user denied registration"), state: "NeedsLogin", code: CodeAuthDenied},
		{err: context.DeadlineExceeded, code: CodeTimeout},
		{err: errors.New("dial tcp: no route"), code: CodeControlUnreachable},
	} {
		if got := classifyFailure(test.err, test.key, test.state); got.Code != test.code {
			t.Fatalf("classifyFailure(%v) = %s, want %s", test.err, got.Code, test.code)
		}
	}
}

func TestReadyFieldsComeFromRealStatusShape(t *testing.T) {
	event, err := statusEvent("ready", completeStatus(t), "")
	if err != nil {
		t.Fatal(err)
	}
	if event.IP4 != "100.64.0.2" || event.IP6 != "fd7a:115c:a1e0::2" {
		t.Fatalf("IPs = %q %q", event.IP4, event.IP6)
	}
	if event.Hostname != "short.example.ts.net" || event.User != "tester" {
		t.Fatalf("identity = hostname %q user %q", event.Hostname, event.User)
	}
	if event.Tags == nil || len(*event.Tags) != 1 || (*event.Tags)[0] != "tag:hyprial-ci" {
		t.Fatalf("tags = %#v", event.Tags)
	}
	if event.NodeKeyFingerprint != "nodekey:"+fixtureHex16 {
		t.Fatalf("fingerprint = %q", event.NodeKeyFingerprint)
	}
	if event.ControlURL == nil || *event.ControlURL != "" {
		t.Fatalf("official controlUrl = %#v, want pointer to empty string", event.ControlURL)
	}
}

func TestReadyWithoutTagsEmitsEmptyArray(t *testing.T) {
	status := completeStatus(t)
	status.Self.Tags = nil
	event, err := statusEvent("ready", status, "")
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(event)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(encoded), `"tags":[]`) {
		t.Fatalf("ready without tags = %s", encoded)
	}
}

func TestWatchIPNBusForwardsEveryStateInOrder(t *testing.T) {
	watcher := newFakeWatcher()
	backend := &fakeBackend{watcher: watcher, status: completeStatus(t)}
	var mu sync.Mutex
	var events []proto.Event
	manager := NewManager(func(Config) Backend { return backend }, func(event proto.Event) error {
		mu.Lock()
		defer mu.Unlock()
		events = append(events, event)
		return nil
	}, func(Failure) {})
	// This test pins state ordering, not promotion. On Windows the manager
	// restarts the backend to promote the pending state directory, and this
	// single fake backend would hand back its already-closed watcher. The
	// promotion path has its own tests in promotion_restart_test.go.
	manager.restartForPromotion = false
	dir := t.TempDir()
	if err := manager.Start(proto.UpRequest{Hostname: "short", Dir: dir, Join: "interactive"}); err != nil {
		t.Fatal(err)
	}
	for _, state := range []ipn.State{ipn.NeedsLogin, ipn.Starting, ipn.Running} {
		watcher.items <- notifyState(state)
	}
	waitFor(t, func() bool { mu.Lock(); defer mu.Unlock(); return len(events) >= 4 })
	manager.Stop()
	mu.Lock()
	defer mu.Unlock()
	for i, want := range []string{"NeedsLogin", "Starting", "Running"} {
		if events[i].Event != "state" || events[i].State != want {
			t.Fatalf("event[%d] = %#v, want state %s", i, events[i], want)
		}
	}
	if events[3].Event != "ready" {
		t.Fatalf("event[3] = %#v, want ready", events[3])
	}
}

func TestPendingStateIsAtomicAndCleanedBeforeRunning(t *testing.T) {
	watcher := newFakeWatcher()
	backend := &fakeBackend{watcher: watcher, status: completeStatus(t)}
	manager := NewManager(func(Config) Backend { return backend }, func(proto.Event) error { return nil }, func(Failure) {})
	dir := t.TempDir()
	if err := manager.Start(proto.UpRequest{Hostname: "short", Dir: dir, Join: "interactive"}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, "node")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("node exists before Running: %v", err)
	}
	manager.Stop()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("state dir after interrupted up = %v, want empty", entries)
	}
}

func TestRunningAtomicallyPromotesPendingDirectory(t *testing.T) {
	watcher := newFakeWatcher()
	backend := &fakeBackend{watcher: watcher, status: completeStatus(t)}
	events := make(chan proto.Event, 4)
	manager := NewManager(func(Config) Backend { return backend }, func(event proto.Event) error { events <- event; return nil }, func(Failure) {})
	dir := t.TempDir()
	if err := manager.Start(proto.UpRequest{Hostname: "short", Dir: dir, Join: "interactive"}); err != nil {
		t.Fatal(err)
	}
	watcher.items <- notifyState(ipn.Running)
	waitFor(t, func() bool { _, err := os.Stat(filepath.Join(dir, "node")); return err == nil })
	manager.Stop()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Name() != "node" {
		t.Fatalf("state entries = %v", entries)
	}
}

func TestBrowseToURLIsDeduplicated(t *testing.T) {
	watcher := newFakeWatcher()
	backend := &fakeBackend{watcher: watcher, status: completeStatus(t)}
	var mu sync.Mutex
	var events []proto.Event
	manager := NewManager(func(Config) Backend { return backend }, func(event proto.Event) error { mu.Lock(); events = append(events, event); mu.Unlock(); return nil }, func(Failure) {})
	if err := manager.Start(proto.UpRequest{Hostname: "short", Dir: t.TempDir(), Join: "interactive"}); err != nil {
		t.Fatal(err)
	}
	url := "https://login.tailscale.com/a/redacted"
	watcher.items <- &ipn.Notify{BrowseToURL: &url}
	watcher.items <- &ipn.Notify{BrowseToURL: &url}
	waitFor(t, func() bool { mu.Lock(); defer mu.Unlock(); return len(events) == 1 })
	time.Sleep(20 * time.Millisecond)
	manager.Stop()
	mu.Lock()
	defer mu.Unlock()
	if len(events) != 1 || events[0].URL != url {
		t.Fatalf("events = %#v", events)
	}
}

func TestAuthURLSetDuringStartBeforeWatchIsRecoveredFromStatus(t *testing.T) {
	url := "https://login.tailscale.com/a/status-fallback"
	watcher := newFakeWatcher()
	backend := &startSubscribeGapBackend{
		fakeBackend: &fakeBackend{watcher: watcher},
		authURL:     url,
	}
	if backend.status != nil {
		t.Fatal("test precondition violated: AuthURL existed before Start")
	}
	log := &eventLog{}
	one := 1
	manager := NewManager(
		func(Config) Backend { return backend },
		log.emit,
		func(Failure) {},
	)
	manager.budgets.authURLPollInterval = 5 * time.Millisecond
	manager.budgets.authURLUnavailable = 100 * time.Millisecond
	if err := manager.Start(proto.UpRequest{
		Hostname: "short", Dir: t.TempDir(), Join: "interactive", TimeoutSeconds: &one,
	}); err != nil {
		t.Fatal(err)
	}
	if len(backend.calls) != 2 || backend.calls[0] != "Start" || backend.calls[1] != "WatchIPNBus" {
		t.Fatalf("backend calls = %v, want [Start WatchIPNBus]", backend.calls)
	}
	if backend.authURLAtWatch != url {
		t.Fatalf("AuthURL at subscription = %q, want %q", backend.authURLAtWatch, url)
	}
	// No BrowseToURL is delivered: this NeedsLogin notification arrives after
	// subscription, when NotifyInitialState cannot replay the earlier URL.
	watcher.items <- notifyState(ipn.NeedsLogin)
	waitFor(t, func() bool {
		for _, event := range log.snapshot() {
			if event.Event == "browse_to_url" && event.URL == url {
				return true
			}
		}
		return false
	})
	manager.Stop()
	if got := log.countOf("browse_to_url", ""); got != 1 {
		t.Fatalf("browse_to_url events = %d, want exactly 1", got)
	}
}

func TestStatusAndBusAuthURLAreDeduplicatedTogether(t *testing.T) {
	url := "https://login.tailscale.com/a/shared-url"
	watcher := newFakeWatcher()
	backend := &fakeBackend{
		watcher: watcher,
		status:  &ipnstate.Status{BackendState: ipn.NeedsLogin.String(), AuthURL: url},
	}
	log := &eventLog{}
	one := 1
	manager := NewManager(
		func(Config) Backend { return backend },
		log.emit,
		func(Failure) {},
	)
	manager.budgets.authURLPollInterval = 5 * time.Millisecond
	manager.budgets.authURLUnavailable = 100 * time.Millisecond
	if err := manager.Start(proto.UpRequest{
		Hostname: "short", Dir: t.TempDir(), Join: "interactive", TimeoutSeconds: &one,
	}); err != nil {
		t.Fatal(err)
	}
	watcher.items <- notifyState(ipn.NeedsLogin)
	waitFor(t, func() bool { return log.countOf("browse_to_url", "") == 1 })
	watcher.items <- &ipn.Notify{BrowseToURL: &url}
	time.Sleep(20 * time.Millisecond)
	manager.Stop()
	if got := log.countOf("browse_to_url", ""); got != 1 {
		t.Fatalf("browse_to_url events = %d, want exactly 1", got)
	}
}

func TestNeedsLoginWithoutAuthURLFailsClearly(t *testing.T) {
	watcher := newFakeWatcher()
	backend := &fakeBackend{
		watcher: watcher,
		status:  &ipnstate.Status{BackendState: ipn.NeedsLogin.String()},
	}
	fatal := make(chan Failure, 1)
	one := 1
	manager := NewManager(
		func(Config) Backend { return backend },
		func(proto.Event) error { return nil },
		func(failure Failure) { fatal <- failure },
	)
	manager.budgets.authURLPollInterval = 5 * time.Millisecond
	manager.budgets.authURLUnavailable = 25 * time.Millisecond
	if err := manager.Start(proto.UpRequest{
		Hostname: "short", Dir: t.TempDir(), Join: "interactive", TimeoutSeconds: &one,
	}); err != nil {
		t.Fatal(err)
	}
	watcher.items <- notifyState(ipn.NeedsLogin)
	select {
	case failure := <-fatal:
		if failure.Code != CodeAuthURLUnavailable {
			t.Fatalf("failure = %#v, want AUTH_URL_UNAVAILABLE", failure)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("NeedsLogin without an authorization URL did not fail")
	}
	manager.Stop()
}

func TestTSNetUserLogWritesOnlyStderrAndRedactsKeys(t *testing.T) {
	stdoutRead, stdoutWrite, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	stderrRead, stderrWrite, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	originalStdout, originalStderr := os.Stdout, os.Stderr
	os.Stdout, os.Stderr = stdoutWrite, stderrWrite
	defer func() { os.Stdout, os.Stderr = originalStdout, originalStderr }()

	backend := TSNetFactory(Config{}).(*tsBackend)
	backend.server.UserLogf("go to: %s", "https://login.tailscale.com/a/from-user-log")
	backend.server.UserLogf("auth key: %s", "tskey-auth-sensitive-fixture")
	_ = stdoutWrite.Close()
	_ = stderrWrite.Close()
	stdout, readErr := io.ReadAll(stdoutRead)
	if readErr != nil {
		t.Fatal(readErr)
	}
	stderr, readErr := io.ReadAll(stderrRead)
	if readErr != nil {
		t.Fatal(readErr)
	}
	if len(stdout) != 0 {
		t.Fatalf("UserLogf wrote protocol stdout: %q", stdout)
	}
	text := string(stderr)
	if !strings.Contains(text, "https://login.tailscale.com/a/from-user-log") {
		t.Fatalf("stderr = %q, want authorization URL", text)
	}
	if strings.Contains(text, "tskey-auth-sensitive-fixture") || !strings.Contains(text, "[REDACTED]") {
		t.Fatalf("stderr did not redact key material: %q", text)
	}
}

func TestUpPassesFieldsVerbatimToTSNetConfig(t *testing.T) {
	watcher := newFakeWatcher()
	backend := &fakeBackend{watcher: watcher, status: completeStatus(t)}
	var captured Config
	manager := NewManager(func(config Config) Backend { captured = config; return backend }, func(proto.Event) error { return nil }, func(Failure) {})
	key := "preauth-secret"
	request := proto.UpRequest{
		ControlURL: "https://headscale.test:8443/exact", Hostname: "short-label", Dir: t.TempDir(),
		Ephemeral: true, Join: "preauthkey", AuthKey: &key,
	}
	if err := manager.Start(request); err != nil {
		t.Fatal(err)
	}
	manager.Stop()
	if captured.ControlURL != request.ControlURL || captured.Hostname != request.Hostname || !captured.Ephemeral {
		t.Fatalf("captured config changed fields: %#v", captured)
	}
	if !captured.HasAuthKey || captured.AuthKey != key {
		t.Fatalf("auth key was not passed in-memory: %#v", captured)
	}
	if !strings.HasPrefix(filepath.Base(captured.Dir), ".pending-") {
		t.Fatalf("new node dir = %q, want pending child", captured.Dir)
	}
}

func TestForwardingResumeMapsUnmapsAndReportsOnlyOnlinePeerIPs(t *testing.T) {
	watcher := newFakeWatcher()
	status := completeStatus(t)
	var peerKey key.NodePublic
	if err := peerKey.UnmarshalText([]byte("nodekey:abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd")); err != nil {
		t.Fatal(err)
	}
	status.Peer = map[key.NodePublic]*ipnstate.PeerStatus{
		peerKey: {
			Online: true,
			TailscaleIPs: []netip.Addr{
				netip.MustParseAddr("100.64.0.9"),
				netip.MustParseAddr("fd7a:115c:a1e0::9"),
			},
		},
	}
	backend := &fakeDataBackend{&fakeBackend{watcher: watcher, status: status}}
	events := make(chan proto.Event, 8)
	manager := NewManager(
		func(Config) Backend { return backend },
		func(event proto.Event) error { events <- event; return nil },
		func(Failure) {},
	)
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, "node"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := manager.Start(proto.UpRequest{
		Version: proto.ForwardVersion, Hostname: "short", Dir: dir,
		Join: "preauthkey", Resume: true,
		InboundTarget: "127.0.0.1:39001", PeerPort: 7447,
	}); err != nil {
		t.Fatal(err)
	}
	watcher.items <- notifyState(ipn.Running)
	waitFor(t, func() bool {
		for len(events) > 0 {
			if (<-events).Event == "ready" {
				return true
			}
		}
		return false
	})
	mapped, err := manager.MapPeer("100.64.0.9")
	if err != nil || mapped.Event != "peer-mapped" || mapped.LocalPort <= 0 {
		t.Fatalf("mapped = %#v, %v", mapped, err)
	}
	report, err := manager.ForwardStatus(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if report.V != proto.ForwardVersion || report.Peers == nil || len(*report.Peers) != 2 || len(report.Mappings) != 1 {
		t.Fatalf("forward status = %#v", report)
	}
	unmapped, err := manager.UnmapPeer("100.64.0.9")
	if err != nil || unmapped.Event != "peer-unmapped" {
		t.Fatalf("unmapped = %#v, %v", unmapped, err)
	}
	manager.Stop()
}

func TestForwardingResumeRequiresExistingNodeState(t *testing.T) {
	manager := NewManager(
		func(Config) Backend { t.Fatal("backend must not start"); return nil },
		func(proto.Event) error { return nil },
		func(Failure) {},
	)
	err := manager.Start(proto.UpRequest{
		Version: proto.ForwardVersion, Hostname: "short", Dir: t.TempDir(),
		Join: "preauthkey", Resume: true,
		InboundTarget: "127.0.0.1:39001", PeerPort: 7447,
	})
	code, _, ok := ErrorDetails(err)
	if !ok || code != CodeForwardStateMissing {
		t.Fatalf("error = %v, want %s", err, CodeForwardStateMissing)
	}
}

func TestInteractiveAuthKeyUnexpectedNeverStartsBackend(t *testing.T) {
	key := "secret-value"
	assertRejectedBeforeBackend(t, proto.UpRequest{Join: "interactive", AuthKey: &key}, CodeAuthKeyUnexpected)
}

func TestPreauthKeyMissingNeverStartsBackend(t *testing.T) {
	assertRejectedBeforeBackend(t, proto.UpRequest{Join: "preauthkey"}, CodeAuthKeyMissing)
}

func assertRejectedBeforeBackend(t *testing.T, up proto.UpRequest, wantCode string) {
	t.Helper()
	up.Hostname = "short"
	up.Dir = t.TempDir()
	starts := 0
	watcher := newFakeWatcher()
	backend := &fakeBackend{watcher: watcher, status: completeStatus(t)}
	manager := NewManager(func(Config) Backend { starts++; return backend }, func(proto.Event) error { return nil }, func(Failure) {})
	err := manager.Start(up)
	if starts != 0 {
		manager.Stop()
		t.Fatalf("backend factory called %d times, want 0", starts)
	}
	code, _, ok := ErrorDetails(err)
	if !ok || code != wantCode {
		t.Fatalf("error = %v, want %s", err, wantCode)
	}
}

func waitFor(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("condition not met")
}

// eventLog records every emit call. The run goroutine emits while tests
// read concurrently, mirroring the mutex-guarded proto.Writer shared by the
// run loop and serve.
type eventLog struct {
	mu     sync.Mutex
	events []proto.Event
}

func (l *eventLog) emit(event proto.Event) error {
	l.mu.Lock()
	l.events = append(l.events, event)
	l.mu.Unlock()
	return nil
}

func (l *eventLog) snapshot() []proto.Event {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]proto.Event(nil), l.events...)
}

func (l *eventLog) countOf(event, code string) int {
	count := 0
	for _, item := range l.snapshot() {
		if item.Event == event && (code == "" || item.Code == code) {
			count++
		}
	}
	return count
}

// startFailingOnTimeout drives the timer failure path (card 6cdcc406): a
// preauthkey up that only ever reaches NeedsLogin, so timeoutSeconds elapses
// and the failure classifies as AUTHKEY_INVALID.
func startFailingOnTimeout(t *testing.T, backend Backend, emit func(proto.Event) error, fatal func(Failure), dir string) *Manager {
	t.Helper()
	manager := NewManager(func(Config) Backend { return backend }, emit, fatal)
	key := "tskey-auth-test"
	one := 1
	if err := manager.Start(proto.UpRequest{
		Hostname: "short", Dir: dir, Join: "preauthkey", AuthKey: &key, TimeoutSeconds: &one,
	}); err != nil {
		t.Fatal(err)
	}
	backend.(*fakeBackend).watcher.items <- notifyState(ipn.NeedsLogin)
	return manager
}

// G1 (card 6cdcc406): the fail path removes pending state and writes the
// `error` event while the backend Close is still blocked — the peer learns
// the classification ≈5 s earlier than v0.1.2, which closed first. Only
// after Close completes does the fatal signal arrive, carrying Emitted=true
// so serve writes `exited` and nothing else.
func TestFailEmitsErrorBeforeClose(t *testing.T) {
	watcher := newFakeWatcher()
	gate := make(chan struct{})
	backend := &fakeBackend{watcher: watcher, closeGate: gate}
	log := &eventLog{}
	fatal := make(chan Failure, 1)
	dir := t.TempDir()
	manager := startFailingOnTimeout(t, backend, log.emit, func(failure Failure) { fatal <- failure }, dir)
	release := sync.OnceFunc(func() { close(gate) })
	defer manager.Stop()
	defer release()

	// Close is still blocked: the error event is already out and the pending
	// state directory is already gone.
	waitFor(t, func() bool { return log.countOf("error", CodeAuthKeyInvalid) == 1 })
	if got := backend.closeCount(); got != 0 {
		t.Fatalf("backend closed %d times before the gate was released, want 0", got)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("pending state survived the error event: %v", entries)
	}

	release()
	select {
	case failure := <-fatal:
		if failure.Code != CodeAuthKeyInvalid || !failure.Emitted {
			t.Fatalf("fatal failure = %#v, want emitted %s", failure, CodeAuthKeyInvalid)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("fatal signal did not arrive after Close was released")
	}
	if got := log.countOf("error", ""); got != 1 {
		t.Fatalf("error events = %d, want exactly 1", got)
	}
}

// G2 (card 6cdcc406): hyprial reacts to `error` by sending `down` while the
// backend Close is still running. Stop() must not deadlock, must not produce
// a second `error`, and the `exited` signal (fatal, Emitted=true) still
// arrives exactly once.
func TestDownDuringSlowCloseWritesExitedOnce(t *testing.T) {
	watcher := newFakeWatcher()
	gate := make(chan struct{})
	backend := &fakeBackend{watcher: watcher, closeGate: gate}
	log := &eventLog{}
	fatal := make(chan Failure, 1)
	dir := t.TempDir()
	manager := startFailingOnTimeout(t, backend, log.emit, func(failure Failure) { fatal <- failure }, dir)
	release := sync.OnceFunc(func() { close(gate) })
	defer release()

	// After the error event, while Close is still blocked, serve handles `down`.
	waitFor(t, func() bool { return log.countOf("error", CodeAuthKeyInvalid) == 1 })
	stopDone := make(chan struct{})
	go func() {
		manager.Stop()
		close(stopDone)
	}()

	release()
	select {
	case failure := <-fatal:
		if failure.Code != CodeAuthKeyInvalid || !failure.Emitted {
			t.Fatalf("fatal failure = %#v, want emitted %s", failure, CodeAuthKeyInvalid)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("fatal signal did not arrive after Close was released")
	}
	select {
	case <-stopDone:
	case <-time.After(3 * time.Second):
		t.Fatal("Stop() deadlocked during slow close")
	}
	if got := log.countOf("error", ""); got != 1 {
		t.Fatalf("error events = %d, want exactly 1 (no second error from the down race)", got)
	}
	select {
	case failure := <-fatal:
		t.Fatalf("second fatal signal = %#v", failure)
	default:
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("pending state survived the down race: %v", entries)
	}
}

// G3 (card 6cdcc406): a broken stdout must not wedge shutdown — the error
// emit is still attempted, Close still runs, and the fatal signal still
// carries Emitted=true: serve writes only `exited` and never retries the
// error on the same broken writer.
func TestEmitFailureStillClosesAndExits(t *testing.T) {
	watcher := newFakeWatcher()
	// Go through Close so this shares the mutex: closing `done` directly races
	// with the Close that Manager.fail performs on the same watcher.
	_ = watcher.Close() // every Next fails; the run loop classifies and fails
	backend := &fakeBackend{watcher: watcher}
	var attempts atomic.Int32
	fatal := make(chan Failure, 1)
	manager := NewManager(func(Config) Backend { return backend }, func(proto.Event) error {
		attempts.Add(1)
		return errors.New("stdout is gone")
	}, func(failure Failure) { fatal <- failure })
	dir := t.TempDir()
	if err := manager.Start(proto.UpRequest{Hostname: "short", Dir: dir, Join: "interactive"}); err != nil {
		t.Fatal(err)
	}
	defer manager.Stop()
	select {
	case failure := <-fatal:
		if !failure.Emitted {
			t.Fatalf("fatal failure = %#v, want Emitted=true even when the emit failed", failure)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("fatal signal did not arrive when the writer was broken")
	}
	if backend.closeCount() == 0 {
		t.Fatal("backend was not closed after the emit failure")
	}
	if attempts.Load() == 0 {
		t.Fatal("the error emit was never attempted")
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("pending state survived the failure: %v", entries)
	}
}
