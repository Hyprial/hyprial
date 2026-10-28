package membership

import (
	"bufio"
	"context"
	"encoding/json"
	"net"
	"sync"
	"time"
)

func response(r Request, data json.RawMessage, err error) Response {
	out := Response{V: r.V, Event: "membership-result", RequestID: r.RequestID, Op: r.Op, Data: data}
	if err != nil {
		out.Event = "error"
		out.Data = nil
		out.Code = Code(err)
		out.Message = "Membership operation failed."
	}
	return out
}

// ServeControl handles exactly one bounded frame. The engine owns the listener
// and resolves peer from the actual Tailcat transport, never request JSON.
func (m *Manager) ServeControl(conn net.Conn, peer string) {
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(Timeout))
	scanner := bufio.NewScanner(conn)
	scanner.Buffer(make([]byte, 4096), MaxFrame)
	if !scanner.Scan() {
		return
	}
	r, err := Parse(scanner.Bytes(), true)
	var data json.RawMessage
	if err == nil {
		m.mu.Lock()
		if m.closed {
			err = fail("MEMBERSHIP_STORE_INVALID")
		} else {
			data, err = m.localLocked(r, *r.Sender, peer, true)
		}
		hook := m.changed
		id, _ := paramsIdentity(r)
		rev := uint64(0)
		if tn := m.store.Tailnets[id]; tn != nil {
			rev = tn.Revision
		}
		m.mu.Unlock()
		if hook != nil && ((err == nil && r.Op != "roster-fetch") || Code(err) == "MEMBERSHIP_STORE_INVALID") {
			code := ""
			if err != nil {
				code = Code(err)
			}
			hook(id, rev, code)
		}
	}
	r.V = 1
	_ = json.NewEncoder(conn).Encode(response(r, data, err))
}
func (m *Manager) exchange(ctx context.Context, pin Authority, r Request, self Sender) (json.RawMessage, error) {
	if m.dial == nil {
		return nil, fail("AUTHORITY_UNREACHABLE")
	}
	ctx, cancel := context.WithTimeout(ctx, Timeout)
	defer cancel()
	conn, err := m.dial(ctx, pin)
	if err != nil {
		return nil, fail("AUTHORITY_UNREACHABLE")
	}
	defer conn.Close()
	deadline, _ := ctx.Deadline()
	_ = conn.SetDeadline(deadline)
	done := context.AfterFunc(ctx, func() { conn.Close() })
	defer done()
	r.V = 1
	r.Sender = &self
	if err := json.NewEncoder(conn).Encode(r); err != nil {
		return nil, fail("AUTHORITY_UNREACHABLE")
	}
	scanner := bufio.NewScanner(conn)
	scanner.Buffer(make([]byte, 4096), MaxFrame)
	if !scanner.Scan() {
		return nil, fail("AUTHORITY_UNREACHABLE")
	}
	var result Response
	if Decode(scanner.Bytes(), &result) != nil {
		return nil, fail("BAD_REQUEST")
	}
	if result.V != 1 {
		return nil, fail("PEER_UPGRADE_REQUIRED")
	}
	if result.RequestID != r.RequestID || result.Op != r.Op {
		return nil, fail("BAD_REQUEST")
	}
	if result.Event == "error" {
		if result.Code == "" {
			return nil, fail("BAD_REQUEST")
		}
		return nil, fail(result.Code)
	}
	if result.Event != "membership-result" || result.Data == nil || ValidateResult(r.Op, result.Data) != nil {
		return nil, fail("BAD_REQUEST")
	}
	if time.Now().After(deadline) {
		return nil, fail("AUTHORITY_UNREACHABLE")
	}
	return result.Data, nil
}
func (m *Manager) noteFailure(id, code string) {
	m.mu.Lock()
	m.codes[id] = code
	// A definitive revocation or invalid snapshot cannot keep stale admission.
	if code == "MEMBERSHIP_STORE_INVALID" || code == "MEMBER_REVOKED" || code == "MEMBER_UNKNOWN" || code == "ROSTER_ROLLBACK" || code == "AUTHORITY_PIN_MISMATCH" {
		delete(m.leases, id)
	}
	hook := m.changed
	rev := uint64(0)
	if tn := m.store.Tailnets[id]; tn != nil {
		rev = tn.Revision
	}
	m.mu.Unlock()
	if hook != nil {
		hook(id, rev, code)
	}
}

// Run refreshes without a daemon maintenance tick. Expiry checks are separate
// from network calls so an unreachable authority cannot delay stream closure.
func (m *Manager) Run(ctx context.Context) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	refresh := time.NewTicker(60 * time.Second)
	defer refresh.Stop()
	workerDone := make(chan struct{}, 1)
	running := false
	start := func() {
		if running {
			return
		}
		running = true
		go func() { m.Refresh(ctx); workerDone <- struct{}{} }()
	}
	start()
	for {
		select {
		case <-ctx.Done():
			if running {
				<-workerDone
			}
			return
		case <-workerDone:
			running = false
		case <-refresh.C:
			start()
		case <-ticker.C:
			m.Expire()
		}
	}
}
func (m *Manager) Expire() {
	m.mu.Lock()
	if m.closed {
		m.mu.Unlock()
		return
	}
	changes := map[string]uint64{}
	for id, end := range m.leases {
		if !time.Now().Before(end) {
			delete(m.leases, id)
			m.codes[id] = "MEMBERSHIP_LEASE_EXPIRED"
			if tn := m.store.Tailnets[id]; tn != nil {
				changes[id] = tn.Revision
			}
		}
	}
	hook := m.changed
	m.mu.Unlock()
	if hook != nil {
		for id, rev := range changes {
			hook(id, rev, "MEMBERSHIP_LEASE_EXPIRED")
		}
	}
}
func (m *Manager) Refresh(ctx context.Context) {
	m.refreshMu.Lock()
	defer m.refreshMu.Unlock()
	m.mu.Lock()
	ids := []string{}
	for id := range m.store.Tailnets {
		if !m.suspended[id] {
			ids = append(ids, id)
		}
	}
	m.mu.Unlock()
	var workers sync.WaitGroup
	defer workers.Wait()
	for _, id := range ids {
		if ctx.Err() != nil {
			return
		}
		workers.Add(1)
		go func() { defer workers.Done(); m.refreshTailnet(ctx, id) }()
	}
}
func (m *Manager) refreshTailnet(ctx context.Context, id string) {
	_, err := m.Execute(ctx, Request{V: 6, Op: "roster-fetch", RequestID: randomToken(), Params: marshal(FetchParams{id})})
	if err != nil {
		return
	}
	m.mu.Lock()
	tn := m.store.Tailnets[id]
	need := false
	if tn != nil && m.valid(tn, time.Now()) {
		row := tn.Members[m.self.DeviceID]
		need = row.Address != m.self.Address
		for _, member := range tn.Members {
			if member.State == "active" && row.ClientPublicByServer[member.ServerPublic] == "" {
				need = true
			}
		}
	}
	addr := m.self.Address
	m.mu.Unlock()
	if need {
		_, _ = m.Execute(ctx, Request{V: 6, Op: "member-announce", RequestID: randomToken(), Params: marshal(AnnounceParams{randomToken(), id, addr, map[string]string{}})})
	}
}
