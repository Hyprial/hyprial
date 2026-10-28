package membership

import (
	"bytes"
	"encoding/json"
	"io"
	"reflect"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/tailscale/tailcat"
	"go4.org/mem"
	"tailscale.com/types/key"
)

// Decode rejects duplicate keys at every depth, null values, unknown fields,
// malformed UTF-8 and trailing JSON. Required-field checks are separate.
func Decode(data []byte, v any) error {
	if len(data) > MaxFrame || !utf8.Valid(data) {
		return fail("BAD_REQUEST")
	}
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	if err := uniqueValue(d); err != nil {
		return fail("BAD_REQUEST")
	}
	if _, err := d.Token(); err != io.EOF {
		return fail("BAD_REQUEST")
	}
	d = json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if d.Decode(v) != nil {
		return fail("BAD_REQUEST")
	}
	return nil
}
func uniqueValue(d *json.Decoder) error {
	token, err := d.Token()
	if err != nil {
		return err
	}
	delim, ok := token.(json.Delim)
	if !ok {
		return nil
	}
	if delim == '{' {
		seen := map[string]bool{}
		for d.More() {
			k, err := d.Token()
			if err != nil {
				return err
			}
			s, ok := k.(string)
			if !ok || seen[s] {
				return fail("BAD_REQUEST")
			}
			seen[s] = true
			if err = uniqueValue(d); err != nil {
				return err
			}
		}
	} else if delim == '[' {
		for d.More() {
			if err := uniqueValue(d); err != nil {
				return err
			}
		}
	} else {
		return fail("BAD_REQUEST")
	}
	_, err = d.Token()
	return err
}
func required(data []byte, t reflect.Type) error {
	if t.Kind() == reflect.Pointer && bytes.Equal(bytes.TrimSpace(data), []byte("null")) {
		return nil
	}
	if t.Kind() == reflect.Pointer {
		t = t.Elem()
	}
	if bytes.Equal(bytes.TrimSpace(data), []byte("null")) {
		return fail("BAD_REQUEST")
	}
	switch t.Kind() {
	case reflect.Struct:
		var obj map[string]json.RawMessage
		if json.Unmarshal(data, &obj) != nil || obj == nil {
			return fail("BAD_REQUEST")
		}
		for i := 0; i < t.NumField(); i++ {
			f := t.Field(i)
			tag := strings.Split(f.Tag.Get("json"), ",")
			raw, ok := obj[tag[0]]
			if !ok {
				if len(tag) > 1 && tag[1] == "omitempty" {
					continue
				}
				return fail("BAD_REQUEST")
			}
			if err := required(raw, f.Type); err != nil {
				return err
			}
		}
	case reflect.Slice:
		if t.Elem().Kind() == reflect.Uint8 {
			return nil
		}
		var a []json.RawMessage
		if json.Unmarshal(data, &a) != nil {
			return fail("BAD_REQUEST")
		}
		for _, r := range a {
			if err := required(r, t.Elem()); err != nil {
				return err
			}
		}
	case reflect.Map:
		var obj map[string]json.RawMessage
		if json.Unmarshal(data, &obj) != nil || obj == nil {
			return fail("BAD_REQUEST")
		}
		for _, r := range obj {
			if err := required(r, t.Elem()); err != nil {
				return err
			}
		}
	}
	return nil
}
func decodeParams(data []byte, v any) error {
	if err := Decode(data, v); err != nil {
		return err
	}
	return required(data, reflect.TypeOf(v))
}
func ID(s string, max int) bool { return s != "" && len(s) <= max && utf8.ValidString(s) }
func public(s string) bool {
	p, err := key.ParseNodePublicUntyped(mem.S(strings.TrimPrefix(s, "nodekey:")))
	return err == nil && !p.IsZero() && p.String() == s
}
func address(s, server string, empty bool) bool {
	if s == "" {
		return empty
	}
	info, err := tailcat.ParseAddr(tailcat.Addr(s))
	return err == nil && info.ServerPublic.String() == server
}
func validAuthority(a Authority) bool {
	return ID(a.Owner, 1024) && ID(a.DeviceID, 1024) && public(a.MasterPublic) && public(a.ServerPublic) && a.MasterPublic != a.ServerPublic && address(a.Address, a.ServerPublic, false) && a.ControlPort > 0 && a.ControlPort <= 65535
}
func validSender(s Sender) bool {
	return ID(s.Owner, 1024) && ID(s.DeviceID, 1024) && public(s.MasterPublic) && public(s.ServerPublic) && s.MasterPublic != s.ServerPublic && address(s.Address, s.ServerPublic, true)
}
func sameIdentity(m Member, s Sender) bool {
	return m.DeviceID == s.DeviceID && m.Owner == s.Owner && m.MasterPublic == s.MasterPublic && m.ServerPublic == s.ServerPublic
}
func isAuthority(a Authority, s Sender) bool {
	return a.DeviceID == s.DeviceID && a.Owner == s.Owner && a.MasterPublic == s.MasterPublic && a.ServerPublic == s.ServerPublic
}
func validateStore(s Store) error {
	if s.SchemaVersion != 1 || s.Tailnets == nil || s.Operations == nil {
		return fail("MEMBERSHIP_STORE_INVALID")
	}
	identities := map[string]Member{}
	keys := map[string]string{}
	for id, tn := range s.Tailnets {
		if tn == nil || id != tn.TailnetID || !ID(id, 1024) || !validAuthority(tn.Authority) || tn.Revision > MaxNumber || tn.Members == nil || tn.Invites == nil || !tn.Migration.Completed || (tn.Migration.Source != "new" && tn.Migration.Source != "directory-0.5.5") {
			return fail("MEMBERSHIP_STORE_INVALID")
		}
		if len(tn.Members) > 1024 {
			return fail("MEMBERSHIP_LIMIT_EXCEEDED")
		}
		for device, m := range tn.Members {
			if device != m.DeviceID || !validSender(Sender{m.Owner, m.DeviceID, m.MasterPublic, m.ServerPublic, m.Address}) || m.ClientPublicByServer == nil || m.Revision < 1 || m.Revision > tn.Revision || (m.State != "active" && m.State != "tombstone") {
				return fail("MEMBERSHIP_STORE_INVALID")
			}
			if m.State == "tombstone" {
				if m.Address != "" || len(m.ClientPublicByServer) > 0 {
					return fail("MEMBERSHIP_STORE_INVALID")
				}
				continue
			}
			if prev, ok := identities[device]; ok && (prev.Owner != m.Owner || prev.MasterPublic != m.MasterPublic || prev.ServerPublic != m.ServerPublic) {
				return fail("MEMBER_CONFLICT")
			}
			identities[device] = m
			all := []string{m.MasterPublic, m.ServerPublic}
			for server, derived := range m.ClientPublicByServer {
				if !public(server) || !public(derived) || derived == m.MasterPublic || derived == m.ServerPublic {
					return fail("MEMBER_CONFLICT")
				}
				all = append(all, derived)
			}
			for _, k := range all {
				if other, ok := keys[k]; ok && other != device {
					return fail("MEMBER_CONFLICT")
				}
				keys[k] = device
			}
		}
		for inviteID, invite := range tn.Invites {
			if inviteID != invite.InviteID || !ID(inviteID, 128) || !ID(invite.Owner, 1024) || invite.Token == "" || invite.ExpiresAtMs < 0 || invite.ExpiresAtMs > MaxNumber || (invite.State != "issued" && invite.State != "redeemed" && invite.State != "revoked") || (invite.State == "redeemed" && !ID(invite.DeviceID, 1024)) {
				return fail("MEMBERSHIP_STORE_INVALID")
			}
		}
		if tn.Revision > 0 {
			a, ok := tn.Members[tn.Authority.DeviceID]
			if !ok || a.State != "active" || !isAuthority(tn.Authority, Sender{a.Owner, a.DeviceID, a.MasterPublic, a.ServerPublic, a.Address}) {
				return fail("MEMBERSHIP_STORE_INVALID")
			}
		}
	}
	for id, receipt := range s.Operations {
		if id != receipt.OperationID || !ID(id, 128) || s.Tailnets[receipt.TailnetID] == nil || !json.Valid(receipt.Request) || ValidateResult(receipt.Op, receipt.Result) != nil {
			return fail("MEMBERSHIP_STORE_INVALID")
		}
	}
	return nil
}
func integerParams(data []byte) error {
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	for {
		token, err := d.Token()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return fail("BAD_REQUEST")
		}
		if n, ok := token.(json.Number); ok {
			value, err := strconv.ParseUint(string(n), 10, 64)
			if err != nil || value > MaxNumber {
				return fail("BAD_REQUEST")
			}
		}
	}
}
func DecodeFields(data []byte, v any) error { return decodeParams(data, v) }
func ValidateResult(op string, data []byte) error {
	var value any
	switch op {
	case "membership-init":
		value = &struct {
			TailnetID   string `json:"tailnetId"`
			Revision    uint64 `json:"revision"`
			Initialized bool   `json:"initialized"`
		}{}
	case "roster-fetch":
		value = &Roster{}
	case "invite-redeem":
		value = &Roster{}
	case "member-announce":
		value = &struct {
			TailnetID string `json:"tailnetId"`
			Revision  uint64 `json:"revision"`
			Member    Member `json:"member"`
		}{}
	case "member-revoke":
		value = &RevokeResult{}
	case "membership-status":
		value = &Status{}
	case "invite-create":
		value = &struct {
			Invite InviteV2 `json:"invite"`
		}{}
	case "authority-repin":
		value = &struct {
			TailnetID       string `json:"tailnetId"`
			Revision        uint64 `json:"revision"`
			BackupPath      string `json:"backupPath"`
			RestartRequired bool   `json:"restartRequired"`
		}{}
	default:
		return fail("BAD_REQUEST")
	}
	if op == "roster-fetch" || op == "invite-redeem" {
		var fields map[string]json.RawMessage
		json.Unmarshal(data, &fields)
		_, device := fields["deviceId"]
		if device != (op == "invite-redeem") {
			return fail("BAD_REQUEST")
		}
	}
	return decodeParams(data, value)
}
func IsOp(op string) bool {
	switch op {
	case "membership-init", "roster-fetch", "member-announce", "member-revoke", "membership-status", "invite-create", "invite-redeem", "authority-repin":
		return true
	}
	return false
}
func Parse(data []byte, remote bool) (Request, error) {
	var r Request
	if err := Decode(data, &r); err != nil {
		return r, err
	}
	if err := required(data, reflect.TypeOf(Request{})); err != nil {
		return r, err
	}
	var envelope map[string]json.RawMessage
	json.Unmarshal(data, &envelope)
	if !remote && envelope["sender"] != nil {
		return r, fail("BAD_REQUEST")
	}
	if err := integerParams(r.Params); err != nil {
		return r, err
	}
	version := 6
	if remote {
		version = 1
	}
	if r.V != version {
		code := "SIDECAR_PROTOCOL_MISMATCH"
		if remote {
			code = "PEER_UPGRADE_REQUIRED"
		}
		return r, fail(code)
	}
	if !IsOp(r.Op) || !ID(r.RequestID, 128) {
		return r, fail("BAD_REQUEST")
	}
	if remote {
		if r.Sender == nil || !validSender(*r.Sender) {
			return r, fail("BAD_REQUEST")
		}
		switch r.Op {
		case "roster-fetch", "member-announce", "member-revoke", "invite-redeem":
		default:
			return r, fail("AUTHORITY_REQUIRED")
		}
	} else if r.Sender != nil {
		return r, fail("BAD_REQUEST")
	}
	var p any
	switch r.Op {
	case "membership-init":
		p = &InitParams{}
	case "roster-fetch":
		p = &FetchParams{}
	case "member-announce":
		p = &AnnounceParams{}
	case "member-revoke":
		p = &RevokeParams{}
	case "membership-status":
		p = &struct {
			TailnetID string `json:"tailnetId,omitempty"`
		}{}
	case "invite-create":
		p = &CreateParams{}
	case "invite-redeem":
		p = &RedeemParams{}
	case "authority-repin":
		p = &RepinParams{}
	}
	if err := decodeParams(r.Params, p); err != nil {
		return r, err
	}
	var fields map[string]json.RawMessage
	json.Unmarshal(r.Params, &fields)
	if op, ok := fields["operationId"]; ok {
		var s string
		json.Unmarshal(op, &s)
		if !ID(s, 128) {
			return r, fail("BAD_REQUEST")
		}
	}
	if tn, ok := fields["tailnetId"]; ok {
		var s string
		json.Unmarshal(tn, &s)
		if !ID(s, 1024) {
			return r, fail("BAD_REQUEST")
		}
	}
	if r.Op == "invite-redeem" {
		_, has := fields["clientPublicByServer"]
		if has != remote {
			return r, fail("BAD_REQUEST")
		}
	}
	return r, nil
}
