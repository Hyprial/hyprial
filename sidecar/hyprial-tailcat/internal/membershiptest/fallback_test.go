package membershiptest

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/engine"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/labrelay"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/membership"
	"github.com/tailscale/tailcat"
	"tailscale.com/tailcfg"
)

func TestReviewProxyFallback(t *testing.T) {
	for _, scenario := range []string{"mixed-node", "mixed-region", "all-blocked", "hung-node", "mixed-node-hung", "fourth-region"} {
		t.Run(scenario, func(t *testing.T) { proxyFallback(t, scenario) })
	}
}

func proxyFallback(t *testing.T, scenario string) {
	relay := labrelay.Start(t)
	d := relay.Config()
	var rejected, accepted atomic.Int32
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" && r.URL.Path == "/rejections" {
			fmt.Fprintln(w, rejected.Load())
			return
		}
		if r.Method != "CONNECT" || r.Header.Get("Proxy-Authorization") != "Basic dXNlcjpzZWNyZXQ=" {
			t.Error("missing CONNECT authentication")
			w.WriteHeader(403)
			return
		}
		if r.Host == "hung.invalid:443" {
			rejected.Add(1)
			conn, _, err := w.(http.Hijacker).Hijack()
			if err != nil {
				t.Error(err)
				return
			}
			defer conn.Close()
			conn.SetDeadline(time.Now().Add(90 * time.Second))
			io.Copy(io.Discard, conn)
			return
		}
		if r.Host == "blocked.invalid:443" || r.Host == "also-blocked.invalid:443" || r.Host == "third-blocked.invalid:443" {
			rejected.Add(1)
			w.WriteHeader(403)
			return
		}
		if r.Host != "relay.invalid:443" {
			w.WriteHeader(403)
			return
		}
		accepted.Add(1)
		upstream, err := net.DialTimeout("tcp", net.JoinHostPort(d.Host, fmt.Sprint(d.DERPPort)), time.Second)
		if err != nil {
			t.Error(err)
			return
		}
		defer upstream.Close()
		conn, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		defer conn.Close()
		conn.SetDeadline(time.Now().Add(90 * time.Second))
		upstream.SetDeadline(time.Now().Add(90 * time.Second))
		io.WriteString(conn, "HTTP/1.1 200 Connection Established\r\n\r\n")
		done := make(chan struct{})
		go func() { io.Copy(upstream, conn); upstream.Close(); close(done) }()
		io.Copy(conn, upstream)
		conn.Close()
		<-done
	}))
	defer proxy.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 95*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestReviewProxyFallbackChild$", "-test.v", "-test.timeout=90s")
	cmd.Env = []string{"HYPRIAL_REVIEW_COUNTER_URL=" + proxy.URL, "GOMAXPROCS=2", "HOME=" + t.TempDir(), "HYPRIAL_REVIEW_FALLBACK=" + scenario, "IN_TS_TEST=true", "TS_DISABLE_PORTMAPPER=true", "NO_PROXY=127.0.0.1,localhost,::1", "HTTPS_PROXY=" + strings.Replace(proxy.URL, "http://", "http://user:secret@", 1)}
	output, err := cmd.CombinedOutput()
	if strings.Contains(string(output), "user:secret") {
		t.Fatal("proxy credential leaked")
	}
	t.Logf("proxy accepted=%d rejected=%d", accepted.Load(), rejected.Load())
	if err != nil {
		t.Fatalf("healthy fallback failed: %v\n%s", err, output)
	}
	for _, line := range strings.Split(string(output), "\n") {
		if strings.HasPrefix(line, "redemption elapsed=") || strings.HasPrefix(line, "exchange elapsed=") || strings.HasPrefix(line, "exchange hint=") {
			t.Log(line)
		}
	}
	if accepted.Load() < 2 || rejected.Load() == 0 {
		t.Fatal("fallback not exercised")
	}
}

