package serve

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tsnet/internal/node"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tsnet/internal/proto"
	"tailscale.com/ipn"
	"tailscale.com/ipn/ipnstate"
)

func TestRunEmitsHelloBeforeProtocolError(t *testing.T) {
	var output bytes.Buffer
	err := Run(context.Background(), strings.NewReader("{\"v\":2,\"op\":\"status\"}\n"), &output, func(node.Config) node.Backend {
		t.Fatal("backend must not be created for a protocol error")
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(output.String()), "\n")
	if len(lines) != 3 {
		t.Fatalf("events = %q", output.String())
	}
	var events []map[string]any
	for _, line := range lines {
		var event map[string]any
		if err := json.Unmarshal([]byte(line), &event); err != nil {
			t.Fatal(err)
		}
		events = append(events, event)
	}
	if events[0]["event"] != "hello" || events[1]["code"] != "PROTOCOL" || events[2]["event"] != "exited" {
		t.Fatalf("events = %#v", events)
	}
}

func TestRunForwardingUsesV2AndRejectsV1BeforeBackend(t *testing.T) {
	var output bytes.Buffer
	err := RunForwarding(
		context.Background(),
		strings.NewReader("{\"v\":1,\"op\":\"status\"}\n"),
		&output,
		func(node.Config) node.Backend {
			t.Fatal("backend must not be created for a protocol error")
			return nil
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(output.String()), "\n")
	if len(lines) != 3 || !strings.Contains(lines[0], `"v":2`) ||
		!strings.Contains(lines[0], `"event":"hello"`) ||
		!strings.Contains(lines[1], `"code":"PROTOCOL"`) ||
		!strings.Contains(lines[2], `"event":"exited"`) {
		t.Fatalf("forwarding events = %q", output.String())
	}
}

func TestAuthKeyUnexpectedDoesNotLeakOrCreateBackend(t *testing.T) {
	key := "tskey-auth-super-secret-test-value"
	dir := t.TempDir()
	input := `{"v":1,"op":"up","controlUrl":"","hostname":"short","dir":"` + jsonText(dir) + `","ephemeral":false,"join":"interactive","authKey":"` + key + `"}` + "\n"
	starts := 0
	var output bytes.Buffer
	if err := Run(context.Background(), strings.NewReader(input), &output, func(node.Config) node.Backend {
		starts++
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if starts != 0 {
		t.Fatalf("backend factory called %d times", starts)
	}
	if strings.Contains(output.String(), key) {
		t.Fatal("auth key leaked to stdout")
	}
	if !strings.Contains(output.String(), `"code":"AUTHKEY_UNEXPECTED"`) {
		t.Fatalf("output = %s", output.String())
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("state dir = %v, want empty", entries)
	}
}

type rejectingBackend struct{}

func (*rejectingBackend) Start() error { return errors.New("backend should not start") }
func (*rejectingBackend) WatchIPNBus(context.Context, ipn.NotifyWatchOpt) (node.Watcher, error) {
	return nil, errors.New("backend should not watch")
}
func (*rejectingBackend) Status(context.Context) (*ipnstate.Status, error) {
	return nil, errors.New("backend should not report status")
}
func (*rejectingBackend) Close() error { return nil }

func TestMissingJoinDoesNotCreateBackend(t *testing.T) {
	dir := t.TempDir()
	input := `{"v":1,"op":"up","controlUrl":"","hostname":"short","dir":"` + jsonText(dir) + `","ephemeral":false,"authKey":null}` + "\n"
	starts := 0
	var output bytes.Buffer
	if err := Run(context.Background(), strings.NewReader(input), &output, func(node.Config) node.Backend {
		starts++
		return &rejectingBackend{}
	}); err != nil {
		t.Fatal(err)
	}
	if starts != 0 {
		t.Fatalf("backend factory called %d times", starts)
	}
	if !strings.Contains(output.String(), `"code":"PROTOCOL"`) {
		t.Fatalf("output = %s", output.String())
	}
}

type slowCloseWatcher struct {
	items chan *ipn.Notify
	done  chan struct{}
}

func (w *slowCloseWatcher) Next() (ipn.Notify, error) {
	select {
	case item := <-w.items:
		return *item, nil
	case <-w.done:
		return ipn.Notify{}, errors.New("closed")
	}
}

func (w *slowCloseWatcher) Close() error {
	select {
	case <-w.done:
	default:
		close(w.done)
	}
	return nil
}

// slowCloseBackend mirrors the real tsnet Close (≈5 s in the field, card
// 6cdcc406): Close blocks until the test releases the gate.
type slowCloseBackend struct {
	watcher *slowCloseWatcher
	gate    chan struct{}
}

func (*slowCloseBackend) Start() error { return nil }
func (b *slowCloseBackend) WatchIPNBus(context.Context, ipn.NotifyWatchOpt) (node.Watcher, error) {
	return b.watcher, nil
}
func (*slowCloseBackend) Status(context.Context) (*ipnstate.Status, error) {
	return nil, errors.New("no status")
}
func (b *slowCloseBackend) Close() error {
	<-b.gate
	return b.watcher.Close()
}

// Card 6cdcc406: on the real wire the `error` line must appear while the
// slow backend Close is still running, `exited` follows exactly once after
// Close, and the fatal branch never writes a second `error`.
func TestRunWritesErrorBeforeSlowCloseAndExitedOnce(t *testing.T) {
	stdin, stdinWriter := io.Pipe()
	stdout, stdoutWriter := io.Pipe()
	backend := &slowCloseBackend{
		watcher: &slowCloseWatcher{items: make(chan *ipn.Notify, 4), done: make(chan struct{})},
		gate:    make(chan struct{}),
	}
	release := sync.OnceFunc(func() { close(backend.gate) })
	defer release()
	defer stdinWriter.Close()
	defer stdoutWriter.Close()
	dir := t.TempDir()
	key := "tskey-auth-test"
	up := `{"v":1,"op":"up","controlUrl":"","hostname":"short","dir":"` + jsonText(dir) + `","ephemeral":false,"join":"preauthkey","authKey":"` + key + `","timeoutSeconds":1}` + "\n"

	lines := make(chan string, 8)
	go func() {
		scanner := bufio.NewScanner(stdout)
		for scanner.Scan() {
			lines <- scanner.Text()
		}
	}()

	runErr := make(chan error, 1)
	go func() {
		runErr <- Run(context.Background(), stdin, stdoutWriter, func(node.Config) node.Backend { return backend })
	}()
	if _, err := stdinWriter.Write([]byte(up)); err != nil {
		t.Fatal(err)
	}
	backend.watcher.items <- &ipn.Notify{State: &[]ipn.State{ipn.NeedsLogin}[0]}

	readLine := func(what string) string {
		select {
		case line := <-lines:
			return line
		case <-time.After(4 * time.Second):
			t.Fatalf("no %s line while the close gate is still held", what)
			return ""
		}
	}

	if line := readLine("hello"); !strings.Contains(line, `"event":"hello"`) {
		t.Fatalf("hello line = %s", line)
	}
	if line := readLine("state"); !strings.Contains(line, `"state":"NeedsLogin"`) {
		t.Fatalf("state line = %s", line)
	}
	// The gate is still held: the error line must already be on the wire.
	errorLine := readLine("error")
	if !strings.Contains(errorLine, `"code":"AUTHKEY_INVALID"`) {
		t.Fatalf("error line = %s", errorLine)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("pending state survived the error line: %v", entries)
	}

	release()
	exitedLine := readLine("exited")
	if !strings.Contains(exitedLine, `"event":"exited"`) {
		t.Fatalf("exited line = %s", exitedLine)
	}
	select {
	case err := <-runErr:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return after exited")
	}
	// Drain whatever else arrived: no second error, no second exited.
	_ = stdinWriter.Close()
	for {
		select {
		case line := <-lines:
			if strings.Contains(line, `"event":"error"`) || strings.Contains(line, `"event":"exited"`) {
				t.Fatalf("duplicate terminal line after exited: %s", line)
			}
			continue
		case <-time.After(500 * time.Millisecond):
		}
		break
	}
}

// jsonText is s escaped for use inside a JSON string literal. The request
// lines here are built by concatenation, and a Windows temp dir
// (C:\Users\…) is not valid JSON unescaped: the sidecar then rightly refuses
// the line as "invalid protocol-v1 request" (tsnet-windows.yml run 36390125901).
func jsonText(s string) string {
	quoted, err := json.Marshal(s)
	if err != nil {
		panic(err)
	}
	return string(quoted[1 : len(quoted)-1])
}

func TestRequestLinesStayValidJSONForWindowsPaths(t *testing.T) {
	dir := `C:\Users\runneradmin\AppData\Local\Temp\TestX\001`
	line := `{"v":1,"op":"up","controlUrl":"","hostname":"short","dir":"` + jsonText(dir) + `","ephemeral":false,"join":"interactive","authKey":null}`
	command, err := proto.ParseCommand([]byte(line))
	if err != nil {
		t.Fatalf("ParseCommand(%s): %v", line, err)
	}
	if command.Up == nil || command.Up.Dir != dir {
		t.Fatalf("up = %#v, want dir %q", command.Up, dir)
	}
}
