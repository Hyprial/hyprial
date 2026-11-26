// Service grants (plan v2 §3.3, slice S4): a keys-mode exposure admits a
// roster member only while the key it shows this server is in the port's
// key set. The set is derived data, so re-exposing the port replaces it.
package membershiptest

import (
	"context"
	"errors"
	"io"
	"net"
	"path/filepath"
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