func TestReviewProxyFallbackChild(t *testing.T) {
	scenario := os.Getenv("HYPRIAL_REVIEW_FALLBACK")
	if scenario == "" {
		return
	}
	derp := &engine.DERPOverride{Host: "relay.invalid", DERPPort: 443, STUNPort: -1, InsecureForTests: true}
	a := start(t, "fallback-a", derp)
	b := start(t, "fallback-b", derp)
	info, err := tailcat.ParseAddr(tailcat.Addr(a.row.Address))
	if err != nil {
		t.Fatal(err)
	}
	region := info.Region[0]
	good := region.Nodes[0]
	bad := *good
	bad.Name = "blocked"
	bad.HostName = "blocked.invalid"
	region.Nodes = []*tailcfg.DERPNode{&bad, good}
	if scenario == "mixed-node-hung" {
		bad.HostName = "hung.invalid"
		bad.Name = "hung"
	}
	if scenario != "mixed-node" && scenario != "mixed-node-hung" {
		second := *region
		second.RegionID++
		second.RegionCode = "fallback-second"
		other := *good
		other.RegionID = second.RegionID
		second.Nodes = []*tailcfg.DERPNode{&other}
		region.Nodes = []*tailcfg.DERPNode{&bad}
		if scenario == "all-blocked" {
			other.HostName = "also-blocked.invalid"
			other.Name = "also-blocked"
		}
		info.Region = []*tailcfg.DERPRegion{region, &second}
		if scenario == "fourth-region" || scenario == "fourth-region-budget" || scenario == "region-hint" {
			info.Region = []*tailcfg.DERPRegion{region}
			for i, host := range []string{"also-blocked.invalid", "third-blocked.invalid", "relay.invalid"} {
				r := second
				r.RegionID += tailcfg.DERPRegionID(i)
				n := other
				n.RegionID = r.RegionID
				n.HostName = host
				n.Name = host
				r.Nodes = []*tailcfg.DERPNode{&n}
				info.Region = append(info.Region, &r)
			}
		}
		if scenario == "hung-node" {
			other.HostName = "hung.invalid"
			other.Name = "hung"
			region.Nodes = []*tailcfg.DERPNode{&bad, &other}
			info.Region = []*tailcfg.DERPRegion{region}
		}
	}
	a.row.Address = string(info.Addr())
	if scenario == "region-hint" {
		threeHintedExchanges(t, a, b)
		return
	}
	initNode(t, a, a, "fallback-tn", []membership.Member{a.row})
	var created struct {
		Invite membership.InviteV2 `json:"invite"`
	}
	json.Unmarshal(call(t, a, "invite-create", membership.CreateParams{OperationID: "fallback-invite", TailnetID: "fallback-tn", Owner: b.row.Owner, ExpiresAtMs: time.Now().Add(time.Minute).UnixMilli()}), &created)
	if scenario == "fourth-region-budget" {
		redeemWithExchangeMargin(t, b, created.Invite)
		return
	}
	started := time.Now()
	_, err = run(b, "invite-redeem", membership.RedeemParams{OperationID: "fallback-redeem", Invite: created.Invite})
	if scenario == "all-blocked" || scenario == "hung-node" {
		data, message := membership.Diagnostic(err)
		if membership.Code(err) != "AUTHORITY_UNREACHABLE" {
			t.Fatalf("expected exhausted transport, got %v", err)
		}
		var detail struct {
			Attempts []struct{ Node, Cause string } `json:"attempts"`
		}
		if err := json.Unmarshal(data, &detail); err != nil {
			t.Fatal(err)
		}
		failed := map[string]string{}
		for _, attempt := range detail.Attempts {
			failed[attempt.Node] = attempt.Cause
		}
		expected := map[string]string{"blocked.invalid:443": "proxy-rejected", "also-blocked.invalid:443": "proxy-rejected"}
		if scenario == "hung-node" {
			delete(expected, "also-blocked.invalid:443")
			expected["hung.invalid:443"] = "deadline-exceeded"
			if elapsed := time.Since(started); elapsed < 29*time.Second || elapsed > 32*time.Second {
				t.Fatalf("hung node did not exhaust the request budget: %s", elapsed)
			}
		}
		for host, cause := range expected {
			if failed[host] != cause {
				t.Fatalf("missing per-node rejection: %s %s", data, message)
			}
		}
		return
	}
	if err != nil {
		data, message := membership.Diagnostic(err)
		t.Fatalf("redeem after %s: %v %s %s", time.Since(started), err, data, message)
	}
	elapsed := time.Since(started)
	fmt.Printf("redemption elapsed=%s scenario=%s\n", elapsed, scenario)
	if len(fetch(t, b, "fallback-tn").Members) != 2 {
		t.Fatal("missing membership")
	}
}
