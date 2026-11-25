package engine

import (
	"context"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/keys"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/labrelay"
)

func writeKeyFile(t *testing.T, dir, name string) (string, keys.Material) {
	t.Helper()
	path := filepath.Join(dir, name)
	if _, err := keys.Generate(path, false); err != nil {
		t.Fatalf("Generate %s: %v", name, err)
	}
	material, err := keys.Load(path)
	if err != nil {
		t.Fatalf("Load %s: %v", name, err)
	}
	return path, material
}

func derivedClientPublic(t *testing.T, client, server keys.Material) string {
	t.Helper()
	derived, err := keys.DeriveClientPrivate(client.ClientPrivate, server.ServerPublic())
	if err != nil {
		t.Fatal(err)
	}
	return derived.Public().String()
}

func echoServer(t *testing.T) string {
	t.Helper()
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
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
	t.Cleanup(func() { _ = listener.Close() })
	return listener.Addr().String()
}

func upConfig(keyFile, addressFile string, relay *labrelay.Relay, allow []string, allowAny bool, inboundTarget string) UpConfig {
	derp := relay.Config()
	return UpConfig{
		KeyFile:       keyFile,
		Allow:         allow,
		AllowAny:      allowAny,
		InboundTarget: inboundTarget,
		PeerPort:      7447,
		AddressFile:   addressFile,
		DERP: &DERPOverride{
			Host:             derp.Host,
			DERPPort:         derp.DERPPort,
			STUNPort:         derp.STUNPort,
			InsecureForTests: derp.InsecureForTests,
		},
	}
}

func readAddress(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(string(data))
}

// echoRoundTrip writes payload to a loopback port and expects it echoed
// back within timeout.
func echoRoundTrip(port int, payload string, timeout time.Duration) error {
	conn, err := net.DialTimeout("tcp", "127.0.0.1:"+itoa(port), timeout)
	if err != nil {
		return err
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(timeout))
	if _, err := conn.Write([]byte(payload)); err != nil {
		return err
	}
	buf := make([]byte, len(payload))
	if _, err := io.ReadFull(conn, buf); err != nil {
		return err
	}
	if string(buf) != payload {
		return errors.New("echo payload mismatch")
	}
	return nil
}

func itoa(v int) string { return strconv.Itoa(v) }

