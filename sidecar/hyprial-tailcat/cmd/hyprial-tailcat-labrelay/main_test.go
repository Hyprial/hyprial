package main

import (
	"bytes"
	"encoding/json"
	"io"
	"reflect"
	"strings"
	"testing"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/labrelay"
)

func TestParseOptionsDefaultsToLoopback(t *testing.T) {
	got, err := parseOptions(nil, io.Discard)
	if err != nil {
		t.Fatalf("parseOptions: %v", err)
	}
	want := labrelay.Options{BindAddress: "127.0.0.1", AdvertiseHost: "127.0.0.1"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("parseOptions = %#v, want %#v", got, want)
	}
}

func TestParseOptionsAcceptsBindAndAdvertiseHost(t *testing.T) {
	got, err := parseOptions(
		[]string{"--bind", "0.0.0.0", "--advertise-host", "relay.example"},
		io.Discard,
	)
	if err != nil {
		t.Fatalf("parseOptions: %v", err)
	}
	want := labrelay.Options{BindAddress: "0.0.0.0", AdvertiseHost: "relay.example"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("parseOptions = %#v, want %#v", got, want)
	}
}

func TestRunPrintsUnchangedJSONShape(t *testing.T) {
	var stdout bytes.Buffer
	if err := run(nil, strings.NewReader(""), &stdout, io.Discard); err != nil {
		t.Fatalf("run: %v", err)
	}

	var output map[string]json.RawMessage
	if err := json.Unmarshal(stdout.Bytes(), &output); err != nil {
		t.Fatalf("decode output: %v", err)
	}
	if len(output) != 2 || output["v"] == nil || output["derp"] == nil {
		t.Fatalf("top-level JSON keys = %v, want exactly [derp v]", mapKeys(output))
	}
	var version int
	if err := json.Unmarshal(output["v"], &version); err != nil || version != 3 {
		t.Fatalf("v = %d, err = %v, want 3", version, err)
	}
	var config struct {
		Host string `json:"host"`
	}
	if err := json.Unmarshal(output["derp"], &config); err != nil {
		t.Fatalf("decode derp: %v", err)
	}
	if config.Host != "127.0.0.1" {
		t.Fatalf("derp.host = %q, want 127.0.0.1", config.Host)
	}
}

func TestRunUsesAdvertisedHost(t *testing.T) {
	var stdout bytes.Buffer
	args := []string{"--bind", "0.0.0.0", "--advertise-host", "relay.example"}
	if err := run(args, strings.NewReader(""), &stdout, io.Discard); err != nil {
		t.Fatalf("run: %v", err)
	}

	var output struct {
		DERP struct {
			Host string `json:"host"`
		} `json:"derp"`
	}
	if err := json.Unmarshal(stdout.Bytes(), &output); err != nil {
		t.Fatalf("decode output: %v", err)
	}
	if output.DERP.Host != "relay.example" {
		t.Fatalf("derp.host = %q, want relay.example", output.DERP.Host)
	}
}

func mapKeys(values map[string]json.RawMessage) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	return keys
}
