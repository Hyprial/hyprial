// Missing upstream node logs are not evidence of node failure. Only after a
// control failure, bounded TLS probes can supply explicitly labelled diagnoses.
// Probes never register a DERP identity and never control transport lifetime.
package outbound

import (
	"bufio"
	"context"
	"crypto/tls"
	"errors"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"

	"tailscale.com/net/tlsdial"
	"tailscale.com/tailcfg"
)

func (d *Detail) ProbeMissing(ctx context.Context, regions []*tailcfg.DERPRegion) {
	if _, bounded := ctx.Deadline(); !bounded {
		return
	}
	known := make(map[string]bool)
	for _, a := range d.Attempts {
		known[a.Node] = true
	}
	var mu sync.Mutex
	var wg sync.WaitGroup
	gate := make(chan struct{}, 4)
	for _, region := range regions {
		for _, node := range region.Nodes {
			port := node.DERPPort
			if port == 0 {
				port = 443
			}
			target := net.JoinHostPort(node.HostName, strconv.Itoa(port))
			if node.STUNOnly || known[target] || !validNode(target) || ctx.Err() != nil {
				continue
			}
			known[target] = true
			select {
			case gate <- struct{}{}:
			case <-ctx.Done():
				continue
			}
			wg.Add(1)
			go func() {
				defer wg.Done()
				defer func() { <-gate }()
				cause := probeNode(ctx, region, node)
				if cause == "" {
					return
				}
				mu.Lock()
				d.Attempts = append(d.Attempts, Attempt{Node: target, Cause: cause, Source: "diagnostic-probe"})
				mu.Unlock()
			}()
		}
	}
	wg.Wait()
	sort.Slice(d.Attempts, func(i, j int) bool { return d.Attempts[i].Node < d.Attempts[j].Node })
}

var errProxyRejected = errors.New("proxy-rejected")

func probeNode(ctx context.Context, _ *tailcfg.DERPRegion, node *tailcfg.DERPNode) string {
	err := probeTLS(ctx, node)
	if err == nil {
		return ""
	}
	if ctx.Err() != nil {
		return ControlFailure(ctx.Err()).Cause
	}
	if errors.Is(err, errProxyRejected) {
		return "proxy-rejected"
	}
	if cause := classify(strings.ToLower(err.Error())); cause != "" {
		return cause
	}
	return ControlFailure(err).Cause
}

func probeTLS(ctx context.Context, node *tailcfg.DERPNode) error {
	port := node.DERPPort
	if port == 0 {
		port = 443
	}
	target := net.JoinHostPort(node.HostName, strconv.Itoa(port))
	proxy, err := environment.proxy(&http.Request{URL: &url.URL{Scheme: "https", Host: target}})
	if err != nil {
		return err
	}
	destination := target
	if proxy != nil {
		proxyPort := proxy.Port()
		if proxyPort == "" {
			proxyPort = "80"
			if proxy.Scheme == "https" {
				proxyPort = "443"
			}
		}
		destination = net.JoinHostPort(proxy.Hostname(), proxyPort)
	}
	var dialer net.Dialer
	raw, err := dialer.DialContext(ctx, "tcp", destination)
	if err != nil {
		return err
	}
	defer raw.Close()
	stop := context.AfterFunc(ctx, func() { raw.Close() })
	defer stop()
	if deadline, ok := ctx.Deadline(); ok {
		raw.SetDeadline(deadline)
	}
	conn := raw
	if proxy != nil {
		if proxy.Scheme == "https" {
			secure := tls.Client(conn, &tls.Config{ServerName: proxy.Hostname()})
			if err := secure.HandshakeContext(ctx); err != nil {
				return err
			}
			conn = secure
		}
		header := make(http.Header)
		if auth, _ := environment.authHeader(proxy); auth != "" {
			header.Set("Proxy-Authorization", auth)
		}
		request := &http.Request{Method: "CONNECT", URL: &url.URL{Opaque: target}, Host: target, Header: header}
		if err := request.Write(conn); err != nil {
			return err
		}
		response, err := http.ReadResponse(bufio.NewReader(conn), request)
		if err != nil {
			return err
		}
		if response.StatusCode != 200 {
			return errProxyRejected
		}
	}
	config := tlsdial.Config(nil, nil)
	config.ServerName = node.HostName
	if node.InsecureForTests {
		config.InsecureSkipVerify = true
		config.VerifyConnection = nil
	}
	if node.CertName != "" {
		if hash, ok := strings.CutPrefix(node.CertName, "sha256-raw:"); ok {
			tlsdial.SetConfigExpectedCertHash(config, hash)
		} else {
			tlsdial.SetConfigExpectedCert(config, node.CertName)
		}
	}
	return tls.Client(conn, config).HandshakeContext(ctx)
}