func poll(t *testing.T, timeout time.Duration, what string, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if ok() {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

// growPeerSlot maps the peer onto a fresh loopback tcp slot (the grow
// spec) and returns the mapping plus the kernel-assigned port the test
// dials -- v4 has no self-allocated localPort shape.
func growPeerSlot(t *testing.T, eng *Engine, peer, address string, port int) (Mapping, int) {
	t.Helper()
	mapping, err := eng.MapPeer(peer, address, port, "tcp:127.0.0.1:0")
	if err != nil {
		t.Fatalf("map-peer %s: %v", peer, err)
	}
	return mapping, slotPortNumber(t, mapping.Slot)
}

func slotPortNumber(t *testing.T, slot string) int {
	t.Helper()
	address, err := netip.ParseAddrPort(strings.TrimPrefix(slot, "tcp:"))
	if err != nil {
		t.Fatalf("slot %q is not a resolved tcp slot", slot)
	}
	return int(address.Port())
}

// TestMapPeerDataPlane is the positive integration path: A exposes its
// inbound target (a local TCP echo) on peerPort 7447, B maps A, and data
// flows through the local forwarding port.
func TestMapPeerDataPlane(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	keyA, materialA := writeKeyFile(t, dir, "a.key")
	keyB, materialB := writeKeyFile(t, dir, "b.key")
	echo := echoServer(t)

	engA := &Engine{}
	if _, _, err := engA.Up(context.Background(), upConfig(
		keyA, filepath.Join(dir, "a.addr"), relay,
		[]string{derivedClientPublic(t, materialB, materialA)}, false, "tcp:"+echo,
	)); err != nil {
		t.Fatalf("A up: %v", err)
	}
	defer engA.Down()

	engB := &Engine{}
	if _, _, err := engB.Up(context.Background(), upConfig(
		keyB, filepath.Join(dir, "b.addr"), relay, nil, false, "",
	)); err != nil {
		t.Fatalf("B up: %v", err)
	}
	defer engB.Down()

	// The address file is the only place the secret address persists.
	addressA := readAddress(t, filepath.Join(dir, "a.addr"))
	if !strings.HasPrefix(addressA, "tc") {
		t.Fatalf("address file does not hold a tailcat address")
	}
	info, _ := os.Stat(filepath.Join(dir, "a.addr"))
	if mode := info.Mode().Perm(); mode != 0600 {
		t.Fatalf("addressFile mode = %o, want 0600", mode)
	}

	mapping, localPort := growPeerSlot(t, engB, "dev-a", addressA, 7447)
	// Idempotent: re-asserting the resolved slot returns the same mapping.
	again, err := engB.MapPeer("dev-a", addressA, 7447, mapping.Slot)
	if err != nil || again != mapping {
		t.Fatalf("idempotent map-peer = %+v, %v; want %+v", again, err, mapping)
	}

	poll(t, 30*time.Second, "echo through mapped peer", func() bool {
		return echoRoundTrip(localPort, "ping-tailcat", 5*time.Second) == nil
	})

	state, _, _, peers, _ := engB.Status()
	if state != "Running" || len(peers) != 1 || peers[0].Peer != "dev-a" {
		t.Fatalf("B status = %q %+v", state, peers)
	}
	poll(t, 5*time.Second, "mapped connection telemetry", func() bool {
		_, _, _, _, telemetry := engB.Status()
		return len(telemetry.Slots) == 1 &&
			telemetry.Slots[0].Peer == "dev-a" &&
			telemetry.Slots[0].Slot == mapping.Slot &&
			telemetry.Slots[0].MapPort != nil &&
			*telemetry.Slots[0].MapPort == localPort &&
			len(telemetry.Slots[0].Recent) > 0
	})

	// Unmap returns the slot to hold mode: it still accepts, but no
	// longer forwards, so the round trip times out unanswered.
	if err := engB.UnmapPeer("dev-a"); err != nil {
		t.Fatalf("unmap-peer: %v", err)
	}
	if err := echoRoundTrip(localPort, "after-unmap", 500*time.Millisecond); err == nil {
		t.Fatal("mapped port still serves after unmap-peer")
	}
	if err := engB.UnmapPeer("dev-a"); !errors.Is(err, ErrPeerUnknown) {
		t.Fatalf("second unmap = %v, want ErrPeerUnknown", err)
	}
}

// TestAdmissionDeny: A's allow set does not contain B's client key and
// allowAny is false. The tunnel handshake now admits every key (design
// §3.3), but the zenoh inbound gate closes B's connection, so traffic
// through the mapped port fails and the denied counter records the
// per-connection rejection — while an admission-any exposure on the
// same node stays reachable for the very same key.
func TestAdmissionDeny(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	keyA, _ := writeKeyFile(t, dir, "a.key")
	keyB, _ := writeKeyFile(t, dir, "b.key")
	echo := echoServer(t)
	joinEcho := echoServer(t)

	engA := &Engine{}
	if _, _, err := engA.Up(context.Background(), upConfig(
		keyA, filepath.Join(dir, "a.addr"), relay, nil, false, "tcp:"+echo,
	)); err != nil {
		t.Fatalf("A up: %v", err)
	}
	defer engA.Down()
	if _, err := engA.Expose(context.Background(), Exposure{
		Port: 8450, Target: "tcp:" + joinEcho, Admission: AdmissionAny,
	}); err != nil {
		t.Fatalf("expose join: %v", err)
	}

	engB := &Engine{}
	if _, _, err := engB.Up(context.Background(), upConfig(
		keyB, filepath.Join(dir, "b.addr"), relay, nil, false, "",
	)); err != nil {
		t.Fatalf("B up: %v", err)
	}
	defer engB.Down()

	addressA := readAddress(t, filepath.Join(dir, "a.addr"))
	_, deniedPort := growPeerSlot(t, engB, "dev-a", addressA, 7447)
	if err := echoRoundTrip(deniedPort, "must-fail", 8*time.Second); err == nil {
		t.Fatal("echo succeeded for a denied client")
	}
	poll(t, 10*time.Second, "admission denial callback", func() bool {
		return engA.DeniedCount() > 0
	})

	// The same non-allowed key reaches the admission-any exposure.
	_, joinPort := growPeerSlot(t, engB, "dev-a-join", addressA, 8450)
	poll(t, 30*time.Second, "echo through admission-any exposure", func() bool {
		return echoRoundTrip(joinPort, "join-ok", 5*time.Second) == nil
	})
}

// dialEcho opens a persistent connection to a loopback port and proves
// one echo round trip on it.
func dialEcho(t *testing.T, port int, payload string) net.Conn {
	t.Helper()
	var conn net.Conn
	poll(t, 30*time.Second, "echo on persistent connection", func() bool {
		candidate, err := net.DialTimeout("tcp", "127.0.0.1:"+itoa(port), 5*time.Second)
		if err != nil {
			return false
		}
		if err := echoOnce(candidate, payload, 5*time.Second); err != nil {
			_ = candidate.Close()
			return false
		}
		conn = candidate
		return true
	})
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

// echoOnce writes payload on an established connection and expects it
// echoed back within timeout.
func echoOnce(conn net.Conn, payload string, timeout time.Duration) error {
	_ = conn.SetDeadline(time.Now().Add(timeout))
	if _, err := conn.Write([]byte(payload)); err != nil {
		return err
	}
	buf := make([]byte, len(payload))
	if _, err := io.ReadFull(conn, buf); err != nil {
		return err
	}
	if string(buf) != payload {
		return errors.New("echo payload mismatch")
	}
	return nil
}

// TestAllowRevokeClosesZenohKeepsAnyService: B connects while
// allowAny=true to the zenoh route, a restricted exposure, and an
// admission-any exposure. Revoking every key closes B's zenoh and
// restricted connections but keeps the admission-any connection alive
// (design §3.3: a joiner connected only to the join endpoint is not cut
// off); re-admitting B restores the restricted routes without a remap
// because the tunnel itself was never disconnected.
func TestAllowRevokeClosesZenohKeepsAnyService(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	keyA, materialA := writeKeyFile(t, dir, "a.key")
	keyB, materialB := writeKeyFile(t, dir, "b.key")
	echo := echoServer(t)
	joinEcho := echoServer(t)
	restrictedEcho := echoServer(t)

	engA := &Engine{}
	if _, _, err := engA.Up(context.Background(), upConfig(
		keyA, filepath.Join(dir, "a.addr"), relay, nil, true, "tcp:"+echo,
	)); err != nil {
		t.Fatalf("A up: %v", err)
	}
	defer engA.Down()
	if _, err := engA.Expose(context.Background(), Exposure{
		Port: 8450, Target: "tcp:" + joinEcho, Admission: AdmissionAny,
	}); err != nil {
		t.Fatalf("expose join: %v", err)
	}
	if _, err := engA.Expose(context.Background(), Exposure{
		Port: 8451, Target: "tcp:" + restrictedEcho, Admission: "allow",
	}); err != nil {
		t.Fatalf("expose restricted: %v", err)
	}

	engB := &Engine{}
	if _, _, err := engB.Up(context.Background(), upConfig(
		keyB, filepath.Join(dir, "b.addr"), relay, nil, false, "",
	)); err != nil {
		t.Fatalf("B up: %v", err)
	}
	defer engB.Down()

	addressA := readAddress(t, filepath.Join(dir, "a.addr"))
	_, zenohPort := growPeerSlot(t, engB, "dev-a", addressA, 7447)
	_, joinPort := growPeerSlot(t, engB, "dev-a-join", addressA, 8450)
	_, restrictedPort := growPeerSlot(t, engB, "dev-a-restricted", addressA, 8451)
	zenohConn := dialEcho(t, zenohPort, "zenoh-before")
	joinConn := dialEcho(t, joinPort, "join-before")
	restrictedConn := dialEcho(t, restrictedPort, "restricted-before")

	count, err := engA.Allow([]string{}, false)
	if err != nil || count != 0 {
		t.Fatalf("allow = %d, %v", count, err)
	}
	poll(t, 10*time.Second, "revoked connections closed", func() bool {
		return engA.DisconnectedCount() >= 2
	})
	poll(t, 10*time.Second, "zenoh connection dead after revoke", func() bool {
		return echoOnce(zenohConn, "zenoh-after", 2*time.Second) != nil
	})
	poll(t, 10*time.Second, "restricted connection dead after revoke", func() bool {
		return echoOnce(restrictedConn, "restricted-after", 2*time.Second) != nil
	})
	// The admission-any connection survives the revocation.
	if err := echoOnce(joinConn, "join-after-revoke", 8*time.Second); err != nil {
		t.Fatalf("admission-any connection died on revoke: %v", err)
	}
	// New zenoh connections from the revoked key are rejected too.
	if err := echoRoundTrip(zenohPort, "zenoh-new", 8*time.Second); err == nil {
		t.Fatal("new zenoh connection succeeded for a revoked key")
	}
	// New admission-any connections still work for the revoked key.
	poll(t, 30*time.Second, "new join connection after revoke", func() bool {
		return echoRoundTrip(joinPort, "join-new", 5*time.Second) == nil
	})

	// Re-admitting B restores zenoh and the restricted exposure on the
	// same mappings: the tunnel was never torn down.
	if _, err := engA.Allow(
		[]string{derivedClientPublic(t, materialB, materialA)}, false,
	); err != nil {
		t.Fatalf("re-allow: %v", err)
	}
	poll(t, 30*time.Second, "zenoh echo after re-admit", func() bool {
		return echoRoundTrip(zenohPort, "zenoh-readmit", 5*time.Second) == nil
	})
	poll(t, 30*time.Second, "restricted echo after re-admit", func() bool {
		return echoRoundTrip(restrictedPort, "restricted-readmit", 5*time.Second) == nil
	})
}

// TestExposeProxyV2AndPeerKey: A exposes port 8447 to a local target with
// PROXY v2; B connects through a mapping to that port. The target reads
// the header: source is B's tailcat IP, TLV 0xE0 is B's client nodekey,
// and Server.PeerKey (via engine.PeerKey) resolves the source address.
// Regression for design §3.3: the exposure runs with the default
// restricted admission, so the TLV carries exactly the authenticated
// peer key that passed the per-connection gate.
func TestExposeProxyV2AndPeerKey(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	keyA, materialA := writeKeyFile(t, dir, "a.key")
	keyB, materialB := writeKeyFile(t, dir, "b.key")
	derivedB := derivedClientPublic(t, materialB, materialA)

	// Target records the PROXY v2 header on a private Unix socket.
	headerCh := make(chan []byte, 1)
	target, err := net.Listen("unix", filepath.Join(shortSlotDir(t), "backend.sock"))
	if err != nil {
		t.Fatal(err)
	}
	defer target.Close()
	go func() {
		for {
			conn, err := target.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				_ = conn.SetDeadline(time.Now().Add(10 * time.Second))
				fixed := make([]byte, 16)
				if _, err := io.ReadFull(conn, fixed); err != nil {
					return
				}
				length := int(binary.BigEndian.Uint16(fixed[14:16]))
				payload := make([]byte, length)
				if _, err := io.ReadFull(conn, payload); err != nil {
					return
				}
				headerCh <- append(fixed, payload...)
				_, _ = io.Copy(conn, conn)
			}()
		}
	}()

	engA := &Engine{}
	if _, _, err := engA.Up(context.Background(), upConfig(
		keyA, filepath.Join(dir, "a.addr"), relay,
		[]string{derivedB}, false, "",
	)); err != nil {
		t.Fatalf("A up: %v", err)
	}
	defer engA.Down()
	if _, err := engA.Expose(context.Background(), Exposure{
		Port: 8447, Target: "unix:" + target.Addr().String(), ProxyProtocol: "v2",
	}); err != nil {
		t.Fatalf("expose: %v", err)
	}

	engB := &Engine{}
	if _, _, err := engB.Up(context.Background(), upConfig(
		keyB, filepath.Join(dir, "b.addr"), relay, nil, false, "",
	)); err != nil {
		t.Fatalf("B up: %v", err)
	}
	defer engB.Down()

	_, proxiedPort := growPeerSlot(
		t, engB, "dev-a", readAddress(t, filepath.Join(dir, "a.addr")), 8447,
	)

	var header []byte
	poll(t, 30*time.Second, "PROXY v2 header", func() bool {
		conn, err := net.DialTimeout("tcp", "127.0.0.1:"+itoa(proxiedPort), 3*time.Second)
		if err != nil {
			return false
		}
		defer conn.Close()
		_ = conn.SetDeadline(time.Now().Add(3 * time.Second))
		select {
		case header = <-headerCh:
			return true
		default:
		}
		_, _ = conn.Write([]byte("probe"))
		buf := make([]byte, 5)
		_, _ = io.ReadFull(conn, buf)
		select {
		case header = <-headerCh:
			return true
		default:
			return false
		}
	})

	// Signature and family.
	if !strings.HasPrefix(string(header[:12]), string(proxyV2Signature[:])) || header[12] != 0x21 || header[13] != 0x21 {
		t.Fatalf("bad PROXY v2 preamble: %x", header[:16])
	}
	payload := header[16:]
	source := netip.AddrFrom16([16]byte(payload[:16]))
	sourcePort := binary.BigEndian.Uint16(payload[32:34])
	tlvType, tlvLen := payload[36], int(binary.BigEndian.Uint16(payload[37:39]))
	tlv := string(payload[39 : 39+tlvLen])
	if tlvType != 0xe0 {
		t.Fatalf("TLV type = %#x, want 0xe0", tlvType)
	}
	if tlv != derivedB {
		t.Fatalf("TLV nodekey = %q, want B's derived client key", tlv)
	}

	// peer-key resolves the source address the target observed.
	found, public, err := engA.PeerKey(netip.AddrPortFrom(source, sourcePort).String())
	if err != nil {
		t.Fatalf("peer-key: %v", err)
	}
	if !found || public != derivedB {
		t.Fatalf("peer-key = %v, %q", found, public)
	}
	found, public, err = engA.PeerKey("[fd7a:115c:a1e0::9999]:1")
	if err != nil {
		t.Fatalf("peer-key unknown addr: %v", err)
	}
	if found || public != "" {
		t.Fatalf("unknown addr resolved: %v %q", found, public)
	}
}

// shortSlotDir returns a directory short enough for unix socket paths
// (macOS caps sun_path at 104 bytes; t.TempDir is too deep).
func shortSlotDir(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("/tmp", "hysl")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	return dir
}

// bannerServer answers every connection with tag, then echoes.
func bannerServer(t *testing.T, tag string) string {
	t.Helper()
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				_, _ = conn.Write([]byte(tag))
				_, _ = io.Copy(conn, conn)
			}()
		}
	}()
	t.Cleanup(func() { _ = listener.Close() })
	return listener.Addr().String()
}

