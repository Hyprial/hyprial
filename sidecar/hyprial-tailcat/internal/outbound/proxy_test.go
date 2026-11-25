package outbound

import (
	"net/http"
	"net/url"
	"testing"

	"tailscale.com/tailcfg"
)

func TestSnapshotAndBypass(t *testing.T) {
	env := map[string]string{"HTTPS_PROXY": "http://user:secret@proxy.invalid:8080", "https_proxy": "http://ignored.invalid", "NO_PROXY": "bypass.invalid,127.0.0.1,localhost,::1"}
	p := newPolicy(func(k string) string { return env[k] })
	env["HTTPS_PROXY"] = "http://changed.invalid"
	for _, host := range []string{"bypass.invalid", "127.0.0.1", "localhost", "[::1]"} {
		u, _ := url.Parse("https://" + host)
		got, err := p.proxy(&http.Request{URL: u})
		if err != nil || got != nil {
			t.Fatalf("bypass %s: %v %v", host, got, err)
		}
	}
	u, _ := url.Parse("https://control.invalid")
	got, err := p.proxy(&http.Request{URL: u})
	if err != nil || got.String() != "http://proxy.invalid:8080" {
		t.Fatalf("snapshot: %v %v", got, err)
	}
	auth, _ := p.authHeader(got)
	if auth != "Basic dXNlcjpzZWNyZXQ=" {
		t.Fatal("authentication lost")
	}
}

func TestMalformedProxyIsSecretSafe(t *testing.T) {
	for _, raw := range []string{"http://user:secret@%", "socks5://user:secret@host", "http://user:secret@host/path"} {
		p := newPolicy(func(k string) string {
			if k == "HTTPS_PROXY" {
				return raw
			}
			return ""
		})
		if p.err == nil || p.err.Error() != "outbound proxy configuration is invalid" {
			t.Fatal("invalid configuration was not safely rejected")
		}
	}
}

func TestNodeProxyClassificationHonoursNoProxy(t *testing.T) {
	env := map[string]string{"HTTPS_PROXY": "http://proxy.invalid:8080", "NO_PROXY": "direct.invalid"}
	p := newPolicy(func(k string) string { return env[k] })
	if !p.proxiesNode(&tailcfg.DERPNode{HostName: "relay.invalid"}) {
		t.Fatal("proxied node classified as direct")
	}
	if p.proxiesNode(&tailcfg.DERPNode{HostName: "direct.invalid", DERPPort: 8443}) {
		t.Fatal("NO_PROXY node classified as proxied")
	}
	unset := newPolicy(func(string) string { return "" })
	if unset.proxiesNode(&tailcfg.DERPNode{HostName: "relay.invalid"}) {
		t.Fatal("node classified as proxied without any proxy configuration")
	}
}
