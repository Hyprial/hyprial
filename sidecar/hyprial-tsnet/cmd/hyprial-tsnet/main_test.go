package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"
)

const buildinfoPath = "code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tsnet/internal/buildinfo"

func TestTerminationContextHelper(t *testing.T) {
	if os.Getenv("HYPRIAL_TERMINATION_CONTEXT_HELPER") == "" {
		return
	}
	ctx, stop := terminationContext()
	defer stop()
	fmt.Println("termination-context-ready")
	<-ctx.Done()
}

func TestTerminationContextHandlesSIGTERM(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("SIGTERM is a Unix process contract")
	}
	command := exec.Command(os.Args[0], "-test.run=^TestTerminationContextHelper$")
	command.Env = append(os.Environ(), "HYPRIAL_TERMINATION_CONTEXT_HELPER=1")
	stdout, err := command.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	ready := make(chan string, 1)
	go func() {
		line, _ := bufio.NewReader(stdout).ReadString('\n')
		ready <- strings.TrimSpace(line)
	}()
	select {
	case line := <-ready:
		if line != "termination-context-ready" {
			t.Fatalf("helper readiness = %q", line)
		}
	case <-time.After(3 * time.Second):
		_ = command.Process.Kill()
		t.Fatal("signal helper did not become ready")
	}
	if err := command.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	if err := command.Wait(); err != nil {
		t.Fatalf("signal helper did not exit cleanly: %v", err)
	}
}

func versionOf(t *testing.T, args ...string) map[string]string {
	t.Helper()
	output, err := exec.Command(args[0], args[1:]...).Output()
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]string
	if err := json.Unmarshal(output, &got); err != nil {
		t.Fatal(err)
	}
	return got
}

func TestForwardSubcommandSpeaksProtocolV2(t *testing.T) {
	command := exec.Command("go", "run", ".", "forward")
	command.Stdin = strings.NewReader("{\"v\":1,\"op\":\"status\"}\n")
	output, err := command.Output()
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(output)), "\n")
	if len(lines) != 3 || !strings.Contains(lines[0], `"v":2`) ||
		!strings.Contains(lines[0], `"event":"hello"`) ||
		!strings.Contains(lines[1], `"code":"PROTOCOL"`) ||
		!strings.Contains(lines[2], `"event":"exited"`) {
		t.Fatalf("forward output = %q", output)
	}
}

// A build without -X must call itself "dev" and must never look like a release.
func TestVersionDefaultsToDevWithoutInjection(t *testing.T) {
	got := versionOf(t, "go", "run", ".", "version")
	if got["sidecar"] != "dev" || got["tailscale"] != "v1.102.3" {
		t.Fatalf("version = %#v", got)
	}
}

// The release job injects the tag; the binary must report exactly that value.
// This fails if SidecarVersion becomes a const again (-X is then ignored).
func TestVersionIsInjectedFromLdflags(t *testing.T) {
	binary := filepath.Join(t.TempDir(), "hyprial-tsnet")
	if runtime.GOOS == "windows" {
		binary += ".exe"
	}
	build := exec.Command("go", "build", "-trimpath",
		"-ldflags", "-X "+buildinfoPath+".SidecarVersion=9.9.9-test",
		"-o", binary, ".")
	if output, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, output)
	}
	got := versionOf(t, binary, "version")
	if got["sidecar"] != "9.9.9-test" {
		t.Fatalf("injected version not reported: %#v", got)
	}
}
