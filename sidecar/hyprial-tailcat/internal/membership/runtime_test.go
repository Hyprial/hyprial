package membership

import (
	"bufio"
	"context"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"tailscale.com/types/key"
)

func TestControlResponseRequiresCompleteFrame(t *testing.T) {
	for _, mode := range []string{"deadline-partial", "eof-partial", "eof-valid-json", "complete", "malformed-complete"} {
		t.Run(mode, func(t *testing.T) {
			a, b := identity("a"), identity("b")
			m := openTest(t, b)
			data, _ := json.Marshal(Roster{TailnetID: "tn", Authority: authority(a), Revision: 1, Members: []Member{row(a), row(b)}, LeaseSeconds: 300})
			full, _ := json.Marshal(Response{V: 1, Event: "membership-result", RequestID: "framing", Op: "roster-fetch", Data: data})
			payload := []byte(`{"v":1,"event":"membership-result",`)
			if mode == "eof-valid-json" || mode == "complete" {
				payload = full
			}
			if mode == "complete" {
				payload = append(payload, '\n')
			}
			if mode == "malformed-complete" {
				payload = []byte("{broken}\n")
			}
			done := make(chan struct{})
			m.dial = func(ctx context.Context, _ Authority) (net.Conn, error) {
				client, server := net.Pipe()
				go func() {
					defer close(done)
					defer server.Close()
					server.SetDeadline(time.Now().Add(time.Second))
					bufio.NewReader(server).ReadString('\n')
					server.Write(payload)
					if mode == "deadline-partial" {
						<-ctx.Done()
					}
				}()
				return client, nil
			}
			ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
			defer cancel()
			_, err := m.exchange(ctx, authority(a), Request{V: 6, Op: "roster-fetch", RequestID: "framing", Params: json.RawMessage(`{"tailnetId":"tn"}`)}, b)
			if mode == "complete" {
				if err != nil {
					t.Fatal(err)
				}
			} else if mode == "malformed-complete" {
				if Code(err) != "BAD_REQUEST" {
					t.Fatalf("complete malformed response: %v", err)
				}
			} else {
				if Code(err) != "AUTHORITY_UNREACHABLE" {
					t.Fatalf("incomplete response classified as %s", Code(err))
				}
				raw, message := Diagnostic(err)
				var detail map[string]any
				json.Unmarshal(raw, &detail)
				if detail["bytesReceived"] != float64(len(payload)) || !ValidateDiagnostic(raw) {
					t.Fatalf("missing safe byte count: %s %s", raw, message)
				}
				want := "transport-failed"
				if mode == "deadline-partial" {
					want = "deadline-exceeded"
				}
				if detail["cause"] != want {
					t.Fatalf("wrong incomplete frame cause: %s", raw)
				}
			}
			select {
			case <-done:
			case <-time.After(time.Second):
				t.Fatal("fixture peer did not stop")
			}
		})
	}
}

func TestRejectIdentityReplacementAndMissingTombstone(t *testing.T) {
	a, b := identity("a"), identity("b")
	m := openTest(t, b)
	initStore(t, m, a)
	original := Roster{TailnetID: "tn", Authority: authority(a), Revision: 1, Members: []Member{row(a), row(b)}, LeaseSeconds: 300}
	if err := m.ApplyRoster(original, time.Now()); err != nil {
		t.Fatal(err)
	}
	changed := original
	changed.Revision = 2
	changed.Members = []Member{row(a), row(b)}
	changed.Members[1].MasterPublic = key.NewNode().Public().String()
	if err := m.ApplyRoster(changed, time.Now()); Code(err) != "MEMBER_CONFLICT" {
		t.Fatal("implicit key replacement", err)
	}
	changed.Members = []Member{row(a)}
	if err := m.ApplyRoster(changed, time.Now()); Code(err) != "ROSTER_ROLLBACK" {
		t.Fatal("missing prior row accepted", err)
	}
}
func TestLeaseDeadlineEmitsWithoutRefreshTick(t *testing.T) {
	a, b := identity("a"), identity("b")
	m := openTest(t, b)
	initStore(t, m, a)
	expired := make(chan struct{}, 1)
	m.SetHooks(func(_ string, _ uint64, code string) {
		if code == "MEMBERSHIP_LEASE_EXPIRED" {
			expired <- struct{}{}
		}
	}, nil)
	if err := m.ApplyRoster(Roster{TailnetID: "tn", Authority: authority(a), Revision: 1, Members: []Member{row(a), row(b)}, LeaseSeconds: 300}, time.Now().Add(-Lease+100*time.Millisecond)); err != nil {
		t.Fatal(err)
	}
	select {
	case <-expired:
	case <-time.After(500 * time.Millisecond):
		t.Fatal("lease expiry waits for maintenance tick")
	}
}

