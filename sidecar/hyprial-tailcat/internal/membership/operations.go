package membership

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"reflect"
	"sort"
	"time"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/privatefs"
)

func randomToken() string {
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic("random source unavailable")
	}
	return base64.RawURLEncoding.EncodeToString(b[:])
}
func paramsIdentity(r Request) (id, operation string) {
	var p struct {
		TailnetID   string   `json:"tailnetId"`
		OperationID string   `json:"operationId"`
		Invite      InviteV2 `json:"invite"`
	}
	json.Unmarshal(r.Params, &p)
	id = p.TailnetID
	if r.Op == "invite-redeem" {
		id = p.Invite.TailnetID
	}
	return id, p.OperationID
}
func normalized(r Request, s Sender) json.RawMessage {
	var p any
	json.Unmarshal(r.Params, &p)
	return marshal(struct {
		Op     string `json:"op"`
		Params any    `json:"params"`
		Sender Sender `json:"sender"`
	}{r.Op, p, Sender{Owner: s.Owner, DeviceID: s.DeviceID, MasterPublic: s.MasterPublic, ServerPublic: s.ServerPublic}})
}
func (m *Manager) Execute(ctx context.Context, r Request) (json.RawMessage, error) {
	parsed, err := Parse(marshal(r), false)
	if err != nil {
		return nil, err
	}
	r = parsed
	if r.Op == "authority-repin" {
		m.mu.Lock()
		q := m.quiescent
		m.mu.Unlock()
		if q == nil || !q() {
			return nil, fail("NETWORK_NOT_QUIESCENT")
		}
	}
	m.mu.Lock()
	if m.closed || m.poisoned {
		m.mu.Unlock()
		return nil, fail("MEMBERSHIP_STORE_INVALID")
	}
	id, _ := paramsIdentity(r)
	tn := m.store.Tailnets[id]
	if r.Op == "membership-status" {
		status, err := m.statusLocked(id)
		m.mu.Unlock()
		if err != nil {
			return nil, err
		}
		return bounded(status)
	}
	if r.Op == "membership-init" || r.Op == "authority-repin" || (tn != nil && isAuthority(tn.Authority, m.self)) {
		data, err := m.localLocked(r, m.self, "", false)
		hook := m.changed
		rev := uint64(0)
		if t := m.store.Tailnets[id]; t != nil {
			rev = t.Revision
		}
		m.mu.Unlock()
		if hook != nil && (err == nil || Code(err) == "MEMBERSHIP_STORE_INVALID") {
			code := ""
			if err != nil {
				code = Code(err)
			}
			hook(id, rev, code)
		}
		return data, err
	}
	if r.Op == "invite-create" {
		m.mu.Unlock()
		return nil, fail("AUTHORITY_REQUIRED")
	}
	var pin Authority
	if tn != nil {
		pin = tn.Authority
	} else if r.Op == "invite-redeem" {
		var p RedeemParams
		json.Unmarshal(r.Params, &p)
		pin = p.Invite.Authority
		if !validAuthority(pin) {
			m.mu.Unlock()
			return nil, fail("INVITE_INVALID")
		}
	} else {
		m.mu.Unlock()
		return nil, fail("MEMBERSHIP_NOT_INITIALIZED")
	}
	if r.Op == "member-revoke" {
		var p RevokeParams
		json.Unmarshal(r.Params, &p)
		if len(p.DeviceIDs) != 1 || p.DeviceIDs[0] != m.self.DeviceID {
			m.mu.Unlock()
			return nil, fail("AUTHORITY_REQUIRED")
		}
	}
	if r.Op == "member-announce" {
		r, err = m.expandLocked(r, tn)
		if err != nil {
			m.mu.Unlock()
			return nil, err
		}
	}
	if r.Op == "invite-redeem" {
		var p RedeemParams
		json.Unmarshal(r.Params, &p)
		if p.Invite.Authority != pin {
			m.mu.Unlock()
			return nil, fail("AUTHORITY_PIN_MISMATCH")
		}
		if p.Invite.Version != 2 {
			m.mu.Unlock()
			return nil, fail("INVITE_VERSION_UNSUPPORTED")
		}
		if m.derive == nil {
			m.mu.Unlock()
			return nil, fail("BAD_REQUEST")
		}
		derived, e := m.derive(pin.ServerPublic)
		if e != nil {
			m.mu.Unlock()
			return nil, e
		}
		p.ClientPublicByServer = map[string]string{pin.ServerPublic: derived}
		r.Params = marshal(p)
	}
	self := m.self
	m.mu.Unlock()
	start := time.Now()
	data, err := m.exchange(ctx, pin, r, self)
	if err != nil {
		m.noteFailure(id, Code(err))
		return nil, err
	}
	if r.Op == "roster-fetch" || r.Op == "invite-redeem" {
		snapshot := data
		if r.Op == "invite-redeem" {
			// A replayed redemption receipt may contain an old roster. Never use
			// that receipt to renew a lease; fetch the current authority snapshot.
			start = time.Now()
			snapshot, err = m.exchange(ctx, pin, Request{V: 6, Op: "roster-fetch", RequestID: randomToken(), Params: marshal(FetchParams{id})}, self)
			if err != nil {
				m.noteFailure(id, Code(err))
				return nil, err
			}
		}
		var roster Roster
		if Decode(snapshot, &roster) != nil {
			return nil, fail("BAD_REQUEST")
		}
		m.mu.Lock()
		if m.store.Tailnets[id] == nil && r.Op == "invite-redeem" {
			next := cloneStore(m.store)
			next.Tailnets[id] = &Tailnet{TailnetID: id, Authority: pin, Members: map[string]Member{}, Migration: Migration{Source: "new", SourceDigest: "", Completed: true}, Invites: map[string]Invite{}}
			err = m.commit(next)
		}
		m.mu.Unlock()
		if err == nil {
			err = m.ApplyRoster(roster, start)
			if err != nil {
				m.noteFailure(id, Code(err))
			}
		}
	} else if r.Op == "member-revoke" {
		// The committed remote receipt closes local gates immediately, without
		// inventing a full roster at the authority's new revision.
		m.mu.Lock()
		delete(m.leases, id)
		m.codes[id] = "MEMBER_REVOKED"
		hook := m.changed
		m.mu.Unlock()
		if hook != nil {
			hook(id, 0, "MEMBER_REVOKED")
		}
	}
	return data, err
}
func (m *Manager) expandLocked(r Request, tn *Tailnet) (Request, error) {
	var p AnnounceParams
	json.Unmarshal(r.Params, &p)
	if old, ok := m.expanded[p.OperationID]; ok {
		if p.TailnetID != old.TailnetID || p.Address != old.Address || (len(p.ClientPublicByServer) > 0 && !reflect.DeepEqual(p.ClientPublicByServer, old.ClientPublicByServer)) {
			return r, fail("OPERATION_CONFLICT")
		}
		r.Params = marshal(old)
		return r, nil
	}
	if m.derive == nil {
		return r, fail("BAD_REQUEST")
	}
	generated := map[string]string{}
	for _, member := range tn.Members {
		if member.State != "active" {
			continue
		}
		d, err := m.derive(member.ServerPublic)
		if err != nil {
			return r, err
		}
		generated[member.ServerPublic] = d
	}
	if len(p.ClientPublicByServer) > 0 && !reflect.DeepEqual(p.ClientPublicByServer, generated) {
		return r, fail("MEMBER_CONFLICT")
	}
	if p.Address != m.self.Address {
		return r, fail("MEMBER_CONFLICT")
	}
	p.ClientPublicByServer = generated
	expanded := make(map[string]AnnounceParams, len(m.expanded)+1)
	for id, old := range m.expanded {
		expanded[id] = old
	}
	expanded[p.OperationID] = p
	if err := privatefs.Write(m.path+".announcements", marshal(expanded)); err != nil {
		return r, fail("MEMBERSHIP_STORE_INVALID")
	}
	m.expanded = expanded
	r.Params = marshal(p)
	return r, nil
}
func (m *Manager) localLocked(r Request, s Sender, peer string, remote bool) (json.RawMessage, error) {
	if m.closed || m.poisoned {
		return nil, fail("MEMBERSHIP_STORE_INVALID")
	}
	id, operation := paramsIdentity(r)
	tn := m.store.Tailnets[id]
	if remote {
		if tn == nil {
			return nil, fail("MEMBERSHIP_NOT_INITIALIZED")
		}
		if !isAuthority(tn.Authority, m.self) {
			return nil, fail("AUTHORITY_REQUIRED")
		}
		if r.Op == "member-announce" {
			var p AnnounceParams
			json.Unmarshal(r.Params, &p)
			if p.ClientPublicByServer[m.self.ServerPublic] != peer || peer == "" {
				return nil, fail("MEMBER_CONFLICT")
			}
		}
		if r.Op == "member-revoke" && operation != "" {
			if receipt, ok := m.store.Operations[operation]; ok {
				if string(receipt.Request) != string(normalized(r, s)) {
					return nil, fail("OPERATION_CONFLICT")
				}
				return receipt.Result, nil
			}
		}
		if r.Op != "invite-redeem" {
			row, ok := tn.Members[s.DeviceID]
			if !ok {
				return nil, fail("MEMBER_UNKNOWN")
			}
			if row.State != "active" {
				return nil, fail("MEMBER_REVOKED")
			}
			if !sameIdentity(row, s) {
				return nil, fail("MEMBER_CONFLICT")
			}
		}
		if r.Op == "member-revoke" {
			var p RevokeParams
			json.Unmarshal(r.Params, &p)
			if len(p.DeviceIDs) != 1 || p.DeviceIDs[0] != s.DeviceID {
				return nil, fail("AUTHORITY_REQUIRED")
			}
		}
	}
	if r.Op == "invite-redeem" && tn != nil {
		var p RedeemParams
		json.Unmarshal(r.Params, &p)
		if invite, ok := tn.Invites[p.Invite.InviteID]; ok && invite.State == "redeemed" && invite.DeviceID == s.DeviceID {
			if row, ok := tn.Members[s.DeviceID]; ok && row.State == "tombstone" {
				return nil, fail("MEMBER_REVOKED")
			}
		}
	}
	normal := normalized(r, s)
	if operation != "" {
		if receipt, ok := m.store.Operations[operation]; ok {
			if string(receipt.Request) != string(normal) {
				return nil, fail("OPERATION_CONFLICT")
			}
			return receipt.Result, nil
		}
	}
	next := cloneStore(m.store)
	current := next.Tailnets[id]
	var result any
	var err error
	switch r.Op {
	case "membership-init":
		var p InitParams
		json.Unmarshal(r.Params, &p)
		if tn != nil {
			return nil, fail("MEMBERSHIP_ALREADY_INITIALIZED")
		}
		if !validAuthority(p.Authority) || !p.Migration.Completed || (p.Migration.Source != "new" && p.Migration.Source != "directory-0.5.5") {
			return nil, fail("BAD_REQUEST")
		}
		current = &Tailnet{TailnetID: id, Authority: p.Authority, Members: map[string]Member{}, Migration: p.Migration, Invites: map[string]Invite{}}
		if isAuthority(p.Authority, m.self) {
			current.Revision = 1
			for _, member := range p.Members {
				if member.State != "active" {
					return nil, fail("BAD_REQUEST")
				}
				if _, ok := current.Members[member.DeviceID]; ok {
					return nil, fail("MEMBER_CONFLICT")
				}
				member.Revision = 1
				member.ClientPublicByServer = map[string]string{}
				current.Members[member.DeviceID] = member
			}
		} else if len(p.Members) > 0 {
			return nil, fail("AUTHORITY_REQUIRED")
		}
		next.Tailnets[id] = current
		result = map[string]any{"tailnetId": id, "revision": current.Revision, "initialized": true}
	case "roster-fetch":
		if current == nil {
			return nil, fail("MEMBERSHIP_NOT_INITIALIZED")
		}
		return bounded(roster(current))
	case "member-announce":
		if current == nil {
			return nil, fail("MEMBERSHIP_NOT_INITIALIZED")
		}
		if !remote {
			r, err = m.expandLocked(r, current)
			if err != nil {
				return nil, err
			}
		}
		var p AnnounceParams
		json.Unmarshal(r.Params, &p)
		row, ok := current.Members[s.DeviceID]
		if !ok {
			return nil, fail("MEMBER_UNKNOWN")
		}
		if row.State != "active" {
			return nil, fail("MEMBER_REVOKED")
		}
		if !sameIdentity(row, s) || !address(p.Address, row.ServerPublic, false) {
			return nil, fail("MEMBER_CONFLICT")
		}
		if remote && p.ClientPublicByServer[m.self.ServerPublic] != peer {
			return nil, fail("MEMBER_CONFLICT")
		}
		if row.Address != p.Address || !reflect.DeepEqual(row.ClientPublicByServer, p.ClientPublicByServer) {
			current.Revision++
			row.Revision = current.Revision
			row.Address = p.Address
			row.ClientPublicByServer = p.ClientPublicByServer
			current.Members[s.DeviceID] = row
		}
		result = map[string]any{"tailnetId": id, "revision": current.Revision, "member": row}
	case "member-revoke":
		var p RevokeParams
		json.Unmarshal(r.Params, &p)
		if current == nil {
			return nil, fail("MEMBERSHIP_NOT_INITIALIZED")
		}
		if !p.Confirmed {
			return nil, fail("CONFIRMATION_REQUIRED")
		}
		if p.ExpectedRevision != current.Revision {
			return nil, fail("REVISION_CONFLICT")
		}
		if len(p.DeviceIDs) == 0 || !ID(p.Reason, 1024) {
			return nil, fail("BAD_REQUEST")
		}
		seen := map[string]bool{}
		for _, device := range p.DeviceIDs {
			if seen[device] {
				return nil, fail("BAD_REQUEST")
			}
			seen[device] = true
			if _, ok := current.Members[device]; !ok {
				return nil, fail("MEMBER_UNKNOWN")
			}
			if device == current.Authority.DeviceID {
				return nil, fail("LAST_AUTHORITY_REFUSED")
			}
			if device == m.self.DeviceID {
				return nil, fail("SELF_REVOKE_REFUSED")
			}
		}
		backup, err := m.backup()
		if err != nil {
			return nil, err
		}
		current.Revision++
		for _, device := range p.DeviceIDs {
			row := current.Members[device]
			row.State = "tombstone"
			row.Revision = current.Revision
			row.Address = ""
			row.ClientPublicByServer = map[string]string{}
			current.Members[device] = row
		}
		sort.Strings(p.DeviceIDs)
		result = RevokeResult{id, current.Revision, p.DeviceIDs, backup, "lease-bounded"}
	case "invite-create":
		if remote || current == nil || !isAuthority(current.Authority, m.self) {
			return nil, fail("AUTHORITY_REQUIRED")
		}
		var p CreateParams
		json.Unmarshal(r.Params, &p)
		now := time.Now().UnixMilli()
		if !ID(p.Owner, 1024) || p.ExpiresAtMs <= now || p.ExpiresAtMs > now+3600000 {
			return nil, fail("BAD_REQUEST")
		}
		invite := Invite{InviteID: randomToken(), Token: randomToken(), Owner: p.Owner, ExpiresAtMs: p.ExpiresAtMs, State: "issued", DeviceID: ""}
		current.Invites[invite.InviteID] = invite
		result = map[string]any{"invite": InviteV2{2, id, current.Authority, invite.InviteID, invite.Token, invite.Owner, invite.ExpiresAtMs}}
	case "invite-redeem":
		var p RedeemParams
		json.Unmarshal(r.Params, &p)
		if current == nil {
			return nil, fail("MEMBERSHIP_NOT_INITIALIZED")
		}
		if p.Invite.Version != 2 {
			return nil, fail("INVITE_VERSION_UNSUPPORTED")
		}
		invite, ok := current.Invites[p.Invite.InviteID]
		if !ok || p.Invite.Authority != current.Authority || p.Invite.Token != invite.Token || p.Invite.Owner != invite.Owner || p.Invite.ExpiresAtMs != invite.ExpiresAtMs || s.Owner != invite.Owner || invite.State == "revoked" {
			return nil, fail("INVITE_INVALID")
		}
		if invite.State == "redeemed" {
			if invite.DeviceID != s.DeviceID {
				return nil, fail("INVITE_CONSUMED")
			}
			row := current.Members[s.DeviceID]
			if row.State != "active" {
				return nil, fail("MEMBER_REVOKED")
			}
			if !sameIdentity(row, s) {
				return nil, fail("MEMBER_CONFLICT")
			}
			rr := roster(current)
			rr.DeviceID = s.DeviceID
			result = rr
			for _, receipt := range next.Operations {
				if receipt.Op != "invite-redeem" {
					continue
				}
				var prior struct {
					Params RedeemParams `json:"params"`
					Sender Sender       `json:"sender"`
				}
				json.Unmarshal(receipt.Request, &prior)
				if prior.Params.Invite.InviteID == invite.InviteID && prior.Sender.DeviceID == s.DeviceID {
					var original Roster
					json.Unmarshal(receipt.Result, &original)
					result = original
					break
				}
			}
			break
		}
		if time.Now().UnixMilli() >= invite.ExpiresAtMs {
			return nil, fail("INVITE_EXPIRED")
		}
		if !validSender(s) || !address(s.Address, s.ServerPublic, false) || p.ClientPublicByServer[m.self.ServerPublic] != peer || peer == "" {
			return nil, fail("MEMBER_CONFLICT")
		}
		if old, ok := current.Members[s.DeviceID]; ok && !sameIdentity(old, s) {
			return nil, fail("MEMBER_CONFLICT")
		}
		// Include our announcement for the new server in the same roster.
		// Otherwise an authority that wins dial ordering cannot reach the
		// joiner until both sides' next 60-second refresh, after join timeout.
		if m.derive == nil {
			return nil, fail("BAD_REQUEST")
		}
		derived, err := m.derive(s.ServerPublic)
		if err != nil {
			return nil, err
		}
		current.Revision++
		local := current.Members[m.self.DeviceID]
		local.ClientPublicByServer[s.ServerPublic] = derived
		local.Revision = current.Revision
		current.Members[m.self.DeviceID] = local
		current.Members[s.DeviceID] = Member{s.Owner, s.DeviceID, s.MasterPublic, s.ServerPublic, p.ClientPublicByServer, s.Address, "active", current.Revision}
		invite.State = "redeemed"
		invite.DeviceID = s.DeviceID
		current.Invites[invite.InviteID] = invite
		rr := roster(current)
		rr.DeviceID = s.DeviceID
		result = rr
	case "authority-repin":
		var p RepinParams
		json.Unmarshal(r.Params, &p)
		if current == nil {
			return nil, fail("MEMBERSHIP_NOT_INITIALIZED")
		}
		if !p.Confirmed {
			return nil, fail("CONFIRMATION_REQUIRED")
		}
		if p.ExpectedRevision != current.Revision {
			return nil, fail("REVISION_CONFLICT")
		}
		if !validAuthority(p.Authority) {
			return nil, fail("BAD_REQUEST")
		}
		row, ok := current.Members[p.Authority.DeviceID]
		if !ok || row.State != "active" || !isAuthority(p.Authority, Sender{row.Owner, row.DeviceID, row.MasterPublic, row.ServerPublic, row.Address}) {
			return nil, fail("MEMBER_UNKNOWN")
		}
		backup, err := m.backup()
		if err != nil {
			return nil, err
		}
		current.Authority = p.Authority
		result = map[string]any{"tailnetId": id, "revision": current.Revision, "backupPath": backup, "restartRequired": true}
	default:
		return nil, fail("BAD_REQUEST")
	}
	data, err := bounded(result)
	if err != nil {
		return nil, err
	}
	// Every resulting roster must fit one control frame, not only this receipt.
	if current != nil {
		if _, err := bounded(roster(current)); err != nil {
			return nil, err
		}
	}
	next.Operations[operation] = Receipt{operation, r.Op, id, normal, data}
	if err := m.commit(next); err != nil {
		return nil, err
	}
	if r.Op == "authority-repin" {
		delete(m.leases, id)
		m.suspended[id] = true
	}
	return data, nil
}
func bounded(v any) (json.RawMessage, error) {
	b := marshal(v)
	if len(b) > MaxFrame-1024 {
		return nil, fail("MEMBERSHIP_LIMIT_EXCEEDED")
	}
	return b, nil
}