// readTag reads len(want) bytes from conn and compares.
func readTag(conn net.Conn, want string, timeout time.Duration) error {
	_ = conn.SetReadDeadline(time.Now().Add(timeout))
	buf := make([]byte, len(want))
	if _, err := io.ReadFull(conn, buf); err != nil {
		return err
	}
	if string(buf) != want {
		return errors.New("tag mismatch: " + string(buf))
	}
	return nil
}

// TestSlotHoldBindAndSplice covers the hold-mode life cycle on one slot:
// a connection accepted before the bind stays unread (its first bytes
// queued), map-peer splices it to the peer, the repeat call is
// idempotent and does not disturb live connections, fresh connections
// forward, unmap-peer closes the slot's connections and returns the
// slot to hold mode.
func TestSlotHoldBindAndSplice(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	slotDir := shortSlotDir(t)
	keyA, _ := writeKeyFile(t, dir, "a.key")
	keyB, _ := writeKeyFile(t, dir, "b.key")
	echo := echoServer(t)

	engA := &Engine{}
	if _, _, err := engA.Up(context.Background(), upConfig(
		keyA, filepath.Join(dir, "a.addr"), relay, nil, true, "tcp:"+echo,
	)); err != nil {
		t.Fatalf("A up: %v", err)
	}
	defer engA.Down()

	slot0 := filepath.Join(slotDir, "slot-00")
	slot1 := filepath.Join(slotDir, "slot-01")
	cfgB := upConfig(keyB, filepath.Join(dir, "b.addr"), relay, nil, false, "")
	cfgB.Slots = []string{slot0, slot1}
	engB := &Engine{}
	if _, _, err := engB.Up(context.Background(), cfgB); err != nil {
		t.Fatalf("B up: %v", err)
	}
	defer engB.Down()

	addressA := readAddress(t, filepath.Join(dir, "a.addr"))
	// Warm the Tailcat transport to A through a grown tcp slot so the
	// unix slot's first dial exercises the splice, not the relay
	// handshake.
	_, warmPort := growPeerSlot(t, engB, "warm", addressA, 7447)
	poll(t, 30*time.Second, "warm echo", func() bool {
		return echoRoundTrip(warmPort, "warm-up", 5*time.Second) == nil
	})

	held, err := net.Dial("unix", slot0)
	if err != nil {
		t.Fatal(err)
	}
	defer held.Close()
	payload := "early-handshake"
	if _, err := held.Write([]byte(payload)); err != nil {
		t.Fatal(err)
	}
	poll(t, 5*time.Second, "connection held", func() bool {
		return engB.heldCount(slot0) == 1
	})

	bound := time.Now()
	mapping, err := engB.MapPeer("dev-a", addressA, 7447, slot0)
	if err != nil {
		t.Fatalf("map-peer slot: %v", err)
	}
	if mapping.Slot != slot0 {
		t.Fatalf("mapping = %+v", mapping)
	}
	if err := readTag(held, payload, 30*time.Second); err != nil {
		t.Fatalf("held connection not spliced: %v", err)
	}
	t.Logf("held connection spliced and echoed in %v", time.Since(bound))
	if engB.heldCount(slot0) != 0 {
		t.Fatalf("held connection still counted after bind")
	}

	again, err := engB.MapPeer("dev-a", addressA, 7447, slot0)
	if err != nil || again != mapping {
		t.Fatalf("idempotent slot map = %+v, %v; want %+v", again, err, mapping)
	}
	if _, err := held.Write([]byte(payload)); err != nil {
		t.Fatal(err)
	}
	if err := readTag(held, payload, 10*time.Second); err != nil {
		t.Fatalf("live connection broken by idempotent repeat: %v", err)
	}

	fresh, err := net.Dial("unix", slot0)
	if err != nil {
		t.Fatal(err)
	}
	defer fresh.Close()
	if _, err := fresh.Write([]byte("fresh-conn")); err != nil {
		t.Fatal(err)
	}
	if err := readTag(fresh, "fresh-conn", 30*time.Second); err != nil {
		t.Fatalf("fresh connection on bound slot: %v", err)
	}

	_, _, _, peers, _ := engB.Status()
	var slotPeer *Mapping
	for i := range peers {
		if peers[i].Peer == "dev-a" {
			slotPeer = &peers[i]
		}
	}
	if slotPeer == nil || slotPeer.Slot != slot0 {
		t.Fatalf("status peers = %+v", peers)
	}

	if err := engB.UnmapPeer("dev-a"); err != nil {
		t.Fatalf("unmap-peer: %v", err)
	}
	if err := readTag(held, payload, 5*time.Second); err == nil {
		t.Fatal("held connection still open after unmap-peer")
	}
	// The slot is back in hold mode: a new dial is accepted and held.
	reheld, err := net.Dial("unix", slot0)
	if err != nil {
		t.Fatalf("dial slot after unmap: %v", err)
	}
	defer reheld.Close()
	poll(t, 5*time.Second, "slot holding again", func() bool {
		return engB.heldCount(slot0) == 1
	})
}