func TestRepinRequiresRestart(t *testing.T) {
	a, b := identity("a"), identity("b")
	m := openTest(t, a)
	initStore(t, m, a, a, b)
	m.SetHooks(nil, func() bool { return true })
	invoke(t, m, "authority-repin", RepinParams{"repin", "tn", authority(a), 1, true})
	if m.Readiness().Member {
		t.Fatal("repin reopened networking before restart")
	}
}
func TestRejectNullSenderAndOversizedRevision(t *testing.T) {
	for _, s := range []string{`{"v":6,"op":"roster-fetch","requestId":"r","params":{"tailnetId":"tn"},"sender":null}`, `{"v":6,"op":"member-revoke","requestId":"r","params":{"operationId":"op","tailnetId":"tn","deviceIds":["b"],"expectedRevision":9007199254740992,"confirmed":true,"reason":"test"}}`} {
		if _, err := Parse([]byte(s), false); err == nil {
			t.Fatal("invalid shape accepted")
		}
	}
}

func TestRemoteSelfLeaveLostReply(t *testing.T) {
	a, b := identity("a"), identity("b")
	m := openTest(t, a)
	initStore(t, m, a, a, b)
	r := request("member-revoke", RevokeParams{OperationID: "leave", TailnetID: "tn", DeviceIDs: []string{"b"}, ExpectedRevision: 1, Confirmed: true, Reason: "leave"})
	m.mu.Lock()
	first, err := m.localLocked(r, b, "", true)
	second, again := m.localLocked(r, b, "", true)
	m.mu.Unlock()
	if err != nil || again != nil || string(first) != string(second) {
		t.Fatalf("lost reply cannot retry: %v %v", err, again)
	}
}
func TestCorruptStoreDuplicateKeys(t *testing.T) {
	a := identity("a")
	m := openTest(t, a)
	m.Close()
	os.WriteFile(m.path, []byte(`{"schemaVersion":9,"schemaVersion":1,"tailnets":{},"operations":{}}`), 0600)
	if reopened, err := Open(m.path, a, nil, nil); err == nil {
		reopened.Close()
		t.Fatal("accepted duplicate store keys")
	}
}
func TestRemoteControlBootstrapInviteAndTransportBinding(t *testing.T) {
	a, b := identity("a"), identity("b")
	authority := openTest(t, a)
	initStore(t, authority, a, a, b)
	derived := key.NewNode().Public().String()
	dial := func(ctx context.Context, pin Authority) (net.Conn, error) {
		client, server := net.Pipe()
		go authority.ServeControl(server, derived)
		return client, nil
	}
	member, err := Open(filepath.Join(t.TempDir(), "state", "membership.json"), b, func(string) (string, error) { return derived, nil }, dial)
	if err != nil {
		t.Fatal(err)
	}
	defer member.Close()
	initStore(t, member, a)
	invoke(t, member, "roster-fetch", FetchParams{"tn"})
	if !member.Readiness().Member || authority.Admits(b.MasterPublic) {
		t.Fatal("invalid bootstrap readiness/admission")
	}
	invoke(t, member, "member-announce", AnnounceParams{"announce", "tn", b.Address, map[string]string{}})
	if !authority.Admits(derived) {
		t.Fatal("announced derived key denied")
	}
	var created struct {
		Invite InviteV2 `json:"invite"`
	}
	json.Unmarshal(invoke(t, authority, "invite-create", CreateParams{"invite", "tn", "owner-c", time.Now().Add(time.Hour - time.Second).UnixMilli()}), &created)
	c := identity("c")
	r := request("invite-redeem", RedeemParams{OperationID: "redeem", Invite: created.Invite, ClientPublicByServer: map[string]string{a.ServerPublic: key.NewNode().Public().String()}})
	authority.mu.Lock()
	_, err = authority.localLocked(r, c, "wrong-peer", true)
	authority.mu.Unlock()
	if Code(err) != "MEMBER_CONFLICT" {
		t.Fatal("wrong transport admitted", err)
	}
	r.Params = marshal(RedeemParams{OperationID: "redeem", Invite: created.Invite, ClientPublicByServer: map[string]string{a.ServerPublic: key.NewNode().Public().String()}})
	var p RedeemParams
	json.Unmarshal(r.Params, &p)
	authority.mu.Lock()
	data, err := authority.localLocked(r, c, p.ClientPublicByServer[a.ServerPublic], true)
	authority.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	var roster Roster
	json.Unmarshal(data, &roster)
	if roster.DeviceID != "c" || len(roster.Members) != 3 {
		t.Fatal("bad redemption")
	}
}
func TestRedeemedInviteCannotRenewRevokedMember(t *testing.T) {
	a, b := identity("a"), identity("b")
	m := openTest(t, a)
	initStore(t, m, a, a)
	var out struct {
		Invite InviteV2 `json:"invite"`
	}
	json.Unmarshal(invoke(t, m, "invite-create", CreateParams{"invite", "tn", b.Owner, time.Now().Add(time.Minute).UnixMilli()}), &out)
	derived := key.NewNode().Public().String()
	r := request("invite-redeem", RedeemParams{"redeem", out.Invite, map[string]string{a.ServerPublic: derived}})
	m.mu.Lock()
	_, err := m.localLocked(r, b, derived, true)
	m.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	invoke(t, m, "member-revoke", RevokeParams{"remove", "tn", []string{"b"}, 2, true, "test"})
	m.mu.Lock()
	_, err = m.localLocked(r, b, derived, true)
	m.mu.Unlock()
	if Code(err) != "MEMBER_REVOKED" {
		t.Fatal("old invite renewed revoked member", err)
	}
}

