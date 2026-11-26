// Gate unit tests: keys mode narrows membership (never widens), empty set
// refuses everyone, set equality ignores order, key encoding requires
// canonical lowercase nodekey:<64-hex> entries with no duplicates and a
// bounded set size.
package exposuregate

import (
	"errors"
	"fmt"
	"testing"

	"go4.org/mem"
	"tailscale.com/types/key"
)

const (
	hexA = "0000000000000000000000000000000000000000000000000000000000000001"
	hexB = "0000000000000000000000000000000000000000000000000000000000000002"
	hexC = "0000000000000000000000000000000000000000000000000000000000000003"

	keyA = "nodekey:" + hexA
	keyB = "nodekey:" + hexB
	keyC = "nodekey:" + hexC

	zeroHex  = "0000000000000000000000000000000000000000000000000000000000000000"
	lowerHex = "0000000000000000000000000000000000000000000000000000000000000abc"
	upperHex = "0000000000000000000000000000000000000000000000000000000000000ABC"
)

func peerKey(t *testing.T, hex string) key.NodePublic {
	t.Helper()
	pub, err := key.ParseNodePublicUntyped(mem.S(hex))
	if err != nil {
		t.Fatalf("parse %s: %v", hex, err)
	}
	return pub
}

func mustGate(t *testing.T, mode string, keys []string) Gate {
	t.Helper()
	gate, err := New(mode, keys)
	if err != nil {
		t.Fatalf("New(%q, %v): %v", mode, keys, err)
	}
	return gate
}

func TestKeysModeAdmitsOnlyListedMembers(t *testing.T) {
	gate := mustGate(t, ModeKeys, []string{keyA})
	if !gate.Admits(true, true, peerKey(t, hexA)) {
		t.Fatal("listed member was refused")
	}
	if gate.Admits(true, true, peerKey(t, hexB)) {
		t.Fatal("unlisted member was admitted")
	}
}

func TestKeysModeStillRequiresMembership(t *testing.T) {
	gate := mustGate(t, ModeKeys, []string{keyA})
	if gate.Admits(false, true, peerKey(t, hexA)) {
		t.Fatal("a listed key that is not a member was admitted")
	}
	if gate.Admits(true, false, peerKey(t, hexA)) {
		t.Fatal("a connection whose peer key could not be established was admitted")
	}
}

func TestKeysModeEmptySetAdmitsNobody(t *testing.T) {
	for name, keys := range map[string][]string{"absent": nil, "empty": {}} {
		gate := mustGate(t, ModeKeys, keys)
		for _, hex := range []string{hexA, hexB} {
			if gate.Admits(true, true, peerKey(t, hex)) {
				t.Fatalf("%s key set admitted a member", name)
			}
		}
	}
}

func TestKeySetIgnoresOrder(t *testing.T) {
	gate := mustGate(t, ModeKeys, []string{keyA, keyB})
	if gate.KeyCount() != 2 {
		t.Fatalf("KeyCount = %d, want 2", gate.KeyCount())
	}
	if !gate.SameKeys(mustGate(t, ModeKeys, []string{keyB, keyA})) {
		t.Fatal("reordered keys counted as a different set")
	}
	if gate.SameKeys(mustGate(t, ModeKeys, []string{keyA})) {
		t.Fatal("a smaller set counted as the same set")
	}
	if gate.SameKeys(mustGate(t, ModeKeys, []string{keyA, keyB, keyC})) {
		t.Fatal("a larger set counted as the same set")
	}
	if gate.SameKeys(mustGate(t, ModeKeys, nil)) {
		t.Fatal("the empty set counted as the same set")
	}
}

func TestAllowAndAnyModesFollowMembershipOnly(t *testing.T) {
	allow := mustGate(t, ModeAllow, nil)
	any := mustGate(t, ModeAny, nil)
	unestablished := key.NodePublic{}
	if !allow.Admits(true, true, peerKey(t, hexA)) {
		t.Fatal("allow mode refused a member")
	}
	if allow.Admits(true, false, unestablished) {
		t.Fatal("allow mode admitted an unestablished peer key")
	}
	if allow.Admits(false, true, peerKey(t, hexA)) {
		t.Fatal("allow mode admitted a non-member")
	}
	if !any.Admits(false, false, unestablished) {
		t.Fatal("any mode refused a connection")
	}
}

func TestNewRejectsMalformedInput(t *testing.T) {
	cases := map[string]struct {
		mode string
		keys []string
	}{
		"keys on allow":  {ModeAllow, []string{keyA}},
		"keys on any":    {ModeAny, []string{keyA}},
		"unknown mode":   {"open", nil},
		"empty mode":     {"", nil},
		"not a nodekey":  {ModeKeys, []string{"alice"}},
		"short key":      {ModeKeys, []string{"nodekey:abcd"}},
		"long key":       {ModeKeys, []string{"nodekey:" + hexA + "00"}},
		"non-hex key":    {ModeKeys, []string{"nodekey:zz" + hexA[2:]}},
		"other key type": {ModeKeys, []string{"privkey:" + hexA}},
		"empty entry":    {ModeKeys, []string{""}},
		"zero key":       {ModeKeys, []string{"nodekey:" + zeroHex}},
		"one bad entry":  {ModeKeys, []string{keyA, "nodekey:zz"}},
	}
	for name, tc := range cases {
		if _, err := New(tc.mode, tc.keys); err == nil {
			t.Fatalf("%s was accepted", name)
		}
	}
}

func TestKeyEncodingRequiresCanonicalUniqueBoundedKeys(t *testing.T) {
	lower := "nodekey:" + lowerHex
	gate := mustGate(t, ModeKeys, []string{lower})
	if !gate.Admits(true, true, peerKey(t, lowerHex)) || gate.KeyCount() != 1 {
		t.Fatal("canonical lowercase nodekey was rejected")
	}
	rejected := map[string][]string{
		"upper case hex":    {"nodekey:" + upperHex},
		"missing prefix":    {lowerHex},
		"upper case prefix": {"NODEKEY:" + lowerHex},
		"duplicate entries": {lower, lower},
	}
	for name, keys := range rejected {
		if _, err := New(ModeKeys, keys); !errors.Is(err, ErrBadInput) {
			t.Fatalf("%s: err = %v, want ErrBadInput", name, err)
		}
	}

	atCap := make([]string, MaxKeys)
	for i := range atCap {
		atCap[i] = fmt.Sprintf("nodekey:%064x", i+1)
	}
	capped := mustGate(t, ModeKeys, atCap)
	if capped.KeyCount() != MaxKeys {
		t.Fatalf("KeyCount at cap = %d, want %d", capped.KeyCount(), MaxKeys)
	}
	overCap := append(atCap, fmt.Sprintf("nodekey:%064x", MaxKeys+1))
	if _, err := New(ModeKeys, overCap); !errors.Is(err, ErrBadInput) {
		t.Fatalf("over-cap keys: err = %v, want ErrBadInput", err)
	}
}
