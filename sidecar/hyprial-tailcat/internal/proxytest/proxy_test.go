// The pinned DERP region client has no custom-dialer seam. Both IP families are
// explicitly disabled in its map: a missing proxy hook cannot reach any network.
// HTTPS map fetches additionally use a dialer that refuses every non-proxy dial.
package proxytest

import (
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	_ "code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/engine"
	"github.com/tailscale/tailcat"
	"tailscale.com/derp/derphttp"
	"tailscale.com/derp/derpserver"
	"tailscale.com/net/netmon"
	"tailscale.com/tailcfg"
	"tailscale.com/types/key"
	"tailscale.com/util/eventbus"
)

func TestProxyEnvironment(t *testing.T) {
	for _, variable := range []string{"HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"} {
		for _, status := range []int{200, 407} {
			t.Run(fmt.Sprintf("%s/%d", variable, status), func(t *testing.T) {
				requests := make(chan string, 8)
				relay := derpserver.New(key.NewNode(), func(string, ...any) {})
				defer relay.Close()
				relayHandler := derpserver.Handler(relay)
				upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.URL.Path == "/derp" {
						relayHandler.ServeHTTP(w, r)
						return
					}
					io.WriteString(w, `{"Regions":{"1":{"RegionID":1,"Nodes":[{"Name":"test","RegionID":1,"HostName":"relay.invalid","IPv4":"none","IPv6":"none"}]}}}`)
				}))
				defer upstream.Close()
				proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					requests <- r.Method + " " + r.Host
					if r.Header.Get("Proxy-Authorization") != "Basic ZmFrZS11c2VyOmZha2UtcHJveHktc2VjcmV0" {
						t.Error("missing proxy authentication")
					}
					if status != 200 {
						w.WriteHeader(status)
						return
					}
					if r.Method != "CONNECT" || (r.Host != "relay.invalid:443" && r.Host != "control.invalid:443") {
						w.WriteHeader(403)
						return
					}
					remote, err := net.DialTimeout("tcp", strings.TrimPrefix(upstream.URL, "https://"), time.Second)
					if err != nil {
						t.Error(err)
						return
					}
					defer remote.Close()
					conn, _, err := w.(http.Hijacker).Hijack()
					if err != nil {
						t.Error(err)
						return
					}
					defer conn.Close()
					conn.SetDeadline(time.Now().Add(8 * time.Second))
					remote.SetDeadline(time.Now().Add(8 * time.Second))
					io.WriteString(conn, "HTTP/1.1 200 Connection Established\r\n\r\n")
					done := make(chan struct{})
					go func() { io.Copy(remote, conn); remote.Close(); close(done) }()
					io.Copy(conn, remote)
					conn.Close()
					<-done
				}))
				defer proxy.Close()
				ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer cancel()
				cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestProxyChild$", "-test.v")
				cmd.Env = []string{"HOME=" + t.TempDir(), fmt.Sprintf("HYPRIAL_PROXY_TEST_CHILD=%d", status), "NO_PROXY=127.0.0.1,localhost,::1", variable + "=" + strings.Replace(proxy.URL, "http://", "http://fake-user:fake-proxy-secret@", 1), "TEST_PROXY=" + proxy.URL}
				out, err := cmd.CombinedOutput()
				if strings.Contains(string(out), "fake-user") || strings.Contains(string(out), "fake-proxy-secret") {
					t.Fatal("proxy credential leaked")
				}
				if err != nil {
					t.Fatalf("child failed: %v\n%s", err, out)
				}
				for _, want := range []string{"CONNECT control.invalid:443", "CONNECT relay.invalid:443"} {
					select {
					case got := <-requests:
						if got != want {
							t.Fatalf("proxy request = %q; want %q", got, want)
						}
					default:
						t.Fatalf("missing request %s", want)
					}
				}
			})
		}
	}
}

func TestProxyChild(t *testing.T) {
	status := os.Getenv("HYPRIAL_PROXY_TEST_CHILD")
	if status == "" {
		return
	}
	proxy, _ := url.Parse(os.Getenv("TEST_PROXY"))
	transport := http.DefaultClient.Transport.(*http.Transport)
	transport.TLSClientConfig = &tls.Config{InsecureSkipVerify: true}
	transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		if address != proxy.Host {
			return nil, fmt.Errorf("direct dial refused")
		}
		return (&net.Dialer{}).DialContext(ctx, network, address)
	}
	defer transport.CloseIdleConnections()
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	_, err := tailcat.FetchDERPMap(ctx, tailcat.DERPMapURL("https://control.invalid/derpmap.json"))
	if status == "200" && err != nil {
		t.Fatal(err)
	}
	if status == "407" && err == nil {
		t.Fatal("map fetch unexpectedly succeeded")
	}
	fmt.Println(err)
	monitor, err := netmon.New(eventbus.New(), func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	defer monitor.Close()
	region := &tailcfg.DERPRegion{RegionID: 1, Nodes: []*tailcfg.DERPNode{{Name: "test", RegionID: 1, HostName: "relay.invalid", IPv4: "none", IPv6: "none", InsecureForTests: true}}}
	client := derphttp.NewRegionClient(key.NewNode(), func(f string, a ...any) { fmt.Printf(f+"\n", a...) }, monitor, func() *tailcfg.DERPRegion { return region })
	defer client.Close()
	err = client.Connect(ctx)
	if status == "200" && err != nil {
		t.Fatal(err)
	}
	if status == "407" && (err == nil || !strings.Contains(err.Error(), "407")) {
		t.Fatalf("wanted proxy rejection, got %v", err)
	}
	fmt.Println(err)
}
