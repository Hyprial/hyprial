package membership

import (
	"bytes"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"time"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/privatefs"
)

func Open(path string, self Sender, derive Derive, dial Dial) (*Manager, error) {
	if !filepath.IsAbs(path) || !validSender(self) {
		return nil, fail("BAD_REQUEST")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return nil, fail("MEMBERSHIP_STORE_INVALID")
	}
	if err := privatefs.Directory(filepath.Dir(path)); err != nil {
		return nil, fail("MEMBERSHIP_STORE_INVALID")
	}
	unlock, err := privatefs.Lock(path + ".lock")
	if err != nil {
		return nil, fail("MEMBERSHIP_STORE_INVALID")
	}
	m := &Manager{path: path, unlock: unlock, self: self, derive: derive, dial: dial, leases: map[string]time.Time{}, codes: map[string]string{}, expanded: map[string]AnnounceParams{}, suspended: map[string]bool{}, timers: map[string]*time.Timer{}, store: Store{SchemaVersion: 1, Tailnets: map[string]*Tailnet{}, Operations: map[string]Receipt{}}}
	if _, err := os.Lstat(path); err == nil {
		if privatefs.File(path) != nil {
			unlock()
			return nil, fail("MEMBERSHIP_STORE_INVALID")
		}
		data, err := os.ReadFile(path)
		if err != nil {
			unlock()
			return nil, fail("MEMBERSHIP_STORE_INVALID")
		}
		// A store can contain several individually bounded tailnets and receipts.
		if decodeStore(data, &m.store) != nil || validateStore(m.store) != nil {
			unlock()
			return nil, fail("MEMBERSHIP_STORE_INVALID")
		}
	} else if !os.IsNotExist(err) {
		unlock()
		return nil, fail("MEMBERSHIP_STORE_INVALID")
	}
	if _, err := os.Lstat(path + ".announcements"); err == nil {
		data, readErr := os.ReadFile(path + ".announcements")
		if privatefs.File(path+".announcements") != nil || readErr != nil || decodeStore(data, &m.expanded) != nil || m.expanded == nil {
			unlock()
			return nil, fail("MEMBERSHIP_STORE_INVALID")
		}
		for id, p := range m.expanded {
			if !ID(id, 128) || p.OperationID != id || !ID(p.TailnetID, 1024) || p.ClientPublicByServer == nil {
				unlock()
				return nil, fail("MEMBERSHIP_STORE_INVALID")
			}
		}
	} else if !os.IsNotExist(err) {
		unlock()
		return nil, fail("MEMBERSHIP_STORE_INVALID")
	}
	return m, nil
}
func decodeStore(data []byte, s any) error {
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	if err := uniqueValue(d); err != nil {
		return err
	}
	if _, err := d.Token(); err != io.EOF {
		return fail("MEMBERSHIP_STORE_INVALID")
	}
	d = json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if err := d.Decode(s); err != nil {
		return err
	}
	return required(data, reflect.TypeOf(s))
}