func TestAutomaticAnnouncementFrozenAcrossRestart(t *testing.T) {
	a, b := identity("a"), identity("b")
	m := openTest(t, a)
	initStore(t, m, a, a, b)
	m.derive = func(string) (string, error) { return key.NewNode().Public().String(), nil }
	req := request("member-announce", AnnounceParams{"frozen", "tn", a.Address, map[string]string{}})
	m.mu.Lock()
	first, err := m.expandLocked(req, m.store.Tailnets["tn"])
	m.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	path := m.path
	m.Close()
	again, err := Open(path, a, func(string) (string, error) { return "", fail("DERIVED_AGAIN") }, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer again.Close()
	again.mu.Lock()
	second, err := again.expandLocked(req, again.store.Tailnets["tn"])
	again.mu.Unlock()
	if err != nil || string(first.Params) != string(second.Params) {
		t.Fatal("announcement was not durable", err)
	}
}

func TestUncertainWriteFailsClosedUntilReopen(t *testing.T) {
	a := identity("a")
	m := openTest(t, a)
	initStore(t, m, a, a)
	if !m.Readiness().Member {
		t.Fatal("authority not ready")
	}
	if err := os.Chmod(filepath.Dir(m.path), 0755); err != nil {
		t.Fatal(err)
	}
	m.mu.Lock()
	err := m.commit(cloneStore(m.store))
	m.mu.Unlock()
	os.Chmod(filepath.Dir(m.path), 0700)
	if Code(err) != "MEMBERSHIP_STORE_INVALID" || m.Readiness().Member {
		t.Fatal("failed write kept admission", err)
	}
	if _, err := m.Execute(context.Background(), request("roster-fetch", FetchParams{"tn"})); Code(err) != "MEMBERSHIP_STORE_INVALID" {
		t.Fatal("uncertain store served roster", err)
	}
}

func TestBackupFailureAndAtomicRevoke(t *testing.T) {
	a, b := identity("a"), identity("b")
	m := openTest(t, a)
	initStore(t, m, a, a, b)
	os.Chmod(filepath.Dir(m.path), 0755)
	defer os.Chmod(filepath.Dir(m.path), 0700)
	_, err := m.Execute(context.Background(), request("member-revoke", RevokeParams{"revoke", "tn", []string{"b"}, 1, true, "test"}))
	if Code(err) != "MEMBERSHIP_BACKUP_FAILED" {
		t.Fatal(err)
	}
	if m.store.Tailnets["tn"].Members["b"].State != "active" || m.store.Tailnets["tn"].Revision != 1 {
		t.Fatal("failed backup mutated store")
	}
}
func TestMultiTailnetAdmissionAndExpiry(t *testing.T) {
	a, b := identity("a"), identity("b")
	m := openTest(t, b)
	initStore(t, m, a)
	d := key.NewNode().Public().String()
	ra := row(a)
	ra.ClientPublicByServer[b.ServerPublic] = d
	roster := Roster{"tn", authority(a), 1, []Member{ra, row(b)}, 300, "", false, 0}
	if err := m.ApplyRoster(roster, time.Now()); err != nil {
		t.Fatal(err)
	}
	invoke(t, m, "membership-init", InitParams{"second", "other", authority(a), Migration{"new", "", true}, []Member{}})
	roster.TailnetID = "other"
	if err := m.ApplyRoster(roster, time.Now()); err != nil {
		t.Fatal(err)
	}
	m.mu.Lock()
	m.leases["tn"] = time.Now().Add(-time.Second)
	m.mu.Unlock()
	m.Expire()
	if !m.Admits(d) {
		t.Fatal("one expiry cut off other membership")
	}
	m.mu.Lock()
	m.leases["other"] = time.Now().Add(-time.Second)
	m.mu.Unlock()
	m.Expire()
	if m.Admits(d) || m.Readiness().Member {
		t.Fatal("expiry did not close admission")
	}
	path := m.path
	m.Close()
	again, err := Open(path, b, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer again.Close()
	if again.Readiness().Member || again.Admits(d) {
		t.Fatal("restart restored lease")
	}
}

func TestTailnetShutdownIsConfirmedAuthorityTerminalAndRevokesMembers(t *testing.T) {
	a, b := identity("a"), identity("b")
	m := openTest(t, a)
	initStore(t, m, a, a, b)
	ctx := context.Background()
	if _, err := m.Execute(ctx, request("tailnet-shutdown", ShutdownParams{OperationID: "shut", TailnetID: "tn", ExpectedRevision: 1, Confirmed: false, Reason: "org-delete"})); Code(err) != "CONFIRMATION_REQUIRED" {
		t.Fatal("unconfirmed shutdown accepted", err)
	}
	if _, err := m.Execute(ctx, request("tailnet-shutdown", ShutdownParams{OperationID: "shut", TailnetID: "tn", ExpectedRevision: 7, Confirmed: true, Reason: "org-delete"})); Code(err) != "REVISION_CONFLICT" {
		t.Fatal("stale shutdown accepted", err)
	}
	if m.store.Tailnets["tn"].Revision != 1 {
		t.Fatal("refused shutdown mutated the store")
	}
	member := openTest(t, b)
	initStore(t, member, a)
	if err := member.ApplyRoster(Roster{TailnetID: "tn", Authority: authority(a), Revision: 1, Members: []Member{row(a), row(b)}, LeaseSeconds: 300}, time.Now()); err != nil {
		t.Fatal(err)
	}
	if _, err := member.Execute(ctx, request("tailnet-shutdown", ShutdownParams{OperationID: "shut", TailnetID: "tn", ExpectedRevision: 1, Confirmed: true, Reason: "org-delete"})); Code(err) != "AUTHORITY_REQUIRED" {
		t.Fatal("non-authority shutdown accepted", err)
	}
	data := invoke(t, m, "tailnet-shutdown", ShutdownParams{OperationID: "shut", TailnetID: "tn", ExpectedRevision: 1, Confirmed: true, Reason: "org-delete"})
	var shutdown struct {
		TailnetID   string `json:"tailnetId"`
		Revision    uint64 `json:"revision"`
		Closed      bool   `json:"closed"`
		ClosedAtMs  int64  `json:"closedAtMs"`
		BackupPath  string `json:"backupPath"`
		Propagation string `json:"propagation"`
	}
	json.Unmarshal(data, &shutdown)
	if !shutdown.Closed || shutdown.Revision != 2 || shutdown.ClosedAtMs <= 0 || shutdown.BackupPath == "" || shutdown.Propagation != "lease-bounded" {
		t.Fatalf("bad shutdown receipt: %s", data)
	}
	tn := m.store.Tailnets["tn"]
	if !tn.Closed || tn.Revision != 2 {
		t.Fatal("shutdown did not close the tailnet terminally")
	}
	for _, row := range tn.Members {
		if row.State != "tombstone" || row.Address != "" || len(row.ClientPublicByServer) != 0 {
			t.Fatal("shutdown left an active member row", row.DeviceID)
		}
	}
	if m.Readiness().Member {
		t.Fatal("closed authority kept admitting")
	}
	replayed, err := m.Execute(ctx, request("tailnet-shutdown", ShutdownParams{OperationID: "shut", TailnetID: "tn", ExpectedRevision: 1, Confirmed: true, Reason: "org-delete"}))
	if err != nil || string(replayed) != string(data) {
		t.Fatal("shutdown receipt is not replayable", err)
	}
	if _, err := m.Execute(ctx, request("member-announce", AnnounceParams{OperationID: "after", TailnetID: "tn", Address: a.Address, ClientPublicByServer: map[string]string{}})); Code(err) != "MEMBER_REVOKED" {
		t.Fatal("announce survived shutdown", err)
	}
	if _, err := m.Execute(ctx, request("invite-create", CreateParams{OperationID: "invite", TailnetID: "tn", Owner: b.Owner, ExpiresAtMs: time.Now().Add(time.Minute).UnixMilli()})); Code(err) != "MEMBER_REVOKED" {
		t.Fatal("invite creation survived shutdown", err)
	}
	fetched := invoke(t, m, "roster-fetch", FetchParams{TailnetID: "tn"})
	var roster Roster
	json.Unmarshal(fetched, &roster)
	if !roster.Closed || roster.Revision != 2 {
		t.Fatal("closed roster no longer answers roster-fetch")
	}
	var status Status
	json.Unmarshal(invoke(t, m, "membership-status", struct {
		TailnetID string `json:"tailnetId,omitempty"`
	}{}), &status)
	if len(status.Tailnets) != 1 || !status.Tailnets[0].Closed {
		t.Fatal("status does not report the closed tailnet")
	}
}

func TestTailnetForgetRequiresClosedRosterOrDeletedProof(t *testing.T) {
	a, b := identity("a"), identity("b")
	m := openTest(t, b)
	initStore(t, m, a)
	if err := m.ApplyRoster(Roster{TailnetID: "tn", Authority: authority(a), Revision: 1, Members: []Member{row(a), row(b)}, LeaseSeconds: 300}, time.Now()); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if _, err := m.Execute(ctx, request("tailnet-forget", ForgetParams{OperationID: "forget", TailnetID: "tn", OrgDeleted: false})); Code(err) != "CONFIRMATION_REQUIRED" {
		t.Fatal("forget without proof accepted", err)
	}
	if m.store.Tailnets["tn"] == nil {
		t.Fatal("refused forget deleted the tailnet")
	}
	invoke(t, m, "tailnet-forget", ForgetParams{OperationID: "forget", TailnetID: "tn", OrgDeleted: true})
	if m.store.Tailnets["tn"] != nil {
		t.Fatal("forget did not remove the local tailnet entry")
	}
	for _, receipt := range m.store.Operations {
		if receipt.TailnetID == "tn" {
			t.Fatal("forget left a receipt referencing the removed tailnet")
		}
	}
	if _, err := m.Execute(ctx, request("roster-fetch", FetchParams{TailnetID: "tn"})); Code(err) != "MEMBERSHIP_NOT_INITIALIZED" {
		t.Fatal("forgotten tailnet still serves rosters", err)
	}
}

func TestClosedRosterStopsMemberAdmissionAndAnnounce(t *testing.T) {
	a, b := identity("a"), identity("b")
	authorityManager := openTest(t, a)
	initStore(t, authorityManager, a, a, b)
	m := openTest(t, b)
	initStore(t, m, a)
	if err := m.ApplyRoster(Roster{TailnetID: "tn", Authority: authority(a), Revision: 1, Members: []Member{row(a), row(b)}, LeaseSeconds: 300}, time.Now()); err != nil {
		t.Fatal(err)
	}
	invoke(t, authorityManager, "tailnet-shutdown", ShutdownParams{OperationID: "shut", TailnetID: "tn", ExpectedRevision: 1, Confirmed: true, Reason: "org-delete"})
	var closed Roster
	json.Unmarshal(invoke(t, authorityManager, "roster-fetch", FetchParams{TailnetID: "tn"}), &closed)
	if !closed.Closed {
		t.Fatal("authority did not publish a closed roster")
	}
	if err := m.ApplyRoster(closed, time.Now()); err != nil {
		t.Fatal("member rejected the closed roster", err)
	}
	if !m.store.Tailnets["tn"].Closed || m.Readiness().Member {
		t.Fatal("closed roster did not stop member admission")
	}
	if _, err := m.Execute(context.Background(), request("member-announce", AnnounceParams{OperationID: "announce", TailnetID: "tn", Address: b.Address, ClientPublicByServer: map[string]string{}})); Code(err) != "MEMBER_REVOKED" {
		t.Fatal("announce survived a closed roster", err)
	}
	invoke(t, m, "tailnet-forget", ForgetParams{OperationID: "forget", TailnetID: "tn", OrgDeleted: false})
	if m.store.Tailnets["tn"] != nil {
		t.Fatal("member could not forget a closed tailnet")
	}
}

func TestTailnetForgetRefusesTheAuthorityOfAnOpenTailnet(t *testing.T) {
	a, b := identity("a"), identity("b")
	m := openTest(t, a)
	initStore(t, m, a, a, b)
	ctx := context.Background()
	if _, err := m.Execute(ctx, request("tailnet-forget", ForgetParams{OperationID: "forget", TailnetID: "tn", OrgDeleted: true})); Code(err) != "LAST_AUTHORITY_REFUSED" {
		t.Fatal("authority forgot its open tailnet", err)
	}
	if tn := m.store.Tailnets["tn"]; tn == nil || tn.Closed {
		t.Fatal("refused forget changed the open tailnet")
	}
	invoke(t, m, "tailnet-shutdown", ShutdownParams{OperationID: "shut", TailnetID: "tn", ExpectedRevision: m.store.Tailnets["tn"].Revision, Confirmed: true, Reason: "org-delete"})
	invoke(t, m, "tailnet-forget", ForgetParams{OperationID: "forget", TailnetID: "tn", OrgDeleted: false})
	if m.store.Tailnets["tn"] != nil {
		t.Fatal("authority could not forget its closed tailnet")
	}
}

func TestMemberFetchesClosedRosterRemotelyAfterAuthorityRestart(t *testing.T) {
	a, b := identity("a"), identity("b")
	authority := openTest(t, a)
	initStore(t, authority, a, a, b)
	derived := key.NewNode().Public().String()
	current := authority
	dial := func(ctx context.Context, pin Authority) (net.Conn, error) {
		client, server := net.Pipe()
		go current.ServeControl(server, derived)
		return client, nil
	}
	member, err := Open(filepath.Join(t.TempDir(), "state", "membership.json"), b, func(string) (string, error) { return derived, nil }, dial)
	if err != nil {
		t.Fatal(err)
	}
	defer member.Close()
	initStore(t, member, a)
	invoke(t, member, "roster-fetch", FetchParams{"tn"})
	if !member.Readiness().Member {
		t.Fatal("member did not become ready before shutdown")
	}
	invoke(t, authority, "tailnet-shutdown", ShutdownParams{OperationID: "shut", TailnetID: "tn", ExpectedRevision: authority.store.Tailnets["tn"].Revision, Confirmed: true, Reason: "org-delete"})
	path := authority.path
	authority.Close()
	reopened, err := Open(path, a, nil, nil)
	if err != nil {
		t.Fatal("closed tailnet store did not reopen", err)
	}
	defer reopened.Close()
	if tn := reopened.store.Tailnets["tn"]; tn == nil || !tn.Closed {
		t.Fatal("closed tailnet lost across restart")
	}
	current = reopened
	var fetched Roster
	json.Unmarshal(invoke(t, member, "roster-fetch", FetchParams{"tn"}), &fetched)
	if !fetched.Closed {
		t.Fatal("remote fetch did not return the closed roster")
	}
	if tn := member.store.Tailnets["tn"]; tn == nil || !tn.Closed || member.Readiness().Member {
		t.Fatal("member did not apply the closed roster")
	}
}
