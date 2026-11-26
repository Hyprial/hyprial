package membershiptest

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/engine"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/keys"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/labrelay"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/membership"
)

type node struct {
	e   *engine.Engine
	row membership.Member
	dir string
	cfg engine.UpConfig
}

func start(t *testing.T, id string, derp *engine.DERPOverride) *node {
	t.Helper()
	dir, err := os.MkdirTemp("/tmp", "hr-go-cycle-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	keyFile := filepath.Join(dir, "device.key")
	public, err := keys.Generate(keyFile, false)
	if err != nil {
		t.Fatal(err)
	}
	cfg := engine.UpConfig{KeyFile: keyFile, MembershipStore: filepath.Join(dir, "tailnet", "membership.json"), Owner: "owner-" + id, DeviceID: id, ControlPort: 7448, PeerPort: 7447, InboundTarget: "unix:" + filepath.Join(dir, "backend"), AddressFile: filepath.Join(dir, "address"), DERP: derp, Slots: []string{filepath.Join(dir, "slot-00"), filepath.Join(dir, "slot-a"), filepath.Join(dir, "slot-b"), filepath.Join(dir, "slot-c")}}
	e := &engine.Engine{}
	if _, _, err := e.Up(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(e.Down)
	address, err := os.ReadFile(cfg.AddressFile)
	if err != nil {
		t.Fatal(err)
	}
	return &node{e: e, dir: dir, cfg: cfg, row: membership.Member{Owner: cfg.Owner, DeviceID: id, MasterPublic: public.ClientPublic, ServerPublic: public.ServerPublic, ClientPublicByServer: map[string]string{}, Address: strings.TrimSpace(string(address)), State: "active", Revision: 1}}
}
func pin(n *node) membership.Authority {
	return membership.Authority{Owner: n.row.Owner, DeviceID: n.row.DeviceID, MasterPublic: n.row.MasterPublic, ServerPublic: n.row.ServerPublic, Address: n.row.Address, ControlPort: 7448}
}
func run(n *node, op string, p any) (json.RawMessage, error) {
	b, _ := json.Marshal(p)
	return n.e.Membership(context.Background(), membership.Request{V: 7, Op: op, RequestID: "r", Params: b})
}
func call(t *testing.T, n *node, op string, p any) json.RawMessage {
	t.Helper()
	started := time.Now()
	b, err := run(n, op, p)
	if err != nil {
		data, message := membership.Diagnostic(err)
		t.Fatalf("%s %s after %s: %v %s %s", n.row.DeviceID, op, time.Since(started), err, data, message)
	}
	return b
}
func initNode(t *testing.T, n, a *node, id string, rows []membership.Member) {
	call(t, n, "membership-init", membership.InitParams{OperationID: "init-" + id, TailnetID: id, Authority: pin(a), Migration: membership.Migration{Source: "directory-0.5.5", SourceDigest: "test", Completed: true}, Members: rows})
}
func fetch(t *testing.T, n *node, id string) membership.Roster {
	t.Helper()
	var r membership.Roster
	json.Unmarshal(call(t, n, "roster-fetch", membership.FetchParams{TailnetID: id}), &r)
	return r
}
func announce(t *testing.T, n *node, id, op string) {
	t.Helper()
	call(t, n, "member-announce", membership.AnnounceParams{OperationID: op, TailnetID: id, Address: n.row.Address, ClientPublicByServer: map[string]string{}})
}
func echo(t *testing.T, n *node) {
	t.Helper()
	ln, err := net.Listen("unix", filepath.Join(n.dir, "backend"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func() { defer c.Close(); io.Copy(c, c) }()
		}
	}()
}
func ping(t *testing.T, c net.Conn) {
	t.Helper()
	c.SetDeadline(time.Now().Add(10 * time.Second))
	if _, err := c.Write([]byte("hello")); err != nil {
		t.Fatal(err)
	}
	b := make([]byte, 5)
	if _, err := io.ReadFull(c, b); err != nil || string(b) != "hello" {
		t.Fatalf("echo: %v", err)
	}
}
func connect(t *testing.T, from, to *node) net.Conn {
	t.Helper()
	if _, err := from.e.MapPeer(to.row.DeviceID, to.row.Address, 7447, from.cfg.Slots[0]); err != nil {
		t.Fatal(err)
	}
	c, err := net.Dial("unix", from.cfg.Slots[0])
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	ping(t, c)
	return c
}

func TestRealControlBootstrapWithoutBackendThenRevoke(t *testing.T) {
	relay := labrelay.Start(t)
	d := relay.Config()
	derp := &engine.DERPOverride{Host: d.Host, DERPPort: d.DERPPort, STUNPort: d.STUNPort, InsecureForTests: true}
	a, b := start(t, "a", derp), start(t, "b", derp)
	if !a.e.Readiness().Control || a.e.Readiness().Member {
		t.Fatal("incorrect initial readiness")
	}
	initNode(t, a, a, "tn", []membership.Member{a.row, b.row})
	initNode(t, b, a, "tn", []membership.Member{})
	first := fetch(t, b, "tn")
	if len(first.Members) != 2 || !b.e.Readiness().Member {
		t.Fatal("bootstrap failed")
	}
	if _, err := os.Stat(filepath.Join(a.dir, "backend")); !os.IsNotExist(err) {
		t.Fatal("backend was prerequisite")
	}
	announce(t, b, "tn", "b-announce")
	announce(t, a, "tn", "a-announce")
	fetch(t, b, "tn")
	echo(t, a)
	echo(t, b)
	ab := connect(t, a, b)
	ba := connect(t, b, a)
	ping(t, ab)
	ping(t, ba)
	before := fetch(t, a, "tn")
	call(t, a, "member-revoke", membership.RevokeParams{OperationID: "remove-b", TailnetID: "tn", DeviceIDs: []string{"b"}, ExpectedRevision: before.Revision, Confirmed: true, Reason: "test"})
	ba.SetReadDeadline(time.Now().Add(5 * time.Second))
	if _, err := ba.Read(make([]byte, 1)); err == nil {
		t.Fatal("revoked connection survived")
	} else if e, ok := err.(net.Error); ok && e.Timeout() {
		t.Fatal("revocation did not close stream")
	}
	if _, err := run(b, "roster-fetch", membership.FetchParams{TailnetID: "tn"}); membership.Code(err) != "MEMBER_REVOKED" {
		t.Fatal("revoked member renewed", err)
	}
	if b.e.Readiness().Member {
		t.Fatal("revoked member remains ready")
	}
}
func TestMappedPeerSurvivesRemoteServerRestart(t *testing.T) {
	relay := labrelay.Start(t)
	d := relay.Config()
	derp := &engine.DERPOverride{Host: d.Host, DERPPort: d.DERPPort, STUNPort: d.STUNPort, InsecureForTests: true}
	a, b := start(t, "a", derp), start(t, "b", derp)
	initNode(t, a, a, "tn", []membership.Member{a.row, b.row})
	initNode(t, b, a, "tn", []membership.Member{})
	fetch(t, b, "tn")
	announce(t, b, "tn", "b-before")
	announce(t, a, "tn", "a-before")
	fetch(t, b, "tn")
	echo(t, b)
	before := connect(t, a, b)
	b.e.Down()
	before.Close()
	if _, _, err := b.e.Up(context.Background(), b.cfg); err != nil {
		t.Fatal(err)
	}
	fetch(t, b, "tn")
	// No unmap/remap, key rotation, or manual client reset may be required.
	deadline := time.Now().Add(30 * time.Second)
	var last error
	for time.Now().Before(deadline) {
		after, err := net.Dial("unix", a.cfg.Slots[0])
		if err != nil {
			t.Fatal(err)
		}
		after.SetDeadline(time.Now().Add(3 * time.Second))
		_, last = after.Write([]byte("hello"))
		if last == nil {
			_, last = io.ReadFull(after, make([]byte, 5))
		}
		after.Close()
		if last == nil {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("mapped peer did not recover after server restart: %v", last)
}

func TestRealAuthorityOutageLeaseExpiry(t *testing.T) {
	relay := labrelay.Start(t)
	d := relay.Config()
	derp := &engine.DERPOverride{Host: d.Host, DERPPort: d.DERPPort, STUNPort: d.STUNPort, InsecureForTests: true}
	a, b, c := start(t, "a", derp), start(t, "b", derp), start(t, "c", derp)
	initNode(t, a, a, "tn", []membership.Member{a.row, b.row, c.row})
	for _, n := range []*node{b, c} {
		initNode(t, n, a, "tn", []membership.Member{})
		fetch(t, n, "tn")
		announce(t, n, "tn", "announce-"+n.row.DeviceID)
	}
	announce(t, a, "tn", "announce-a")
	fetch(t, b, "tn")
	fetch(t, c, "tn")
	echo(t, c)
	stream := connect(t, b, c)
	a.e.Down()
	stopped := time.Now()
	// No accelerated clock: B-C traffic survives A's loss until the real lease.
	for time.Since(stopped) < 270*time.Second {
		ping(t, stream)
		time.Sleep(5 * time.Second)
	}
	stream.SetReadDeadline(stopped.Add(310 * time.Second))
	if _, err := stream.Read(make([]byte, 1)); err == nil {
		t.Fatal("lease expiry did not close the existing member-member stream")
	} else if e, ok := err.(net.Error); ok && e.Timeout() {
		t.Fatal("stream survived 300-second lease bound")
	}
	if b.e.Readiness().Member || c.e.Readiness().Member {
		// Their last successful fetches are separated by a bounded control RTT.
		deadline := stopped.Add(310 * time.Second)
		for (b.e.Readiness().Member || c.e.Readiness().Member) && time.Now().Before(deadline) {
			time.Sleep(100 * time.Millisecond)
		}
	}
	if b.e.Readiness().Member || c.e.Readiness().Member || !b.e.Readiness().Control || !c.e.Readiness().Control {
		t.Fatal("expired members must retain control only")
	}
	t.Logf("real lease expired after %.2fs; live B-C stream closed", time.Since(stopped).Seconds())
	c.e.Down()
	if _, _, err := c.e.Up(context.Background(), c.cfg); err != nil {
		t.Fatal(err)
	}
	if c.e.Readiness().Member {
		t.Fatal("restart revived a persisted lease")
	}
}

func TestFreshInviteAllowsAuthorityDialBeforeRefresh(t *testing.T) {
	relay := labrelay.Start(t)
	d := relay.Config()
	derp := &engine.DERPOverride{Host: d.Host, DERPPort: d.DERPPort, STUNPort: d.STUNPort, InsecureForTests: true}
	a, b := start(t, "a", derp), start(t, "b", derp)
	initNode(t, a, a, "tn", []membership.Member{a.row})
	announce(t, a, "tn", "initial-announce")
	var invite struct {
		Invite membership.InviteV2 `json:"invite"`
	}
	json.Unmarshal(call(t, a, "invite-create", membership.CreateParams{OperationID: "invite-b", TailnetID: "tn", Owner: b.row.Owner, ExpiresAtMs: time.Now().Add(time.Minute).UnixMilli()}), &invite)
	call(t, b, "invite-redeem", membership.RedeemParams{OperationID: "redeem-b", Invite: invite.Invite})
	// The authority can win lexical dial ordering. Its key for the new
	// server must be in the returned roster, not delayed until the 60s tick.
	echo(t, b)
	connect(t, a, b)
}

func TestFreshInviteAndThreeServerSharedPool(t *testing.T) {
	relay := labrelay.Start(t)
	d := relay.Config()
	derp := &engine.DERPOverride{Host: d.Host, DERPPort: d.DERPPort, STUNPort: d.STUNPort, InsecureForTests: true}
	a := start(t, "a", derp)
	initNode(t, a, a, "tn", []membership.Member{a.row})
	nodes := []*node{a}
	for _, id := range []string{"b", "c", "d"} {
		n := start(t, id, derp)
		var invite struct {
			Invite membership.InviteV2 `json:"invite"`
		}
		json.Unmarshal(call(t, a, "invite-create", membership.CreateParams{OperationID: "invite-" + id, TailnetID: "tn", Owner: n.row.Owner, ExpiresAtMs: time.Now().Add(50 * time.Minute).UnixMilli()}), &invite)
		p := membership.RedeemParams{OperationID: "redeem-" + id, Invite: invite.Invite}
		call(t, n, "invite-redeem", p)
		call(t, n, "invite-redeem", p)
		nodes = append(nodes, n)
	}
	for i, n := range nodes {
		fetch(t, n, "tn")
		announce(t, n, "tn", "announce-"+strconv.Itoa(i))
		echo(t, n)
	}
	for _, n := range nodes {
		fetch(t, n, "tn")
	}
	source := nodes[3]
	var wg sync.WaitGroup
	errs := make(chan error, 12)
	for _, target := range nodes[:3] {
		id := "pool-" + target.row.DeviceID
		initNode(t, target, target, id, []membership.Member{target.row, source.row})
		initNode(t, source, target, id, []membership.Member{})
		fetch(t, source, id)
		announce(t, source, id, "source-"+id)
		announce(t, target, id, "target-"+id)
		fetch(t, source, id)
		slot := filepath.Join(source.dir, "slot-"+target.row.DeviceID)
		if _, err := source.e.MapPeer(target.row.DeviceID, target.row.Address, 7447, slot); err != nil {
			t.Fatal(err)
		}
		if _, err := target.e.Expose(context.Background(), engine.Exposure{Port: 8080, Target: target.cfg.InboundTarget, Admission: "allow"}); err != nil {
			t.Fatal(err)
		}
		mapping, err := source.e.MapService(engine.ServiceRequest{Name: "svc-" + target.row.DeviceID, DeviceID: target.row.DeviceID, Address: target.row.Address, ServerPublic: target.row.ServerPublic, RemotePort: 8080, RecordGeneration: 1})
		if err != nil {
			t.Fatal(err)
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 5; i++ {
				if _, err := run(source, "roster-fetch", membership.FetchParams{TailnetID: id}); err != nil {
					errs <- err
					return
				}
				mesh, err := net.Dial("unix", slot)
				if err != nil {
					errs <- err
					return
				}
				mesh.SetDeadline(time.Now().Add(10 * time.Second))
				_, err = mesh.Write([]byte("hello"))
				if err == nil {
					_, err = io.ReadFull(mesh, make([]byte, 5))
				}
				mesh.Close()
				if err != nil {
					errs <- err
					return
				}
				c, err := net.Dial("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(mapping.LocalPort)))
				if err != nil {
					errs <- err
					return
				}
				c.SetDeadline(time.Now().Add(10 * time.Second))
				_, err = c.Write([]byte("hello"))
				if err == nil {
					b := make([]byte, 5)
					_, err = io.ReadFull(c, b)
				}
				c.Close()
				if err != nil {
					errs <- err
					return
				}
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}
}
