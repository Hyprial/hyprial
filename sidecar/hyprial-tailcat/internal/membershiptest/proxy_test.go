// This test runs real encrypted Tailcat membership streams through a local
// CONNECT proxy. The advertised region has no usable direct IP or STUN path.
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
)

func TestMembershipOverHTTPProxy(t *testing.T) {
	for _, status := range []int{200, 407} {
		t.Run(fmt.Sprint(status), func(t *testing.T) { membershipProxy(t, status) })
	}
}

func membershipProxy(t *testing.T, status int) {
	relay := labrelay.Start(t)
	d := relay.Config()
	var connections atomic.Int32
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "CONNECT" || r.Host != "relay.invalid:443" {
			t.Errorf("unexpected proxy target: %s %s", r.Method, r.Host)
			w.WriteHeader(403)
			return
		}
		if r.Header.Get("Proxy-Authorization") != "Basic dXNlcjpzZWNyZXQ=" {
			t.Error("missing proxy auth")
			w.WriteHeader(407)
			return
		}
		connections.Add(1)
		if status != 200 {
			w.WriteHeader(status)
			return
		}
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
		conn.SetDeadline(time.Now().Add(25 * time.Second))
		upstream.SetDeadline(time.Now().Add(25 * time.Second))
		io.WriteString(conn, "HTTP/1.1 200 Connection Established\r\n\r\n")
		done := make(chan struct{})
		go func() { io.Copy(upstream, conn); upstream.Close(); close(done) }()
		io.Copy(conn, upstream)
		conn.Close()
		<-done
	}))
	defer proxy.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestMembershipProxyChild$", "-test.v", "-test.timeout=25s")
	cmd.Env = []string{"GOMAXPROCS=2", "HOME=" + t.TempDir(), fmt.Sprintf("HYPRIAL_MEMBERSHIP_PROXY_CHILD=%d", status), "IN_TS_TEST=true", "TS_DISABLE_PORTMAPPER=true", "NO_PROXY=127.0.0.1,localhost,::1", "HTTPS_PROXY=" + strings.Replace(proxy.URL, "http://", "http://user:secret@", 1)}
	output, err := cmd.CombinedOutput()
	if strings.Contains(string(output), "user:secret") {
		t.Fatal("proxy credential leaked")
	}
	if err != nil {
		t.Fatalf("control stream failed: %v\n%s", err, output)
	}
	if connections.Load() < 2 {
		t.Fatalf("only %d proxy connections", connections.Load())
	}
}

func TestMembershipProxyChild(t *testing.T) {
	status := os.Getenv("HYPRIAL_MEMBERSHIP_PROXY_CHILD")
	if status == "" {
		return
	}
	derp := &engine.DERPOverride{Host: "relay.invalid", DERPPort: 443, STUNPort: -1, InsecureForTests: true}
	t.Log("starting authority")
	a := start(t, "proxy-a", derp)
	t.Log("starting member")
	b := start(t, "proxy-b", derp)
	t.Log("creating membership")
	initNode(t, a, a, "proxy-tn", []membership.Member{a.row})
	var created struct {
		Invite membership.InviteV2 `json:"invite"`
	}
	json.Unmarshal(call(t, a, "invite-create", membership.CreateParams{OperationID: "proxy-invite", TailnetID: "proxy-tn", Owner: b.row.Owner, ExpiresAtMs: time.Now().Add(time.Minute).UnixMilli()}), &created)
	started := time.Now()
	_, err := run(b, "invite-redeem", membership.RedeemParams{OperationID: "proxy-redeem", Invite: created.Invite})
	if status == "407" {
		data, message := membership.Diagnostic(err)
		if membership.Code(err) != "AUTHORITY_UNREACHABLE" || !strings.Contains(message, "proxy-rejected") || !strings.Contains(string(data), `"stage":"derp"`) {
			t.Fatalf("missing safe transport cause: %v %s %s", err, data, message)
		}
		if time.Since(started) > 12*time.Second {
			t.Fatal("diagnostic exceeded the upstream Ping deadline plus margin")
		}
		return
	}
	if err != nil {
		data, message := membership.Diagnostic(err)
		t.Fatalf("%v: %s %s", err, message, data)
	}
	roster := fetch(t, b, "proxy-tn")
	if len(roster.Members) != 2 || !b.e.Readiness().Member {
		t.Fatal("redemption/registration did not complete")
	}
}
