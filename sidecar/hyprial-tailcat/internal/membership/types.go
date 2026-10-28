// Package membership owns durable tailnet membership. It never reads OrgFS.
package membership

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"sync"
	"time"
)

const MaxFrame = 1 << 20
const MaxNumber = 1<<53 - 1
const Lease = 300 * time.Second
const Timeout = 30 * time.Second

type Error struct{ Value string }

func (e *Error) Error() string { return e.Value }
func fail(code string) error   { return &Error{code} }
func Code(err error) string {
	var e *Error
	if errors.As(err, &e) {
		return e.Value
	}
	return "INTERNAL"
}

type Sender struct {
	Owner        string `json:"owner"`
	DeviceID     string `json:"deviceId"`
	MasterPublic string `json:"masterPublic"`
	ServerPublic string `json:"serverPublic"`
	Address      string `json:"address"`
}
type Authority struct {
	Owner        string `json:"owner"`
	DeviceID     string `json:"deviceId"`
	MasterPublic string `json:"masterPublic"`
	ServerPublic string `json:"serverPublic"`
	Address      string `json:"address"`
	ControlPort  int    `json:"controlPort"`
}
type Member struct {
	Owner                string            `json:"owner"`
	DeviceID             string            `json:"deviceId"`
	MasterPublic         string            `json:"masterPublic"`
	ServerPublic         string            `json:"serverPublic"`
	ClientPublicByServer map[string]string `json:"clientPublicByServer"`
	Address              string            `json:"address"`
	State                string            `json:"state"`
	Revision             uint64            `json:"revision"`
}
type Migration struct {
	Source       string `json:"source"`
	SourceDigest string `json:"sourceDigest"`
	Completed    bool   `json:"completed"`
}
type Invite struct {
	InviteID    string `json:"inviteId"`
	Token       string `json:"token"`
	Owner       string `json:"owner"`
	ExpiresAtMs int64  `json:"expiresAtMs"`
	State       string `json:"state"`
	DeviceID    string `json:"deviceId"`
}
type InviteV2 struct {
	Version     int       `json:"version"`
	TailnetID   string    `json:"tailnetId"`
	Authority   Authority `json:"authority"`
	InviteID    string    `json:"inviteId"`
	Token       string    `json:"token"`
	Owner       string    `json:"owner"`
	ExpiresAtMs int64     `json:"expiresAtMs"`
}
type Tailnet struct {
	TailnetID string            `json:"tailnetId"`
	Authority Authority         `json:"authority"`
	Revision  uint64            `json:"revision"`
	Members   map[string]Member `json:"members"`
	Migration Migration         `json:"migration"`
	Invites   map[string]Invite `json:"invites"`
}
type Receipt struct {
	OperationID string          `json:"operationId"`
	Op          string          `json:"op"`
	TailnetID   string          `json:"tailnetId"`
	Request     json.RawMessage `json:"request"`
	Result      json.RawMessage `json:"result"`
}
type Store struct {
	SchemaVersion int                 `json:"schemaVersion"`
	Tailnets      map[string]*Tailnet `json:"tailnets"`
	Operations    map[string]Receipt  `json:"operations"`
}
type Request struct {
	V         int             `json:"v"`
	Op        string          `json:"op"`
	RequestID string          `json:"requestId"`
	Params    json.RawMessage `json:"params"`
	Sender    *Sender         `json:"sender,omitempty"`
}
type Response struct {
	V         int             `json:"v"`
	Event     string          `json:"event"`
	RequestID string          `json:"requestId"`
	Op        string          `json:"op"`
	Data      json.RawMessage `json:"data,omitempty"`
	Code      string          `json:"code,omitempty"`
	Message   string          `json:"message,omitempty"`
}
type InitParams struct {
	OperationID string    `json:"operationId"`
	TailnetID   string    `json:"tailnetId"`
	Authority   Authority `json:"authority"`
	Migration   Migration `json:"migration"`
	Members     []Member  `json:"members"`
}
type FetchParams struct {
	TailnetID string `json:"tailnetId"`
}
type AnnounceParams struct {
	OperationID          string            `json:"operationId"`
	TailnetID            string            `json:"tailnetId"`
	Address              string            `json:"address"`
	ClientPublicByServer map[string]string `json:"clientPublicByServer"`
}
type RevokeParams struct {
	OperationID      string   `json:"operationId"`
	TailnetID        string   `json:"tailnetId"`
	DeviceIDs        []string `json:"deviceIds"`
	ExpectedRevision uint64   `json:"expectedRevision"`
	Confirmed        bool     `json:"confirmed"`
	Reason           string   `json:"reason"`
}
type CreateParams struct {
	OperationID string `json:"operationId"`
	TailnetID   string `json:"tailnetId"`
	Owner       string `json:"owner"`
	ExpiresAtMs int64  `json:"expiresAtMs"`
}
type RedeemParams struct {
	OperationID          string            `json:"operationId"`
	Invite               InviteV2          `json:"invite"`
	ClientPublicByServer map[string]string `json:"clientPublicByServer,omitempty"`
}
type RepinParams struct {
	OperationID      string    `json:"operationId"`
	TailnetID        string    `json:"tailnetId"`
	Authority        Authority `json:"authority"`
	ExpectedRevision uint64    `json:"expectedRevision"`
	Confirmed        bool      `json:"confirmed"`
}
type Roster struct {
	TailnetID    string    `json:"tailnetId"`
	Authority    Authority `json:"authority"`
	Revision     uint64    `json:"revision"`
	Members      []Member  `json:"members"`
	LeaseSeconds int       `json:"leaseSeconds"`
	DeviceID     string    `json:"deviceId,omitempty"`
}
type RevokeResult struct {
	TailnetID   string   `json:"tailnetId"`
	Revision    uint64   `json:"revision"`
	DeviceIDs   []string `json:"deviceIds"`
	BackupPath  string   `json:"backupPath"`
	Propagation string   `json:"propagation"`
}
type Readiness struct {
	Sidecar bool `json:"sidecar"`
	Control bool `json:"control"`
	Member  bool `json:"member"`
}
type TailnetStatus struct {
	TailnetID             string   `json:"tailnetId"`
	Role                  string   `json:"role"`
	Revision              uint64   `json:"revision"`
	AuthorityDeviceID     string   `json:"authorityDeviceId"`
	MemberReady           bool     `json:"memberReady"`
	LeaseRemainingSeconds *int     `json:"leaseRemainingSeconds"`
	ActiveCount           int      `json:"activeCount"`
	TombstoneCount        int      `json:"tombstoneCount"`
	Code                  *string  `json:"code"`
	Members               []Member `json:"members"`
}
type Status struct {
	Readiness Readiness       `json:"readiness"`
	Tailnets  []TailnetStatus `json:"tailnets"`
}

type Dial func(context.Context, Authority) (net.Conn, error)
type Derive func(string) (string, error)

// Changed runs after releasing the membership mutex. The caller must close
// business streams that no longer pass admission before returning.
type Changed func(string, uint64, string)
type Manager struct {
	mu        sync.Mutex
	refreshMu sync.Mutex
	path      string
	unlock    func() error
	store     Store
	self      Sender
	derive    Derive
	dial      Dial
	changed   Changed
	leases    map[string]time.Time
	codes     map[string]string
	expanded  map[string]AnnounceParams
	quiescent func() bool
	closed    bool
	poisoned  bool
	suspended map[string]bool
	timers    map[string]*time.Timer
}

func marshal(v any) json.RawMessage { b, _ := json.Marshal(v); return b }
func (m *Manager) SetHooks(changed Changed, quiescent func() bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.changed = changed
	m.quiescent = quiescent
}
func (m *Manager) Close() {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.closed {
		m.closed = true
		for _, timer := range m.timers {
			timer.Stop()
		}
		if m.unlock != nil {
			_ = m.unlock()
		}
	}
}
