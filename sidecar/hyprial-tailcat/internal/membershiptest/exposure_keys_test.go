// Exposure key-set admission tests: a keys-mode exposure admits an active
// tailnet member only while the per-server client key it shows this server is
// in the port's key set. Re-exposing the port replaces the set in place and
// closes newly excluded connections; revoking a listed member still closes its
// connections because keys only narrow membership.
package membershiptest

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/engine"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/membership"
)

const grantedPort = 8447

type grantMesh struct {
	a, b, c  *node
	target   string
	accepted *atomic.Int64
}

func joinGrantMesh(t *testing.T) *grantMesh {
	t.Helper()
	derp := localDERP(t)
	mesh := &grantMesh{a: start(t, "grant-a", derp), b: start(t, "grant-b", derp), c: start(t, "grant-c", derp), accepted: &atomic.Int64{}}
	initNode(t, mesh.a, mesh.a, "tn", []membership.Member{mesh.a.row})
	for _, member := range []*node{mesh.b, mesh.c} {
		invite := issue(t, mesh.a, member, "invite-"+member.row.DeviceID)
		call(t, member, "invite-redeem", membership.RedeemParams{OperationID: "join-" + member.row.DeviceID, Invite: invite})
	}
	for _, n := range []*node{mesh.b, mesh.c, mesh.a} {
		announce(t, n, "tn", "announce-"+n.row.DeviceID)
	}
	for _, n := range []*node{mesh.b, mesh.c, mesh.a} {
		fetch(t, n, "tn")
	}
	listener, err := net.Listen("unix", filepath.Join(mesh.a.dir, "backend"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	mesh.target = "unix:" + listener.Addr().String()
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			mesh.accepted.Add(1)
			go func() { defer conn.Close(); _, _ = io.Copy(conn, conn) }()
		}
	}()
	return mesh
}

func shownKey(t *testing.T, member, server *node) string {
	t.Helper()
	public, err := member.e.DeriveClientPublic(server.row.ServerPublic)
	if err != nil {
		t.Fatal(err)
	}
	return public
}

func exposeKeys(t *testing.T, mesh *grantMesh, keys ...string) engine.Exposure {
	t.Helper()
	exposure, err := mesh.a.e.Expose(context.Background(), engine.Exposure{
		Port: grantedPort, Target: mesh.target, Admission: "keys", Keys: keys,
	})
	if err != nil {
		t.Fatalf("expose with %d keys: %v", len(keys), err)
	}
	return exposure
}

