package keys

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"go4.org/mem"
	"tailscale.com/types/key"
)

func TestGenerateModeAndNoSecrets(t *testing.T) {
	path := filepath.Join(t.TempDir(), "dev.key")
	public, err := Generate(path, false)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if mode := info.Mode().Perm(); mode != 0600 {
		t.Fatalf("mode = %o, want 0600", mode)
	}
	if !strings.HasPrefix(public.ServerPublic, "nodekey:") ||
		!strings.HasPrefix(public.ClientPublic, "nodekey:") {
		t.Fatalf("public keys malformed: %+v", public)
	}
	if public.ServerPublic == public.ClientPublic {
		t.Fatal("server and client identities must differ")
	}

	// The file must parse back into the same identities.
	material, err := Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if material.ServerPublic().String() != public.ServerPublic ||
		material.ClientPublic().String() != public.ClientPublic {
		t.Fatal("Load round trip changed identities")
	}

	// The public projection must not contain private keys or the PSK.
	if strings.Contains(public.ServerPublic, "privkey:") ||
		strings.Contains(public.ClientPublic, "privkey:") ||
		strings.Contains(public.ServerPublic, "psk:") {
		t.Fatal("public output leaks secret material")
	}
}

func TestDeriveClientPrivatePerServer(t *testing.T) {
	master, err := key.ParseNodePrivateUntyped(mem.S(
		"000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
	))
	if err != nil {
		t.Fatal(err)
	}
	serverA, err := key.ParseNodePublicUntyped(mem.S(
		"202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f",
	))
	if err != nil {
		t.Fatal(err)
	}
	serverB, err := key.ParseNodePublicUntyped(mem.S(
		"212122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f",
	))
	if err != nil {
		t.Fatal(err)
	}

	first, err := DeriveClientPrivate(master, serverA)
	if err != nil {
		t.Fatal(err)
	}
	second, err := DeriveClientPrivate(master, serverA)
	if err != nil {
		t.Fatal(err)
	}
	other, err := DeriveClientPrivate(master, serverB)
	if err != nil {
		t.Fatal(err)
	}
	if !first.Equal(second) {
		t.Fatal("same master and server did not derive the same key")
	}
	if first.Equal(master) || first.Equal(other) {
		t.Fatal("derivation did not separate the master and target server domains")
	}
	const want = "nodekey:8d780ab3155fca1027d050ad228859815aee20f85efcb9227960edc9bc689b46"
	if got := first.Public().String(); got != want {
		t.Fatalf("derived public = %s, want domain-separated vector %s", got, want)
	}
}

func TestGenerateRefusesOverwrite(t *testing.T) {
	path := filepath.Join(t.TempDir(), "dev.key")
	first, err := Generate(path, false)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	if _, err := Generate(path, false); !errors.Is(err, ErrExists) {
		t.Fatalf("second Generate = %v, want ErrExists", err)
	}
	// Untouched: the same identities still load.
	material, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if material.ServerPublic().String() != first.ServerPublic {
		t.Fatal("existing key file was overwritten without force")
	}
	// --force regenerates.
	second, err := Generate(path, true)
	if err != nil {
		t.Fatalf("forced Generate: %v", err)
	}
	if second.ServerPublic == first.ServerPublic {
		t.Fatal("forced Generate kept the old identity")
	}
	info, _ := os.Stat(path)
	if mode := info.Mode().Perm(); mode != 0600 {
		t.Fatalf("mode after force = %o, want 0600", mode)
	}
}

func TestLoadRejectsBadFiles(t *testing.T) {
	dir := t.TempDir()
	cases := map[string]string{
		"missing.json":   "",
		"badjson.key":    "{",
		"wrongv.key":     `{"v":2,"server":{"private":"x","presharedKey":"y"},"client":{"private":"z"}}`,
		"badprivate.key": `{"v":3,"server":{"private":"privkey:nope","presharedKey":"psk:0000000000000000000000000000000000000000000000000000000000000000"},"client":{"private":"privkey:nope"}}`,
		"badpsk.key":     `{"v":3,"server":{"private":"privkey:0000000000000000000000000000000000000000000000000000000000000000","presharedKey":"psk:nope"},"client":{"private":"privkey:0000000000000000000000000000000000000000000000000000000000000000"}}`,
	}
	for name, content := range cases {
		path := filepath.Join(dir, name)
		if content != "" {
			if err := os.WriteFile(path, []byte(content), 0600); err != nil {
				t.Fatal(err)
			}
		}
		if _, err := Load(path); !errors.Is(err, ErrInvalid) {
			t.Fatalf("Load(%s) = %v, want ErrInvalid", name, err)
		}
	}
}
