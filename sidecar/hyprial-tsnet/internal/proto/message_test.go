package proto

import (
	"bytes"
	"encoding/json"
	"testing"
)

func TestWriterHelloIsOneJSONLine(t *testing.T) {
	var output bytes.Buffer
	if err := NewWriter(&output).Write(Hello("0.1.0", "v1.102.3")); err != nil {
		t.Fatal(err)
	}
	want := `{"v":1,"event":"hello","sidecar":"0.1.0","tailscale":"v1.102.3"}` + "\n"
	if output.String() != want {
		t.Fatalf("hello = %q, want %q", output.String(), want)
	}
}

func TestParseCommandRejectsWrongVersionAndUnknownOp(t *testing.T) {
	for _, input := range []string{`{"v":2,"op":"status"}`, `{"v":1,"op":"future"}`} {
		if _, err := ParseCommand([]byte(input)); !IsProtocolError(err) {
			t.Fatalf("ParseCommand(%s) error = %v, want protocol error", input, err)
		}
	}
}

func TestForwardingV2HelloAndPeerMappedAreExactJSONLines(t *testing.T) {
	var output bytes.Buffer
	writer := NewWriter(&output)
	if err := writer.Write(ForwardHello("dev", "v1.102.3")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Write(PeerMapped("100.64.0.2", 43123)); err != nil {
		t.Fatal(err)
	}
	want := "" +
		`{"v":2,"event":"hello","sidecar":"dev","tailscale":"v1.102.3"}` + "\n" +
		`{"v":2,"event":"peer-mapped","peer":"100.64.0.2","localPort":43123}` + "\n"
	if output.String() != want {
		t.Fatalf("forward events = %q, want %q", output.String(), want)
	}
}

func TestForwardingV2ParsesUpMapAndUnmapAsAClosedProtocol(t *testing.T) {
	up := `{"v":2,"op":"up","controlUrl":"https://head.test",` +
		`"hostname":"node-a","dir":"/state","ephemeral":false,` +
		`"join":"preauthkey","authKey":null,"resume":true,` +
		`"inboundTarget":"127.0.0.1:39001","peerPort":7447}`
	command, err := ParseForwardCommand([]byte(up))
	if err != nil {
		t.Fatal(err)
	}
	if command.Op != "up" || command.Up == nil || !command.Up.Resume {
		t.Fatalf("up command = %#v", command)
	}
	if command.Up.InboundTarget != "127.0.0.1:39001" || command.Up.PeerPort != 7447 {
		t.Fatalf("forward config = %#v", command.Up)
	}

	for _, test := range []struct {
		input string
		op    string
	}{
		{`{"v":2,"op":"map-peer","peer":"100.64.0.2"}`, "map-peer"},
		{`{"v":2,"op":"unmap-peer","peer":"100.64.0.2"}`, "unmap-peer"},
	} {
		parsed, err := ParseForwardCommand([]byte(test.input))
		if err != nil || parsed.Op != test.op || parsed.Peer != "100.64.0.2" {
			t.Fatalf("ParseForwardCommand(%s) = %#v, %v", test.input, parsed, err)
		}
	}

	for _, input := range []string{
		`{"v":1,"op":"map-peer","peer":"100.64.0.2"}`,
		`{"v":2,"op":"map-peer","peer":"100.64.0.2","extra":true}`,
		`{"v":2,"op":"future"}`,
	} {
		if _, err := ParseForwardCommand([]byte(input)); !IsProtocolError(err) {
			t.Fatalf("ParseForwardCommand(%s) error = %v, want protocol error", input, err)
		}
	}
}

func TestV1AndV2ParsersDoNotPretendToBeWireCompatible(t *testing.T) {
	if _, err := ParseCommand([]byte(`{"v":2,"op":"status"}`)); !IsProtocolError(err) {
		t.Fatalf("v1 parser accepted v2: %v", err)
	}
	if _, err := ParseForwardCommand([]byte(`{"v":1,"op":"status"}`)); !IsProtocolError(err) {
		t.Fatalf("v2 parser accepted v1: %v", err)
	}
}

func TestParseUpRequiresValidJoin(t *testing.T) {
	for _, input := range []string{
		`{"v":1,"op":"up","controlUrl":"","hostname":"short","dir":"/tmp/state","ephemeral":false,"authKey":null}`,
		`{"v":1,"op":"up","controlUrl":"","hostname":"short","dir":"/tmp/state","ephemeral":false,"join":"oidc","authKey":null}`,
	} {
		if _, err := ParseCommand([]byte(input)); !IsProtocolError(err) {
			t.Fatalf("ParseCommand(%s) error = %v, want protocol error", input, err)
		}
	}
}

func TestParseUpPreservesControlURL(t *testing.T) {
	input := `{"v":1,"op":"up","controlUrl":"https://headscale.test:8443/base","hostname":"short","dir":"/tmp/state","ephemeral":true,"join":"interactive","authKey":null}`
	command, err := ParseCommand([]byte(input))
	if err != nil {
		t.Fatal(err)
	}
	if command.Up.ControlURL != "https://headscale.test:8443/base" {
		t.Fatalf("controlUrl changed: %q", command.Up.ControlURL)
	}
	encoded, _ := json.Marshal(command.Up.ControlURL)
	if string(encoded) != `"https://headscale.test:8443/base"` {
		t.Fatalf("unexpected encoding: %s", encoded)
	}
}

// "No online peer yet" is a real answer, so the key must reach the wire as []
// even though the list is empty. A dropped key is not the same thing to a
// reader that validates presence: the daemon raised "forwarding status has
// invalid peers" on every poll and a healthy, empty mesh never came up.
func TestForwardEventCarriesAnEmptyPeerList(t *testing.T) {
	peers := []string{}
	event := ForwardState("Running")
	event.Peers = &peers

	var output bytes.Buffer
	if err := NewWriter(&output).Write(event); err != nil {
		t.Fatal(err)
	}
	want := `{"v":2,"event":"state","state":"Running","peers":[]}` + "\n"
	if output.String() != want {
		t.Fatalf("empty peers = %q, want %q", output.String(), want)
	}
}

// The same event with no Peers set at all must still omit the key: only the
// empty *list* is load-bearing, not the field's presence everywhere.
func TestForwardEventWithoutPeersOmitsTheKey(t *testing.T) {
	var output bytes.Buffer
	if err := NewWriter(&output).Write(ForwardState("NoState")); err != nil {
		t.Fatal(err)
	}
	want := `{"v":2,"event":"state","state":"NoState"}` + "\n"
	if output.String() != want {
		t.Fatalf("absent peers = %q, want %q", output.String(), want)
	}
}