// TestListenSlotsCreatesMissingSlotsDirectory: up on a fresh home
// names unix slots under <home>/state/tailcat/slots, which nobody
// pre-creates -- until the listener owned its directory the unix
// transport was unusable on a new home (e2e 2026-10-08). listenSlots
// must create the whole missing chain and bind both sockets.
func TestListenSlotsRefusesUnixPathOverSunPathLimit(t *testing.T) {
	base := shortSlotDir(t)
	tooLong := filepath.Join(base, strings.Repeat("x", 120), "slot-00")

	e := &Engine{}
	e.mu.Lock()
	e.up = true
	e.slots = make(map[string]*slotPort)
	e.mu.Unlock()
	err := e.listenSlots([]string{tooLong}, defaultSlotHold)
	if err == nil {
		t.Fatal("listenSlots accepted a unix path over sun_path")
	}
	if !errors.Is(err, ErrBadInput) {
		t.Fatalf("err = %v, want ErrBadInput", err)
	}
	if !strings.Contains(err.Error(), "path") {
		t.Fatalf("error does not name the path limit: %v", err)
	}
}

func TestListenSlotsCreatesMissingSlotsDirectory(t *testing.T) {
	// Fresh base, short enough for unix socket paths (macOS sun_path).
	base := shortSlotDir(t)
	slotDir := filepath.Join(base, "state", "tailcat", "slots")
	slot0 := filepath.Join(slotDir, "slot-00")
	slot1 := filepath.Join(slotDir, "slot-01")

	e := &Engine{}
	e.mu.Lock()
	e.up = true
	e.slots = make(map[string]*slotPort)
	e.mu.Unlock()
	if err := e.listenSlots([]string{slot0, slot1}, defaultSlotHold); err != nil {
		t.Fatalf("listenSlots on a fresh home: %v", err)
	}
	t.Cleanup(func() {
		e.mu.Lock()
		e.closeSlotsLocked()
		e.mu.Unlock()
		e.wg.Wait()
	})

	for _, path := range []string{slot0, slot1} {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatalf("slot socket %s missing: %v", path, err)
		}
		if info.Mode()&os.ModeSocket == 0 {
			t.Fatalf("slot path %s is not a socket: %v", path, info.Mode())
		}
	}
	info, err := os.Stat(slotDir)
	if err != nil || !info.IsDir() {
		t.Fatalf("slots directory not created: %v", err)
	}
	if got := e.SlotIDs(); len(got) != 2 || got[0] != slot0 || got[1] != slot1 {
		t.Fatalf("SlotIDs = %v, want [%s %s]", got, slot0, slot1)
	}
}

