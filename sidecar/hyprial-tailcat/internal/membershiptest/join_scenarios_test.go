package membershiptest

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"net"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"github.com/tailscale/tailcat"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/engine"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/keys"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/labrelay"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/membership"
)

func localDERP(t *testing.T) *engine.DERPOverride {
	d := labrelay.Start(t).Config()
	return &engine.DERPOverride{Host: d.Host, DERPPort: d.DERPPort, STUNPort: d.STUNPort, InsecureForTests: true}
}

func issue(t *testing.T, a, b *node, op string) membership.InviteV2 {
	t.Helper()
	var out struct {
		Invite membership.InviteV2 `json:"invite"`
	}
	if err := json.Unmarshal(call(t, a, "invite-create", membership.CreateParams{
		OperationID: op, TailnetID: "tn", Owner: b.row.Owner,
		ExpiresAtMs: time.Now().Add(10 * time.Minute).UnixMilli(),
	}), &out); err != nil {
		t.Fatal("cannot decode invitation")
	}
	return out.Invite
}

// A hostile peer cannot be modeled with MapPeer: v6 refuses unknown outbound
// members locally. Use a real Tailcat client to test the authority's INBOUND
// gate, not merely the cooperative sender's local check. The key is derived
// exactly as the production pool does; never overlap this client with a live
// engine client of the same identity.
func rawClient(t *testing.T, from, to *node) *tailcat.Client {
	t.Helper()
	material, err := keys.Load(from.cfg.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	info, err := tailcat.ParseAddr(tailcat.Addr(to.row.Address))
	if err != nil {
		t.Fatal("cannot parse test address")
	}
	derived, err := keys.DeriveClientPrivate(material.ClientPrivate, info.ServerPublic.NodePublic)
	if err != nil {
		t.Fatal(err)
	}
	client := &tailcat.Client{Key: derived, Server: tailcat.Addr(to.row.Address), Logf: func(string, ...any) {}}
	t.Cleanup(func() { client.Close() })
	return client
}

func rawDial(t *testing.T, client *tailcat.Client, port uint16) net.Conn {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	conn, err := client.DialTCPPort(ctx, port)
	if err != nil {
		t.Fatal("raw transport dial failed (not an admission refusal)")
	}
	conn.SetDeadline(time.Now().Add(10 * time.Second))
	t.Cleanup(func() { conn.Close() })
	return conn
}

func redemption(t *testing.T, from, to *node, invite membership.InviteV2, op string) membership.Request {
	t.Helper()
	derived, err := from.e.DeriveClientPublic(to.row.ServerPublic)
	if err != nil {
		t.Fatal(err)
	}
	params, _ := json.Marshal(membership.RedeemParams{OperationID: op, Invite: invite,
		ClientPublicByServer: map[string]string{to.row.ServerPublic: derived}})
	return membership.Request{V: 1, Op: "invite-redeem", RequestID: op, Params: params,
		Sender: &membership.Sender{Owner: from.row.Owner, DeviceID: from.row.DeviceID,
			MasterPublic: from.row.MasterPublic, ServerPublic: from.row.ServerPublic, Address: from.row.Address}}
}

func rawRequest(t *testing.T, client *tailcat.Client, request membership.Request) membership.Response {
	t.Helper()
	conn := rawDial(t, client, 7448)
	defer conn.Close()
	if err := json.NewEncoder(conn).Encode(request); err != nil {
		t.Fatal("control write failed")
	}
	var result membership.Response
	if err := json.NewDecoder(bufio.NewReader(conn)).Decode(&result); err != nil {
		t.Fatal("control did not return a terminal frame")
	}
	if result.V != 1 || result.RequestID != request.RequestID || result.Op != request.Op {
		t.Fatal("uncorrelated control result")
	}
	return result
}

func requireClosed(t *testing.T, conn net.Conn) {
	t.Helper()
	conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	_, err := conn.Read(make([]byte, 1))
	if err == nil {
		t.Fatal("business stream returned data after denial")
	}
	if timeout, ok := err.(net.Error); ok && timeout.Timeout() {
		t.Fatal("business stream timed out instead of closing at admission")
	}
}

func TestE2E029AdmissionDeny(t *testing.T) {
	derp := localDERP(t)
	a, b, mallory := start(t, "a", derp), start(t, "b", derp), start(t, "mallory", derp)
	initNode(t, a, a, "tn", []membership.Member{a.row})
	good := issue(t, a, b, "honest")
	call(t, b, "invite-redeem", membership.RedeemParams{OperationID: "join-b", Invite: good})
	// Count real backend connections: zero payload is not enough to prove
	// that an unauthorised transport never reached the private backend.
	var accepted atomic.Int64
	listener, err := net.Listen("unix", filepath.Join(a.dir, "backend"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			accepted.Add(1)
			go func() { defer conn.Close(); io.Copy(conn, conn) }()
		}
	}()
	honest := connect(t, b, a)
	baseline := accepted.Load()
	if baseline != 1 {
		t.Fatal("honest positive control did not reach the backend")
	}
	candidate := issue(t, a, mallory, "negative")
	before := fetch(t, a, "tn")
	client := rawClient(t, mallory, a)
	bad := candidate
	bad.Token = "incorrect-token"
	result := rawRequest(t, client, redemption(t, mallory, a, bad, "wrong-token"))
	if result.Event != "error" || result.Code != "INVITE_INVALID" {
		t.Fatalf("wrong token: event=%s code=%s", result.Event, result.Code)
	}
	// A valid bearer cannot substitute a JSON peer key for the wire key.
	forged := redemption(t, mallory, a, candidate, "wrong-peer")
	var params membership.RedeemParams
	json.Unmarshal(forged.Params, &params)
	params.ClientPublicByServer[a.row.ServerPublic] = b.row.MasterPublic
	forged.Params, _ = json.Marshal(params)
	result = rawRequest(t, client, forged)
	if result.Event != "error" || result.Code != "MEMBER_CONFLICT" {
		t.Fatalf("forged peer: event=%s code=%s", result.Event, result.Code)
	}
	denied := rawDial(t, client, 7447)
	denied.Write([]byte("must-not-reach-backend"))
	requireClosed(t, denied)
	// Positive traffic after the negative probe rules out a dead listener.
	ping(t, honest)
	after := fetch(t, a, "tn")
	if after.Revision != before.Revision || len(after.Members) != 2 || accepted.Load() != baseline {
		t.Fatal("denial changed membership or reached the backend")
	}
	call(t, a, "member-revoke", membership.RevokeParams{OperationID: "revoke-b", TailnetID: "tn",
		DeviceIDs: []string{"b"}, ExpectedRevision: after.Revision, Confirmed: true, Reason: "e2e-029"})
	requireClosed(t, honest)
	// Bob still has an old local lease. Do not fetch his revoked roster yet:
	// a fresh connection through his existing slot must be denied by Alice.
	fresh, err := net.Dial("unix", b.cfg.Slots[0])
	if err != nil {
		t.Fatal(err)
	}
	defer fresh.Close()
	fresh.Write([]byte("after-revoke"))
	requireClosed(t, fresh)
	if accepted.Load() != baseline {
		t.Fatal("revoked connection reached backend")
	}
	if _, err := run(b, "invite-redeem", membership.RedeemParams{OperationID: "join-b", Invite: good}); membership.Code(err) != "MEMBER_REVOKED" {
		t.Fatal("redeemed token resurrected revoked device")
	}
	result = rawRequest(t, client, redemption(t, mallory, a, bad, "still-control"))
	if result.Code != "INVITE_INVALID" {
		t.Fatal("control did not remain reachable after business denial")
	}
	t.Log("E2E-029: honest echo; wrong token; forged peer; inbound shutout; live and fresh revoked streams closed; no resurrection; control remains reachable")
}

func TestE2E030JoinResume(t *testing.T) {
	if testing.Short() {
		t.Skip("E2E-030 waits past the 65-second v6 refresh interval; run without -short")
	}
	derp := localDERP(t)
	a, b := start(t, "a", derp), start(t, "b", derp)
	initNode(t, a, a, "tn", []membership.Member{a.row})
	// Exceed the real 60-second v6 refresh interval before issuing the link.
	// The control-only process must not depend on a startup admission window.
	until := time.Now().Add(65 * time.Second)
	for time.Now().Before(until) {
		if !b.e.Readiness().Control || b.e.Readiness().Member {
			t.Fatal("uninvited running joiner did not remain control-only")
		}
		time.Sleep(time.Second)
	}
	invite := issue(t, a, b, "resume-invite")
	client := rawClient(t, b, a)
	conn := rawDial(t, client, 7448)
	request := redemption(t, b, a, invite, "resume-redemption")
	if err := json.NewEncoder(conn).Encode(request); err != nil {
		t.Fatal("redemption write failed")
	}
	// Discard the reply. Observe commit only through the authority's local
	// public roster operation, then restart BOTH engines and their stores.
	deadline := time.Now().Add(15 * time.Second)
	var committed membership.Roster
	for time.Now().Before(deadline) {
		committed = fetch(t, a, "tn")
		if len(committed.Members) == 2 {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if len(committed.Members) != 2 || b.e.Readiness().Member {
		t.Fatal("lost-reply boundary not established")
	}
	conn.Close()
	if err := client.Close(); err != nil {
		t.Fatal("raw generation did not close before replay")
	}
	for _, n := range []*node{b, a} {
		n.e.Down()
	}
	for _, n := range []*node{a, b} {
		if _, _, err := n.e.Up(context.Background(), n.cfg); err != nil {
			t.Fatal("restart failed")
		}
	}
	for _, n := range []*node{a, b} {
		if _, err := os.Stat(filepath.Join(n.dir, "backend")); !os.IsNotExist(err) {
			t.Fatal("backend existed before redemption recovery")
		}
	}
	params := membership.RedeemParams{OperationID: "resume-redemption", Invite: invite}
	first := call(t, b, "invite-redeem", params)
	second := call(t, b, "invite-redeem", params)
	if string(first) != string(second) || !b.e.Readiness().Member {
		t.Fatal("replay did not return original receipt and a fresh lease")
	}
	after := fetch(t, a, "tn")
	if after.Revision != committed.Revision || len(after.Members) != 2 {
		t.Fatal("replay consumed the invitation twice")
	}
	echo(t, a)
	echo(t, b)
	connect(t, b, a)
	connect(t, a, b)
	// Same owner, different device cannot use the consumed bearer.
	other := start(t, "other", derp)
	other.e.Down()
	other.cfg.Owner = b.row.Owner
	other.row.Owner = b.row.Owner
	if _, _, err := other.e.Up(context.Background(), other.cfg); err != nil {
		t.Fatal(err)
	}
	if _, err := run(other, "invite-redeem", membership.RedeemParams{OperationID: "other-redemption", Invite: invite}); membership.Code(err) != "INVITE_CONSUMED" {
		t.Fatal("another device reused the consumed bearer")
	}
	if other.e.Readiness().Member {
		t.Fatal("refused device became ready")
	}
	t.Log("E2E-030: control-only beyond refresh; reply discarded after commit; both stores restarted; same receipt; one consume; no backend prerequisite; bidirectional echo; other device refused")
}
