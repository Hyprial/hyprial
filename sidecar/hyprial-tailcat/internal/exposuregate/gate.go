// Package exposuregate decides whether one inbound connection may use an
// exposed port. It holds the admission rule and nothing else: the engine
// resolves the peer and the membership answer, then asks the gate.
//
// Decision record (service grant line, plan v2 §3.3, slice S4-go):
//   - Three modes. "any" admits every connection. "allow" admits a roster
//     member whose tunnel key is established. "keys" admits a roster member
//     whose established tunnel key is in the port's set, so the set only
//     ever narrows membership.
//   - An empty "keys" set admits nobody. A grant that names no device must
//     not fall back to "every member".
//   - Keys are spelled as the retired allow list spelled them: 64 hex
//     characters with an optional nodekey prefix, letter case
//     insignificant. The zero key is refused because it is the engine's
//     "no key established" value, never a device identity.
package exposuregate

import (
	"errors"
	"fmt"
	"strings"

	"github.com/tailscale/tailcat"
	"go4.org/mem"
	"tailscale.com/types/key"
)

const (
	ModeAny   = "any"
	ModeAllow = "allow"
	ModeKeys  = "keys"
)

var ErrBadInput = errors.New("bad input")

type Gate struct {
	mode   string
	keys   *tailcat.KeySet
	listed []key.NodePublic
}

func New(mode string, keys []string) (Gate, error) {
	if mode != ModeAny && mode != ModeAllow && mode != ModeKeys {
		return Gate{}, fmt.Errorf("%w: admission must be any, allow or keys", ErrBadInput)
	}
	if mode != ModeKeys && len(keys) > 0 {
		return Gate{}, fmt.Errorf("%w: keys are only valid with admission keys", ErrBadInput)
	}
	set := &tailcat.KeySet{}
	listed := make([]key.NodePublic, 0, len(keys))
	for _, entry := range keys {
		pub, err := parseKey(entry)
		if err != nil {
			return Gate{}, err
		}
		if set.Contains(pub) {
			continue
		}
		set.Add(pub)
		listed = append(listed, pub)
	}
	return Gate{mode: mode, keys: set, listed: listed}, nil
}

func parseKey(entry string) (key.NodePublic, error) {
	pub, err := key.ParseNodePublicUntyped(mem.S(strings.TrimPrefix(entry, "nodekey:")))
	if err != nil || pub.IsZero() {
		return key.NodePublic{}, fmt.Errorf("%w: keys entries must be nodekeys", ErrBadInput)
	}
	return pub, nil
}

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