// TestSlotHoldTimeout: a hold-mode connection older than the configured
// hold timeout is dropped so the dialer can redial.
func TestSlotHoldTimeout(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	slotDir := shortSlotDir(t)
	keyB, _ := writeKeyFile(t, dir, "b.key")

	slot0 := filepath.Join(slotDir, "slot-00")
	cfg := upConfig(keyB, filepath.Join(dir, "b.addr"), relay, nil, false, "")
	cfg.Slots = []string{slot0}
	cfg.SlotHold = 300 * time.Millisecond
	eng := &Engine{}
	if _, _, err := eng.Up(context.Background(), cfg); err != nil {
		t.Fatalf("up: %v", err)
	}
	defer eng.Down()

	conn, err := net.Dial("unix", slot0)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	poll(t, 5*time.Second, "connection held", func() bool {
		return eng.heldCount(slot0) == 1
	})
	poll(t, 5*time.Second, "held connection dropped", func() bool {
		return eng.heldCount(slot0) == 0
	})
	_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	if _, err := conn.Read(make([]byte, 1)); err == nil {
		t.Fatal("dropped connection still readable")
	}
}

// TestSlotRebindClosesOldPeer: pointing a bound slot at another peer
// closes the old peer's connections and routes new connections to the
// new peer (design §2 rebind behaviour).
func TestSlotRebindClosesOldPeer(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	slotDir := shortSlotDir(t)
	keyA, _ := writeKeyFile(t, dir, "a.key")
	keyB, _ := writeKeyFile(t, dir, "b.key")
	keyC, _ := writeKeyFile(t, dir, "c.key")

	engA := &Engine{}
	if _, _, err := engA.Up(context.Background(), upConfig(
		keyA, filepath.Join(dir, "a.addr"), relay, nil, true, "tcp:"+bannerServer(t, "AA"),
	)); err != nil {
		t.Fatalf("A up: %v", err)
	}
	defer engA.Down()
	engC := &Engine{}
	if _, _, err := engC.Up(context.Background(), upConfig(
		keyC, filepath.Join(dir, "c.addr"), relay, nil, true, "tcp:"+bannerServer(t, "CC"),
	)); err != nil {
		t.Fatalf("C up: %v", err)
	}
	defer engC.Down()

	slot0 := filepath.Join(slotDir, "slot-00")
	cfgB := upConfig(keyB, filepath.Join(dir, "b.addr"), relay, nil, false, "")
	cfgB.Slots = []string{slot0}
	engB := &Engine{}
	if _, _, err := engB.Up(context.Background(), cfgB); err != nil {
		t.Fatalf("B up: %v", err)
	}
	defer engB.Down()

	addressA := readAddress(t, filepath.Join(dir, "a.addr"))
	addressC := readAddress(t, filepath.Join(dir, "c.addr"))
	for peer, address := range map[string]string{"warm-a": addressA, "warm-c": addressC} {
		_, warmPort := growPeerSlot(t, engB, peer, address, 7447)
		poll(t, 30*time.Second, peer+" banner", func() bool {
			conn, err := net.DialTimeout("tcp", "127.0.0.1:"+itoa(warmPort), 3*time.Second)
			if err != nil {
				return false
			}
			defer conn.Close()
			tag := strings.ToUpper(string(peer[len(peer)-1]))
			return readTag(conn, tag+tag, 3*time.Second) == nil
		})
	}

	if _, err := engB.MapPeer("dev-a", addressA, 7447, slot0); err != nil {
		t.Fatalf("bind slot to A: %v", err)
	}
	oldConn, err := net.Dial("unix", slot0)
	if err != nil {
		t.Fatal(err)
	}
	defer oldConn.Close()
	if err := readTag(oldConn, "AA", 30*time.Second); err != nil {
		t.Fatalf("banner through slot before rebind: %v", err)
	}

	if _, err := engB.MapPeer("dev-c", addressC, 7447, slot0); err != nil {
		t.Fatalf("rebind slot to C: %v", err)
	}
	if err := readTag(oldConn, "AA", 5*time.Second); err == nil {
		t.Fatal("old peer connection survived the rebind")
	}
	newConn, err := net.Dial("unix", slot0)
	if err != nil {
		t.Fatal(err)
	}
	defer newConn.Close()
	if err := readTag(newConn, "CC", 30*time.Second); err != nil {
		t.Fatalf("banner through slot after rebind: %v", err)
	}

	_, _, _, peers, _ := engB.Status()
	for _, peer := range peers {
		if peer.Peer == "dev-a" {
			t.Fatalf("old peer still mapped: %+v", peers)
		}
		if peer.Peer == "dev-c" && peer.Slot != slot0 {
			t.Fatalf("new peer not on slot: %+v", peers)
		}
	}
	if err := engB.UnmapPeer("dev-a"); !errors.Is(err, ErrPeerUnknown) {
		t.Fatalf("unmap displaced peer = %v, want ErrPeerUnknown", err)
	}
}

