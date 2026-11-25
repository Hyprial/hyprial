// Package contracttest pins the repository's contract golden files for the
// hyprial-tailcat protocol v6 (contract/sidecar-tailcat/golden/*.jsonl) to
// the proto package: every positive line must parse, every line in an
// invalid-* file must be rejected.
package contracttest

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/proto"
)

const goldenDir = "../../../../contract/sidecar-tailcat/golden"

func TestContractGolden(t *testing.T) {
	entries, err := os.ReadDir(goldenDir)
	if err != nil {
		t.Fatalf("read golden dir: %v", err)
	}
	dir := t.TempDir()
	if err := os.Chmod(dir, 0700); err != nil {
		t.Fatal(err)
	}
	target, err := json.Marshal("unix:" + filepath.Join(dir, "app.sock"))
	if err != nil {
		t.Fatal(err)
	}
	checked := 0
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".jsonl") {
			continue
		}
		data, err := os.ReadFile(filepath.Join(goldenDir, entry.Name()))
		if err != nil {
			t.Fatal(err)
		}
		negative := strings.HasPrefix(entry.Name(), "invalid-")
		for i, line := range bytes.Split(data, []byte("\n")) {
			line = bytes.TrimSpace(line)
			if len(line) == 0 {
				continue
			}
			checked++
			kind := lineKind(t, line)
			if negative {
				var err error
				if kind == "event" {
					_, err = proto.ParseEvent(line)
				} else {
					_, err = proto.ParseCommand(line)
				}
				if err == nil {
					t.Errorf("%s:%d: invalid line accepted: %s", entry.Name(), i+1, line)
				}
				continue
			}
			switch kind {
			case "command":
				// Materialize only the positive v2 fixture's directory in isolated state.
				line = bytes.ReplaceAll(line, []byte(`"unix:/run/hyprial/app.sock"`), target)
				if _, err := proto.ParseCommand(line); err != nil {
					t.Errorf("%s:%d: command rejected: %v\n%s", entry.Name(), i+1, err, line)
				}
			case "event":
				if _, err := proto.ParseEvent(line); err != nil {
					t.Errorf("%s:%d: event rejected: %v\n%s", entry.Name(), i+1, err, line)
				}
			}
		}
	}
	if checked == 0 {
		t.Fatal("no golden lines checked")
	}
	t.Logf("checked %d golden lines", checked)
}

func lineKind(t *testing.T, line []byte) string {
	t.Helper()
	var probe struct {
		Op    string `json:"op"`
		Event string `json:"event"`
	}
	if err := json.Unmarshal(line, &probe); err != nil {
		return "command" // invalid JSON still exercises the command parser
	}
	if probe.Event != "" {
		return "event"
	}
	return "command"
}

func TestGoldenPinsDERPMapFields(t *testing.T) {
	commands, err := os.ReadFile(filepath.Join(goldenDir, "commands.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(commands, []byte(`"derpMapURL":"https://derpmap.hyprial.ai/map.json"`)) ||
		!bytes.Contains(commands, []byte(`"derpMapCacheDir"`)) {
		t.Fatal("golden commands do not pin the DERP map URL and cache dir")
	}
	events, err := os.ReadFile(filepath.Join(goldenDir, "events.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(events, []byte(`"derpMapSource":"configured"`)) ||
		!bytes.Contains(events, []byte(`"derpMapURL":"https://derpmap.hyprial.ai/map.json"`)) {
		t.Fatal("golden running events do not pin the DERP map source")
	}
}
