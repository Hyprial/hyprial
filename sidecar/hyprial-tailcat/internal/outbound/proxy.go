// Package outbound installs the missing Tailcat DERP proxy hooks. The upstream
// useproxy feature logs credential-bearing URLs; this environment-only policy
// snapshots configuration once and keeps credentials out of returned URLs.
package outbound

import (
	"context"
	"encoding/base64"
	"errors"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"

	"golang.org/x/net/http/httpproxy"
	"tailscale.com/feature"
	"tailscale.com/tailcfg"
)

type policy struct {
	lookup func(*url.URL) (*url.URL, error)
	auth   map[string]string
	err    error
}

func newPolicy(getenv func(string) string) *policy {
	first := func(names ...string) string {
		for _, name := range names {
			if value := getenv(name); value != "" {
				return value
			}
		}
		return ""
	}
	p := &policy{auth: map[string]string{}}
	clean := func(raw string) string {
		if raw == "" {
			return ""
		}
		if !strings.Contains(raw, "://") {
			raw = "http://" + raw
		}
		u, err := url.Parse(raw)
		if err != nil || u.Hostname() == "" || (u.Scheme != "http" && u.Scheme != "https") || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") {
			p.err = errors.New("outbound proxy configuration is invalid")
			return ""
		}
		auth := ""
		if u.User != nil {
			password, _ := u.User.Password()
			auth = "Basic " + base64.StdEncoding.EncodeToString([]byte(u.User.Username()+":"+password))
		}
		u.User = nil
		u.Path = ""
		if prior, ok := p.auth[u.String()]; ok && prior != auth {
			p.err = errors.New("outbound proxy authentication is inconsistent")
		}
		p.auth[u.String()] = auth
		return u.String()
	}
	httpProxy := first("HTTP_PROXY", "http_proxy")
	httpsProxy := first("HTTPS_PROXY", "https_proxy")
	if httpsProxy == "" {
		httpsProxy = httpProxy
	}
	cfg := &httpproxy.Config{HTTPProxy: clean(httpProxy), HTTPSProxy: clean(httpsProxy), NoProxy: first("NO_PROXY", "no_proxy")}
	p.lookup = cfg.ProxyFunc()
	return p
}

var environment = newPolicy(os.Getenv)

func init() {
	feature.HookProxyFromEnvironment.Set(environment.proxy)
	feature.HookProxyGetAuthHeader.Set(environment.authHeader)
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = environment.proxy
	transport.GetProxyConnectHeader = func(_ context.Context, proxy *url.URL, _ string) (http.Header, error) {
		header := make(http.Header)
		if auth, _ := environment.authHeader(proxy); auth != "" {
			header.Set("Proxy-Authorization", auth)
		}
		return header, nil
	}
	http.DefaultClient.Transport = transport
}

func (p *policy) proxy(r *http.Request) (*url.URL, error) {
	if p.err != nil {
		return nil, p.err
	}
	return p.lookup(r.URL)
}
func (p *policy) authHeader(u *url.URL) (string, error) { return p.auth[u.String()], nil }

func (p *policy) proxiesNode(node *tailcfg.DERPNode) bool {
	if p.err != nil || node == nil || node.HostName == "" {
		return false
	}
	port := node.DERPPort
	if port == 0 {
		port = 443
	}
	proxy, err := p.lookup(&url.URL{Scheme: "https", Host: net.JoinHostPort(node.HostName, strconv.Itoa(port))})
	return err == nil && proxy != nil
}
func Validate() error { return environment.err }
