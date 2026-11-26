// Gate unit tests: keys mode narrows membership (never widens), empty set
// refuses everyone, set equality ignores order/duplicates, key encoding matches
// the retired allow list (64 hex chars, optional lowercase nodekey: prefix).
package exposuregate

import (
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

func TestKeySetIgnoresOrderAndDuplicates(t *testing.T) {
	gate := mustGate(t, ModeKeys, []string{keyA, keyB})
	if !gate.SameKeys(mustGate(t, ModeKeys, []string{keyB, keyA, keyB})) {
		t.Fatal("reordered or duplicated keys counted as a different set")
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

func TestKeyEncodingMatchesAllowList(t *testing.T) {
	lower := "nodekey:" + lowerHex
	reference := mustGate(t, ModeKeys, []string{lower})
	for name, spelling := range map[string][]string{
		"lower case": {lower},
		"upper case": {"nodekey:" + upperHex},
		"no prefix":  {lowerHex},
		"duplicated": {lower, "nodekey:" + upperHex},
	} {
		gate := mustGate(t, ModeKeys, spelling)
		if !gate.SameKeys(reference) || !gate.Admits(true, true, peerKey(t, lowerHex)) {
			t.Fatalf("%s spelling did not resolve to the same key", name)
		}
	}
	if _, err := New(ModeKeys, []string{"NODEKEY:" + upperHex}); err == nil {
		t.Fatal("an upper-case prefix was accepted")
	}
}
