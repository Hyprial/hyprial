// Package exposuregate decides whether one inbound connection may use an
// exposed port. It holds the admission rule and nothing else: the engine
// resolves the peer and the membership answer, then asks the gate.
//
// Decision record:
//   - Three modes. "any" admits every connection. "allow" admits a roster
//     member whose tunnel key is established. "keys" admits a roster member
//     whose established tunnel key is in the port's set, so the set only
//     ever narrows membership.
//   - An empty "keys" set admits nobody. A grant that names no device must
//     not fall back to "every member".
//   - Keys require canonical spelling: the "nodekey:" prefix followed by 64
//     lowercase hexadecimal characters, matching membership roster keys.
//     Zero keys, duplicates, and sets exceeding MaxKeys (4096) are refused.
package exposuregate

import (
	"errors"
	"fmt"
	"strings"

	"github.com/tailscale/tailcat"
	"go4.org/mem"
	"tailscale.com/types/key"
)

// Admission mode names evaluated by Gate.
const (
	ModeAny   = "any"
	ModeAllow = "allow"
	ModeKeys  = "keys"
)

// MaxKeys bounds the number of node public keys allowed in one keys-mode
// exposure so oversized requests are rejected cleanly with BAD_REQUEST instead
// of tripping the stdio line limit.
const MaxKeys = 4096

// ErrBadInput marks invalid admission modes or malformed key sets.
var ErrBadInput = errors.New("bad input")

// Gate holds the immutable admission rule for one exposed port.
type Gate struct {
	mode   string
	keys   *tailcat.KeySet
	listed []key.NodePublic
}

// New validates mode and keys and builds a Gate.
func New(mode string, keys []string) (Gate, error) {
	if mode != ModeAny && mode != ModeAllow && mode != ModeKeys {
		return Gate{}, fmt.Errorf("%w: admission must be any, allow or keys", ErrBadInput)
	}
	if mode != ModeKeys && len(keys) > 0 {
		return Gate{}, fmt.Errorf("%w: keys are only valid with admission keys", ErrBadInput)
	}
	if mode == ModeKeys && len(keys) > MaxKeys {
		return Gate{}, fmt.Errorf("%w: keys exceeds maximum of %d entries", ErrBadInput, MaxKeys)
	}
	set := &tailcat.KeySet{}
	listed := make([]key.NodePublic, 0, len(keys))
	for _, entry := range keys {
		pub, err := parseKey(entry)
		if err != nil {
			return Gate{}, err
		}
		if set.Contains(pub) {
			return Gate{}, fmt.Errorf("%w: duplicate key in keys", ErrBadInput)
		}
		set.Add(pub)
		listed = append(listed, pub)
	}
	return Gate{mode: mode, keys: set, listed: listed}, nil
}

func parseKey(entry string) (key.NodePublic, error) {
	if !strings.HasPrefix(entry, "nodekey:") {
		return key.NodePublic{}, fmt.Errorf("%w: keys entries must be canonical nodekeys", ErrBadInput)
	}
	pub, err := key.ParseNodePublicUntyped(mem.S(strings.TrimPrefix(entry, "nodekey:")))
	if err != nil || pub.IsZero() || pub.String() != entry {
		return key.NodePublic{}, fmt.Errorf("%w: keys entries must be canonical nodekeys", ErrBadInput)
	}
	return pub, nil
}

// Admits reports whether a connection with the given membership status and
// peer key passes the gate.
func (g Gate) Admits(member, haveKey bool, peer key.NodePublic) bool {
	switch g.mode {
	case ModeAny:
		return true
	case ModeAllow:
		return member && haveKey
	case ModeKeys:
		return member && haveKey && g.keys != nil && g.keys.Contains(peer)
	}
	return false
}

// KeyCount reports the number of distinct keys in a keys-mode gate.
func (g Gate) KeyCount() int {
	return len(g.listed)
}

// SameKeys reports whether g and other are both keys-mode gates with identical
// key sets regardless of input order.
func (g Gate) SameKeys(other Gate) bool {
	if g.mode != ModeKeys || other.mode != ModeKeys || len(g.listed) != len(other.listed) {
		return false
	}
	for _, pub := range g.listed {
		if !other.keys.Contains(pub) {
			return false
		}
	}
	return true
}
