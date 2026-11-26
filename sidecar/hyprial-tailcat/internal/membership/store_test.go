package membership

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/tailscale/tailcat"
	"tailscale.com/types/key"
)

func identity(id string) Sender {
	server := key.NewNode()
	addr := tailcat.ConnInfo{ServerPublic: tailcat.NodePublic{NodePublic: server.Public()}, RegionID: 1}
	return Sender{Owner: "owner-" + id, DeviceID: id, MasterPublic: key.NewNode().Public().String(), ServerPublic: server.Public().String(), Address: string(addr.Addr())}
}
func row(s Sender) Member {
	return Member{Owner: s.Owner, DeviceID: s.DeviceID, MasterPublic: s.MasterPublic, ServerPublic: s.ServerPublic, Address: s.Address, ClientPublicByServer: map[string]string{}, State: "active", Revision: 1}
}
func authority(s Sender) Authority {
	return Authority{Owner: s.Owner, DeviceID: s.DeviceID, MasterPublic: s.MasterPublic, ServerPublic: s.ServerPublic, Address: s.Address, ControlPort: 7448}
}
func request(op string, p any) Request {
	b, _ := json.Marshal(p)
	return Request{V: 7, Op: op, RequestID: "test", Params: b}
}
func invoke(t *testing.T, m *Manager, op string, p any) json.RawMessage {
	t.Helper()
	b, err := m.Execute(context.Background(), request(op, p))
	if err != nil {
		t.Fatalf("%s: %v", op, err)
	}
	return b
}
func initStore(t *testing.T, m *Manager, a Sender, members ...Sender) {
	t.Helper()
	rows := []Member{}
	for _, s := range members {
		rows = append(rows, row(s))
	}
	invoke(t, m, "membership-init", InitParams{OperationID: "init", TailnetID: "tn", Authority: authority(a), Migration: Migration{Source: "directory-0.5.5", SourceDigest: "digest", Completed: true}, Members: rows})
}
func openTest(t *testing.T, s Sender) *Manager {
	t.Helper()
	derived := map[string]string{}
	derive := func(server string) (string, error) {
		if derived[server] == "" {
			derived[server] = key.NewNode().Public().String()
		}
		return derived[server], nil
	}
	m, err := Open(filepath.Join(t.TempDir(), "state", "membership.json"), s, derive, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { m.Close() })
	return m
}
func TestStoreImportReceiptRevokeAndRestart(t *testing.T) {
	a, b := identity("a"), identity("b")
	m := openTest(t, a)
	initStore(t, m, a, a, b)
	initStore(t, m, a, a, b) // lost-reply retry
	p := RevokeParams{OperationID: "remove", TailnetID: "tn", DeviceIDs: []string{"b"}, ExpectedRevision: 1, Confirmed: true, Reason: "test"}
	first := invoke(t, m, "member-revoke", p)
	second := invoke(t, m, "member-revoke", p)
	if string(first) != string(second) {
		t.Fatal("replay differs")
	}
	var result RevokeResult
	json.Unmarshal(first, &result)
	if st, err := os.Stat(result.BackupPath); err != nil || st.Mode().Perm() != 0600 {
		t.Fatal("no private backup", err)
	}
	path := m.path
	m.Close()
	m2, err := Open(path, a, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer m2.Close()
	r := invoke(t, m2, "roster-fetch", FetchParams{TailnetID: "tn"})
	var roster Roster
	json.Unmarshal(r, &roster)
	if roster.Revision != 2 || roster.Members[1].State != "tombstone" {
		t.Fatal("lost tombstone")
	}
	p.Reason = "different"
	_, err = m2.Execute(context.Background(), request("member-revoke", p))
	if Code(err) != "OPERATION_CONFLICT" {
		t.Fatal(err)
	}
}
func TestStoreLockAndCorruption(t *testing.T) {
	a := identity("a")
	m := openTest(t, a)
	if other, err := Open(m.path, a, nil, nil); err == nil {
		other.Close()
		t.Fatal("second writer acquired store")
	}
	m.Close()
	os.WriteFile(m.path, []byte(`{"schemaVersion":99}`), 0600)
	if _, err := Open(m.path, a, nil, nil); Code(err) != "MEMBERSHIP_STORE_INVALID" {
		t.Fatal(err)
	}
}
func TestRosterLeaseRollbackAndNoMasterAdmission(t *testing.T) {
	a, b := identity("a"), identity("b")
	m := openTest(t, b)
	initStore(t, m, a)
	roster := Roster{TailnetID: "tn", Authority: authority(a), Revision: 1, Members: []Member{row(a), row(b)}, LeaseSeconds: 300}
	start := time.Now()
	if err := m.ApplyRoster(roster, start); err != nil {
		t.Fatal(err)
	}
	if m.Admits(a.MasterPublic) {
		t.Fatal("master admitted")
	}
	if !m.Readiness().Member {
		t.Fatal("fresh roster not ready")
	}
	if err := m.ApplyRoster(roster, start.Add(-301*time.Second)); err != nil {
		t.Fatal(err)
	}
	if m.Readiness().Member {
		t.Fatal("expired roster ready")
	}
	roster.Revision = 0
	if err := m.ApplyRoster(roster, start); Code(err) != "ROSTER_ROLLBACK" {
		t.Fatal(err)
	}
}
func TestRevokeGuards(t *testing.T) {
	a, b := identity("a"), identity("b")
	m := openTest(t, a)
	initStore(t, m, a, a, b)
	for _, tc := range []struct {
		ids       []string
		confirmed bool
		code      string
	}{{[]string{"b"}, false, "CONFIRMATION_REQUIRED"}, {[]string{"a"}, true, "LAST_AUTHORITY_REFUSED"}, {[]string{"missing"}, true, "MEMBER_UNKNOWN"}} {
		_, err := m.Execute(context.Background(), request("member-revoke", RevokeParams{OperationID: tc.code, TailnetID: "tn", DeviceIDs: tc.ids, ExpectedRevision: 1, Confirmed: tc.confirmed, Reason: "test"}))
		if Code(err) != tc.code {
			t.Fatalf("want %s got %v", tc.code, err)
		}
	}
}
func TestStrictWire(t *testing.T) {
	for _, data := range []string{`{"v":7,"op":"roster-fetch","params":{"tailnetId":"tn"}}`, `{"v":7,"op":"roster-fetch","requestId":"r","params":{"tailnetId":"tn","tailnetId":"other"}}`, `{"v":7,"op":"roster-fetch","requestId":"r","params":{"tailnetId":"tn","extra":1}}`} {
		if _, err := Parse([]byte(data), false); err == nil {
			t.Fatal("accepted malformed request")
		}
	}
}
