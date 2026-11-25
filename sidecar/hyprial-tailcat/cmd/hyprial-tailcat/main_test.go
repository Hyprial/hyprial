package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestVersion pins the version subcommand's wire shape.
func TestVersion(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := run([]string{"version"}, strings.NewReader(""), &stdout, &stderr); code != 0 {
		t.Fatalf("version exit = %d, stderr %q", code, stderr.String())
	}
	var out map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(stdout.Bytes()), &out); err != nil {
		t.Fatalf("version output: %v", err)
	}
	if out["v"] != float64(6) || out["sidecar"] == "" ||
		out["tailcat"] != "b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f" {
		t.Fatalf("version output = %v", out)
	}
}

// TestGenkey covers genkey's exit codes, file mode, and the rule that
// stdout carries only public keys.
func TestGenkey(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "dev.key")

	var stdout, stderr bytes.Buffer
	if code := run([]string{"genkey", "--out", path}, strings.NewReader(""), &stdout, &stderr); code != 0 {
		t.Fatalf("genkey exit = %d, stderr %q", code, stderr.String())
	}
	out := bytes.TrimSpace(stdout.Bytes())
	var parsed map[string]any
	if err := json.Unmarshal(out, &parsed); err != nil {
		t.Fatalf("genkey output: %v", err)
	}
	serverPublic, _ := parsed["serverPublic"].(string)
	clientPublic, _ := parsed["clientPublic"].(string)
	if !strings.HasPrefix(serverPublic, "nodekey:") || !strings.HasPrefix(clientPublic, "nodekey:") {
		t.Fatalf("genkey output = %s", out)
	}
	// stdout must never carry private keys or the PSK.
	if bytes.Contains(out, []byte("privkey:")) || bytes.Contains(out, []byte("psk:")) {
		t.Fatalf("genkey leaked secret material: %s", out)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if mode := info.Mode().Perm(); mode != 0600 {
		t.Fatalf("key file mode = %o, want 0600", mode)
	}

	// Existing file without --force: exit code 3, file untouched, and the
	// same public keys printed again (login/republish re-run genkey).
	stdout.Reset()
	if code := run([]string{"genkey", "--out", path}, strings.NewReader(""), &stdout, &stderr); code != 3 {
		t.Fatalf("second genkey exit = %d, want 3", code)
	}
	var kept map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(stdout.Bytes()), &kept); err != nil {
		t.Fatalf("second genkey output: %v", err)
	}
	if kept["serverPublic"] != serverPublic || kept["clientPublic"] != clientPublic {
		t.Fatalf("second genkey printed different keys: %v", kept)
	}
	if bytes.Contains(stdout.Bytes(), []byte("privkey:")) || bytes.Contains(stdout.Bytes(), []byte("psk:")) {
		t.Fatalf("second genkey leaked secret material: %s", stdout.String())
	}
	var parsedAgain map[string]any
	stdout.Reset()
	if code := run([]string{"genkey", "--out", path, "--force"}, strings.NewReader(""), &stdout, &stderr); code != 0 {
		t.Fatalf("forced genkey exit = %d", code)
	}
	if err := json.Unmarshal(bytes.TrimSpace(stdout.Bytes()), &parsedAgain); err != nil {
		t.Fatal(err)
	}
	if parsedAgain["serverPublic"] == serverPublic {
		t.Fatal("--force kept the old server identity")
	}

	// Bad usage.
	if code := run([]string{"genkey"}, strings.NewReader(""), &stdout, &stderr); code != 2 {
		t.Fatalf("genkey without --out exit = %d, want 2", code)
	}
	if code := run([]string{"genkey", "--out", "relative.key"}, strings.NewReader(""), &stdout, &stderr); code != 2 {
		t.Fatalf("genkey with relative path exit = %d, want 2", code)
	}
	if code := run([]string{"bogus"}, strings.NewReader(""), &stdout, &stderr); code != 2 {
		t.Fatalf("unknown subcommand exit = %d, want 2", code)
	}
	if code := run(nil, strings.NewReader(""), &stdout, &stderr); code != 2 {
		t.Fatalf("no subcommand exit = %d, want 2", code)
	}
}