func cloneStore(s Store) Store { var out Store; json.Unmarshal(marshal(s), &out); return out }
func (m *Manager) commit(next Store) error {
	if err := validateStore(next); err != nil {
		return err
	}
	if err := privatefs.Write(m.path, marshal(next)); err != nil {
		// A failure after rename can leave an uncertain commit. Do not keep
		// admitting from the old in-memory snapshot; require a fresh open.
		m.poisoned = true
		return fail("MEMBERSHIP_STORE_INVALID")
	}
	m.store = next
	return nil
}
func (m *Manager) backup() (string, error) {
	path := m.path + ".backup-" + randomToken()
	if err := privatefs.Write(path, marshal(m.store)); err != nil {
		return "", fail("MEMBERSHIP_BACKUP_FAILED")
	}
	return path, nil
}
func members(tn *Tailnet) []Member {
	out := make([]Member, 0, len(tn.Members))
	for _, m := range tn.Members {
		out = append(out, m)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].DeviceID < out[j].DeviceID })
	return out
}
func roster(tn *Tailnet) Roster {
	return Roster{TailnetID: tn.TailnetID, Authority: tn.Authority, Revision: tn.Revision, Members: members(tn), LeaseSeconds: 300}
}
func (m *Manager) valid(tn *Tailnet, now time.Time) bool {
	local, ok := tn.Members[m.self.DeviceID]
	return !m.closed && !m.poisoned && !m.suspended[tn.TailnetID] && ok && local.State == "active" && sameIdentity(local, m.self) && (isAuthority(tn.Authority, m.self) || now.Before(m.leases[tn.TailnetID]))
}
func (m *Manager) admitsLocked(public string) bool {
	for _, tn := range m.store.Tailnets {
		if !m.valid(tn, time.Now()) {
			continue
		}
		for _, row := range tn.Members {
			if row.State == "active" && row.ClientPublicByServer[m.self.ServerPublic] == public {
				return true
			}
		}
	}
	return false
}
func (m *Manager) Admits(public string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return !m.closed && m.admitsLocked(public)
}
func (m *Manager) AllowsAddress(addr string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.closed {
		return false
	}
	for _, tn := range m.store.Tailnets {
		if !m.valid(tn, time.Now()) {
			continue
		}
		for _, row := range tn.Members {
			if row.State == "active" && row.Address != "" && row.Address == addr {
				return true
			}
		}
	}
	return false
}
func (m *Manager) readinessLocked() Readiness {
	r := Readiness{Sidecar: !m.closed, Control: !m.closed}
	for _, tn := range m.store.Tailnets {
		if m.valid(tn, time.Now()) {
			r.Member = true
		}
	}
	return r
}
func (m *Manager) Readiness() Readiness { m.mu.Lock(); defer m.mu.Unlock(); return m.readinessLocked() }
func (m *Manager) statusLocked(id string) (Status, error) {
	out := Status{Readiness: m.readinessLocked(), Tailnets: []TailnetStatus{}}
	if id != "" && m.store.Tailnets[id] == nil {
		return out, fail("MEMBERSHIP_NOT_INITIALIZED")
	}
	for key, tn := range m.store.Tailnets {
		if id != "" && id != key {
			continue
		}
		s := TailnetStatus{TailnetID: key, Role: "member", Revision: tn.Revision, AuthorityDeviceID: tn.Authority.DeviceID, MemberReady: m.valid(tn, time.Now()), Members: members(tn)}
		if isAuthority(tn.Authority, m.self) {
			s.Role = "authority"
		} else {
			n := int(time.Until(m.leases[key]).Seconds())
			if n < 0 {
				n = 0
			}
			if n > 300 {
				n = 300
			}
			s.LeaseRemainingSeconds = &n
		}
		for _, r := range tn.Members {
			if r.State == "active" {
				s.ActiveCount++
			} else {
				s.TombstoneCount++
			}
		}
		code := m.codes[key]
		if !s.MemberReady {
			r, ok := tn.Members[m.self.DeviceID]
			if ok && r.State == "tombstone" {
				code = "MEMBER_REVOKED"
			} else if code == "" {
				code = "MEMBERSHIP_LEASE_EXPIRED"
			}
		}
		if code != "" {
			s.Code = &code
		}
		out.Tailnets = append(out.Tailnets, s)
	}
	sort.Slice(out.Tailnets, func(i, j int) bool { return out.Tailnets[i].TailnetID < out.Tailnets[j].TailnetID })
	return out, nil
}

// ApplyRoster installs one complete authority snapshot. The lease uses the
// request start, never response time, and is deliberately absent from disk.
func (m *Manager) ApplyRoster(r Roster, start time.Time) error {
	m.mu.Lock()
	err := m.applyRosterLocked(r, start)
	hook := m.changed
	m.mu.Unlock()
	if err == nil && hook != nil {
		hook(r.TailnetID, r.Revision, "")
	}
	return err
}
func (m *Manager) applyRosterLocked(r Roster, start time.Time) error {
	old := m.store.Tailnets[r.TailnetID]
	if old == nil {
		return fail("MEMBERSHIP_NOT_INITIALIZED")
	}
	if old.Authority != r.Authority {
		return fail("AUTHORITY_PIN_MISMATCH")
	}
	if r.Revision < old.Revision {
		return fail("ROSTER_ROLLBACK")
	}
	if r.LeaseSeconds != 300 || len(r.Members) > 1024 || r.Revision == 0 {
		return fail("MEMBERSHIP_LIMIT_EXCEEDED")
	}
	next := cloneStore(m.store)
	tn := next.Tailnets[r.TailnetID]
	tn.Revision = r.Revision
	tn.Members = map[string]Member{}
	for _, row := range r.Members {
		if previous, ok := old.Members[row.DeviceID]; ok && (previous.Owner != row.Owner || previous.MasterPublic != row.MasterPublic || previous.ServerPublic != row.ServerPublic) {
			return fail("MEMBER_CONFLICT")
		}
		if _, ok := tn.Members[row.DeviceID]; ok {
			return fail("MEMBER_CONFLICT")
		}
		tn.Members[row.DeviceID] = row
	}
	for device := range old.Members {
		if _, ok := tn.Members[device]; !ok {
			return fail("ROSTER_ROLLBACK")
		}
	}
	if r.Revision == old.Revision && string(marshal(members(tn))) != string(marshal(members(old))) {
		return fail("ROSTER_ROLLBACK")
	}
	if err := m.commit(next); err != nil {
		return err
	}
	m.leases[r.TailnetID] = start.Add(Lease)
	if old := m.timers[r.TailnetID]; old != nil {
		old.Stop()
	}
	m.timers[r.TailnetID] = time.AfterFunc(time.Until(start.Add(Lease)), m.Expire)
	delete(m.codes, r.TailnetID)
	return nil
}