func dialGranted(t *testing.T, mesh *grantMesh, member *node) net.Conn {
	t.Helper()
	slot := filepath.Join(member.dir, "slot-a")
	if _, err := member.e.MapPeer("granted", mesh.a.row.Address, grantedPort, slot); err != nil {
		t.Fatal(err)
	}
	conn, err := net.Dial("unix", slot)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

func echoOn(t *testing.T, conn net.Conn, payload string, timeout time.Duration) error {
	t.Helper()
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

func until(t *testing.T, timeout time.Duration, what string, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if ok() {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func TestExposureKeysAdmitsListedMembersOnly(t *testing.T) {
	mesh := joinGrantMesh(t)
	exposeKeys(t, mesh, shownKey(t, mesh.b, mesh.a))

	refused := dialGranted(t, mesh, mesh.c)
	_, _ = refused.Write([]byte("must-not-reach-backend"))
	requireClosed(t, refused)

	admitted := dialGranted(t, mesh, mesh.b)
	until(t, 30*time.Second, "listed member echo", func() bool {
		return echoOn(t, admitted, "granted", 5*time.Second) == nil
	})
	if got := mesh.accepted.Load(); got != 1 {
		t.Fatalf("backend accepted %d connections, want 1", got)
	}
	if mesh.a.e.DeniedCount() == 0 {
		t.Fatal("the refused connection was not counted as denied")
	}

	again := dialGranted(t, mesh, mesh.c)
	_, _ = again.Write([]byte("still-refused"))
	requireClosed(t, again)
	if got := mesh.accepted.Load(); got != 1 {
		t.Fatalf("the unlisted member reached the backend: %d connections", got)
	}
}

func TestExposureKeysShrinkClosesRemovedKeyOnly(t *testing.T) {
	mesh := joinGrantMesh(t)
	exposeKeys(t, mesh, shownKey(t, mesh.b, mesh.a), shownKey(t, mesh.c, mesh.a))
	connB := dialGranted(t, mesh, mesh.b)
	connC := dialGranted(t, mesh, mesh.c)
	until(t, 30*time.Second, "both members echo", func() bool {
		return echoOn(t, connB, "b-before", 5*time.Second) == nil &&
			echoOn(t, connC, "c-before", 5*time.Second) == nil
	})
	disconnected := mesh.a.e.DisconnectedCount()

	exposeKeys(t, mesh, shownKey(t, mesh.b, mesh.a))

	until(t, 10*time.Second, "removed key's connection closed", func() bool {
		return echoOn(t, connC, "c-after", 2*time.Second) != nil
	})
	until(t, 5*time.Second, "disconnected counter", func() bool {
		return mesh.a.e.DisconnectedCount() > disconnected
	})
	if err := echoOn(t, connB, "b-after", 8*time.Second); err != nil {
		t.Fatalf("the kept member's connection died when the set shrank: %v", err)
	}
	fresh := dialGranted(t, mesh, mesh.c)
	_, _ = fresh.Write([]byte("c-new"))
	requireClosed(t, fresh)
	if got := mesh.accepted.Load(); got != 2 {
		t.Fatalf("backend accepted %d connections, want 2: the removed key got back in", got)
	}
}

func TestExposureKeysEmptySetRefusesEveryone(t *testing.T) {
	mesh := joinGrantMesh(t)
	for _, keys := range [][]string{nil, {}} {
		exposeKeys(t, mesh, keys...)
		for _, member := range []*node{mesh.b, mesh.c} {
			conn := dialGranted(t, mesh, member)
			_, _ = conn.Write([]byte("nobody-is-granted"))
			requireClosed(t, conn)
		}
	}
	if got := mesh.accepted.Load(); got != 0 {
		t.Fatalf("an empty key set let %d connections reach the backend", got)
	}
}

func TestExposureKeysAllowTransitions(t *testing.T) {
	mesh := joinGrantMesh(t)
	exposeKeys(t, mesh)

	if _, err := mesh.a.e.Expose(context.Background(), engine.Exposure{
		Port: grantedPort, Target: mesh.target, Admission: "allow",
	}); err != nil {
		t.Fatalf("widen to allow: %v", err)
	}
	connB := dialGranted(t, mesh, mesh.b)
	connC := dialGranted(t, mesh, mesh.c)
	until(t, 30*time.Second, "both members echo under allow", func() bool {
		return echoOn(t, connB, "b-allow", 5*time.Second) == nil &&
			echoOn(t, connC, "c-allow", 5*time.Second) == nil
	})
	disconnected := mesh.a.e.DisconnectedCount()

	exposeKeys(t, mesh, shownKey(t, mesh.b, mesh.a))
	until(t, 10*time.Second, "unlisted member closed on allow->keys narrow", func() bool {
		return echoOn(t, connC, "c-after", 2*time.Second) != nil
	})
	until(t, 5*time.Second, "disconnected counter on allow->keys", func() bool {
		return mesh.a.e.DisconnectedCount() > disconnected
	})
	if err := echoOn(t, connB, "b-kept", 8*time.Second); err != nil {
		t.Fatalf("kept member closed on allow->keys narrow: %v", err)
	}
}

func TestExposureKeysListedMemberRevokedIsClosed(t *testing.T) {
	mesh := joinGrantMesh(t)
	keyB, keyC := shownKey(t, mesh.b, mesh.a), shownKey(t, mesh.c, mesh.a)
	exposeKeys(t, mesh, keyB, keyC)
	connB := dialGranted(t, mesh, mesh.b)
	connC := dialGranted(t, mesh, mesh.c)
	until(t, 30*time.Second, "both members echo", func() bool {
		return echoOn(t, connB, "b-before", 5*time.Second) == nil && echoOn(t, connC, "c-before", 5*time.Second) == nil
	})
	roster := fetch(t, mesh.a, "tn")
	call(t, mesh.a, "member-revoke", membership.RevokeParams{
		OperationID: "revoke-listed-b", TailnetID: "tn",
		DeviceIDs: []string{mesh.b.row.DeviceID}, ExpectedRevision: roster.Revision, Confirmed: true, Reason: "revoked member must not pass keys gate",
	})
	until(t, 10*time.Second, "revoked listed member closed", func() bool {
		return echoOn(t, connB, "b-after", 2*time.Second) != nil
	})
	if err := echoOn(t, connC, "c-after", 8*time.Second); err != nil {
		t.Fatalf("still-listed member closed by another member's revoke: %v", err)
	}
	before := mesh.accepted.Load()
	fresh := dialGranted(t, mesh, mesh.b)
	_, _ = fresh.Write([]byte("revoked-but-listed"))
	requireClosed(t, fresh)
	exposeKeys(t, mesh, keyB)
	again := dialGranted(t, mesh, mesh.b)
	_, _ = again.Write([]byte("revoked-but-relisted"))
	requireClosed(t, again)
	if got := mesh.accepted.Load(); got != before {
		t.Fatalf("revoked listed member reached backend: %d -> %d", before, got)
	}
}

func TestExposureKeysRefusedUpdatesKeepOldGate(t *testing.T) {
	mesh := joinGrantMesh(t)
	keyB, keyC := shownKey(t, mesh.b, mesh.a), shownKey(t, mesh.c, mesh.a)
	exposeKeys(t, mesh, keyB)
	other, err := net.Listen("unix", filepath.Join(mesh.a.dir, "backend2"))
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	var otherAccepted atomic.Int64
	go func() {
		for {
			conn, err := other.Accept()
			if err != nil {
				return
			}
			otherAccepted.Add(1)
			_ = conn.Close()
		}
	}()
	ctx := context.Background()
	if _, err = mesh.a.e.Expose(ctx, engine.Exposure{Port: grantedPort, Target: "unix:" + other.Addr().String(), Admission: "keys", Keys: []string{keyB, keyC}}); !errors.Is(err, engine.ErrPortInUse) {
		t.Fatalf("target change err = %v, want ErrPortInUse", err)
	}
	if _, err = mesh.a.e.Expose(ctx, engine.Exposure{Port: grantedPort, Target: mesh.target, ProxyProtocol: "v2", Admission: "keys", Keys: []string{keyB, keyC}}); !errors.Is(err, engine.ErrPortInUse) {
		t.Fatalf("proxyProtocol change err = %v, want ErrPortInUse", err)
	}
	if _, err = mesh.a.e.Expose(ctx, engine.Exposure{Port: grantedPort, Target: mesh.target, Admission: "any"}); !errors.Is(err, engine.ErrBadInput) {
		t.Fatalf("keys->any err = %v, want ErrBadInput", err)
	}
	_, err = mesh.a.e.Expose(ctx, engine.Exposure{Port: grantedPort, Target: mesh.target, Admission: "keys", Keys: []string{keyB, keyC, "nodekey:nope"}})
	if !errors.Is(err, engine.ErrBadInput) {
		t.Fatalf("malformed key err = %v, want ErrBadInput", err)
	}
	if strings.Contains(err.Error(), "bad input: bad input:") {
		t.Fatalf("duplicated bad input prefix: %v", err)
	}
	if _, err = mesh.a.e.Expose(ctx, engine.Exposure{Port: grantedPort, Target: mesh.target, Admission: "allow", Keys: []string{keyC}}); !errors.Is(err, engine.ErrBadInput) {
		t.Fatalf("allow with keys err = %v, want ErrBadInput", err)
	}
	refused := dialGranted(t, mesh, mesh.c)
	_, _ = refused.Write([]byte("c-after-refused-updates"))
	requireClosed(t, refused)
	admitted := dialGranted(t, mesh, mesh.b)
	until(t, 30*time.Second, "listed member echo", func() bool {
		return echoOn(t, admitted, "granted", 5*time.Second) == nil
	})
	if got := mesh.accepted.Load(); got != 1 || otherAccepted.Load() != 0 {
		t.Fatalf("backend=%d other=%d, want 1 and 0", got, otherAccepted.Load())
	}
}

func TestExposureKeysShrinkRacesFreshAccept(t *testing.T) {
	mesh := joinGrantMesh(t)
	keyB := shownKey(t, mesh.b, mesh.a)
	exposeKeys(t, mesh, keyB)
	warm := dialGranted(t, mesh, mesh.b)
	until(t, 30*time.Second, "warm echo", func() bool {
		return echoOn(t, warm, "warm", 5*time.Second) == nil
	})
	slot := filepath.Join(mesh.b.dir, "slot-a")
	marker := []byte("AFTER-SHRINK")
	leaked, lingering, preEcho := 0, 0, 0
	const rounds = 60
	for i := 0; i < rounds; i++ {
		exposeKeys(t, mesh, keyB)
		conn, err := net.Dial("unix", slot)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = conn.Write([]byte("before"))
		time.Sleep(time.Duration(i%12) * 300 * time.Microsecond)
		exposeKeys(t, mesh)
		_, _ = conn.Write(marker)
		_ = conn.SetReadDeadline(time.Now().Add(4 * time.Second))
		got, err := io.ReadAll(conn)
		if bytes.Contains(got, []byte("before")) {
			preEcho++
		}
		if bytes.Contains(got, marker) {
			leaked++
		}
		var netErr net.Error
		if errors.As(err, &netErr) && netErr.Timeout() {
			lingering++
		}
		_ = conn.Close()
	}
	until(t, 5*time.Second, "all closed race connections counted", func() bool {
		return mesh.a.e.DeniedCount()+mesh.a.e.DisconnectedCount() >= int64(rounds+1)
	})
	t.Logf("rounds=%d echoed-before-shrink=%d leaked-after-shrink=%d left-open=%d denied=%d disconnected=%d backend=%d",
		rounds, preEcho, leaked, lingering, mesh.a.e.DeniedCount(), mesh.a.e.DisconnectedCount(), mesh.accepted.Load())
	if leaked != 0 || lingering != 0 {
		t.Fatalf("shrink race: leaked=%d left-open=%d", leaked, lingering)
	}
}

func TestExposureKeysProxyProtocolV2(t *testing.T) {
	mesh := joinGrantMesh(t)
	keyB := shownKey(t, mesh.b, mesh.a)
	v2Dir := filepath.Join(mesh.a.dir, "v2backend")
	if err := os.MkdirAll(v2Dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(v2Dir, 0700); err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("unix", filepath.Join(v2Dir, "app.sock"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	var v2Accepted atomic.Int64
	tlvCh := make(chan string, 4)
	signature := [12]byte{0x0d, 0x0a, 0x0d, 0x0a, 0x00, 0x0d, 0x0a, 0x51, 0x55, 0x49, 0x54, 0x0a}
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			v2Accepted.Add(1)
			go func(c net.Conn) {
				defer c.Close()
				preamble := make([]byte, 16)
				if _, err := io.ReadFull(c, preamble); err != nil {
					return
				}
				if !bytes.Equal(preamble[:12], signature[:]) || preamble[12] != 0x21 || preamble[13] != 0x21 {
					return
				}
				payloadLen := int(binary.BigEndian.Uint16(preamble[14:16]))
				payload := make([]byte, payloadLen)
				if _, err := io.ReadFull(c, payload); err != nil {
					return
				}
				if len(payload) >= 39 && payload[36] == 0xe0 {
					tlvLen := int(binary.BigEndian.Uint16(payload[37:39]))
					if 39+tlvLen <= len(payload) {
						tlvCh <- string(payload[39 : 39+tlvLen])
					}
				}
				_, _ = io.Copy(c, c)
			}(conn)
		}
	}()

	v2Target := "unix:" + listener.Addr().String()
	const v2Port = grantedPort + 1
	if _, err := mesh.a.e.Expose(context.Background(), engine.Exposure{
		Port: v2Port, Target: v2Target, ProxyProtocol: "v2", Admission: "keys", Keys: []string{keyB},
	}); err != nil {
		t.Fatalf("expose v2 keys: %v", err)
	}
	dialV2 := func(member *node) net.Conn {
		t.Helper()
		slot := filepath.Join(member.dir, "slot-a")
		if _, err := member.e.MapPeer("granted-v2", mesh.a.row.Address, v2Port, slot); err != nil {
			t.Fatal(err)
		}
		conn, err := net.Dial("unix", slot)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = conn.Close() })
		return conn
	}

	refused := dialV2(mesh.c)
	_, _ = refused.Write([]byte("unlisted-v2"))
	requireClosed(t, refused)
	if got := v2Accepted.Load(); got != 0 {
		t.Fatalf("unlisted member reached v2 backend: %d", got)
	}

	connB := dialV2(mesh.b)
	until(t, 30*time.Second, "listed member v2 echo", func() bool {
		return echoOn(t, connB, "v2-granted", 5*time.Second) == nil
	})
	select {
	case gotKey := <-tlvCh:
		if gotKey != keyB {
			t.Fatalf("PROXY v2 TLV nodekey = %q, want %q", gotKey, keyB)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for PROXY v2 TLV")
	}

	disconnected := mesh.a.e.DisconnectedCount()
	if _, err := mesh.a.e.Expose(context.Background(), engine.Exposure{
		Port: v2Port, Target: v2Target, ProxyProtocol: "v2", Admission: "keys", Keys: []string{},
	}); err != nil {
		t.Fatalf("shrink v2 keys to empty: %v", err)
	}
	until(t, 10*time.Second, "v2 connection closed after shrink", func() bool {
		return echoOn(t, connB, "after-v2-shrink", 2*time.Second) != nil
	})
	until(t, 5*time.Second, "disconnected counter on v2 shrink", func() bool {
		return mesh.a.e.DisconnectedCount() > disconnected
	})
	fresh := dialV2(mesh.b)
	_, _ = fresh.Write([]byte("after-v2-shrink-fresh"))
	requireClosed(t, fresh)
	if got := v2Accepted.Load(); got != 1 {
		t.Fatalf("v2 backend accepted %d connections, want 1", got)
	}
}