// TestMapPeerUnknownSlot: binding a slot that was not declared at up is
// a bad request.
func TestMapPeerUnknownSlot(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	keyB, _ := writeKeyFile(t, dir, "b.key")
	eng := &Engine{}
	if _, _, err := eng.Up(context.Background(), upConfig(
		keyB, filepath.Join(dir, "b.addr"), relay, nil, false, "",
	)); err != nil {
		t.Fatalf("up: %v", err)
	}
	defer eng.Down()
	address := readAddress(t, filepath.Join(dir, "b.addr"))
	if _, err := eng.MapPeer("dev-a", address, 7447, "/tmp/absent-slot.sock"); !errors.Is(err, ErrBadInput) {
		t.Fatalf("unknown slot = %v, want ErrBadInput", err)
	}
}

// TestTCPSlotsResolveAndGrow pins the Windows transport contract: a
// tcp:127.0.0.1:0 up spec resolves to a kernel-assigned port reported in
// SlotIDs, map-peer may name a resolved id or grow with the zero spec,
// and the unix lazy-growth rule admits only the shared naming scheme.
func TestTCPSlotsResolveAndGrow(t *testing.T) {
	relay := labrelay.Start(t)
	dir := t.TempDir()
	slotDir := shortSlotDir(t)
	keyB, _ := writeKeyFile(t, dir, "b.key")
	cfg := upConfig(keyB, filepath.Join(dir, "b.addr"), relay, nil, false, "")
	cfg.Slots = []string{
		"tcp:127.0.0.1:0", "tcp:127.0.0.1:0", filepath.Join(slotDir, "slot-02"),
	}
	eng := &Engine{}
	if _, _, err := eng.Up(context.Background(), cfg); err != nil {
		t.Fatalf("up: %v", err)
	}
	defer eng.Down()

	ids := eng.SlotIDs()
	if len(ids) != 3 {
		t.Fatalf("SlotIDs = %v, want 3 entries", ids)
	}
	for _, id := range ids[:2] {
		if !strings.HasPrefix(id, "tcp:127.0.0.1:") || strings.HasSuffix(id, ":0") {
			t.Fatalf("tcp slot id %q is not a resolved loopback port", id)
		}
	}
	if ids[2] != filepath.Join(slotDir, "slot-02") {
		t.Fatalf("unix slot id = %q", ids[2])
	}

	// A resolved tcp slot accepts and holds like a unix slot.
	conn, err := net.Dial("tcp", strings.TrimPrefix(ids[0], "tcp:"))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	poll(t, 5*time.Second, "tcp slot holds", func() bool {
		return eng.heldCount(ids[0]) == 1
	})

	address := readAddress(t, filepath.Join(dir, "b.addr"))
	// Growth: the zero spec answers a NEW resolved id, appended in order.
	mapping, err := eng.MapPeer("dev-grow", address, 7447, "tcp:127.0.0.1:0")
	if err != nil {
		t.Fatalf("tcp growth map-peer: %v", err)
	}
	if !strings.HasPrefix(mapping.Slot, "tcp:127.0.0.1:") ||
		strings.HasSuffix(mapping.Slot, ":0") {
		t.Fatalf("growth answered %q, want a resolved tcp id", mapping.Slot)
	}
	grown := eng.SlotIDs()
	if len(grown) != 4 || grown[3] != mapping.Slot {
		t.Fatalf("SlotIDs after growth = %v, want %q appended", grown, mapping.Slot)
	}

	// Unix lazy growth follows the shared naming rule in an up directory...
	unixGrown := filepath.Join(slotDir, "slot-07")
	if mapped, err := eng.MapPeer("dev-unix", address, 7447, unixGrown); err != nil || mapped.Slot != unixGrown {
		t.Fatalf("unix growth = %v, %v", mapped, err)
	}
	// ...and nothing else: a foreign directory, a non-slot name, and an
	// unknown resolved tcp id are all refused.
	for _, spec := range []string{
		filepath.Join(dir, "slot-09"),
		filepath.Join(slotDir, "other-01"),
		"tcp:127.0.0.1:1",
	} {
		if _, err := eng.MapPeer("dev-bad", address, 7447, spec); !errors.Is(err, ErrBadInput) {
			t.Fatalf("spec %q = %v, want ErrBadInput", spec, err)
		}
	}
}
