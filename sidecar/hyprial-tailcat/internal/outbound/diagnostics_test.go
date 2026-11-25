package outbound

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"tailscale.com/tailcfg"
)

func TestDiagnosticByteCountContext(t *testing.T) {
	for _, stage := range []string{"control", "derp", "sidecar"} {
		for _, cause := range causes {
			received := 33
			detail := Detail{Stage: stage, Cause: cause, Proxy: "configured", BytesReceived: &received}
			want := stage == "control" && (cause == "deadline-exceeded" || cause == "transport-failed")
			if detail.Valid() != want {
				t.Fatalf("byte count context accepted incorrectly: stage=%s cause=%s", stage, cause)
			}
		}
	}
}

func TestDiagnosticObservationsAreRequestScoped(t *testing.T) {
	d := &Diagnostics{}
	d.Logf("derphttp: CONNECT dial to stale.invalid:443: 407")
	o := d.Begin()
	defer o.Close()
	if got := o.Failure(context.DeadlineExceeded); got.Stage != "control" || len(got.Attempts) != 0 {
		t.Fatal("old failure crossed request boundary")
	}
	d.Logf("derphttp: CONNECT dial to relay.invalid:443: 407")
	other := d.Begin()
	defer other.Close()
	if other.Failure(context.DeadlineExceeded).Stage != "control" {
		t.Fatal("concurrent observer inherited prior failure")
	}
	o.Close()
	d.Logf("derphttp: CONNECT dial to relay.invalid:443: 200 OK")
	if o.Failure(context.DeadlineExceeded).Stage != "derp" {
		t.Fatal("closed observer changed")
	}
	if other.Failure(context.DeadlineExceeded).Stage != "control" {
		t.Fatal("successful CONNECT misclassified")
	}
}

func TestDiagnosticParsesStatusInsteadOfHostname(t *testing.T) {
	for _, status := range []string{"407", "401", "500"} {
		d := &Diagnostics{}
		o := d.Begin()
		d.Logf("derphttp: CONNECT dial to relay200.invalid:443: %s", status)
		if o.Failure(context.Canceled).Cause != "proxy-rejected" {
			t.Fatal("status confused with hostname")
		}
		o.Close()
	}
}

func TestDiagnosticPerNodeClassificationAndSafety(t *testing.T) {
	for _, tc := range []struct{ text, cause string }{
		{"407 fake-proxy-secret", "proxy-rejected"},
		{"lookup fake-proxy-secret: no such host", "dns-failed"},
		{"tls: failed to verify certificate fake-proxy-secret", "tls-failed"},
		{"i/o timeout fake-proxy-secret", "deadline-exceeded"},
		{"connection refused fake-proxy-secret", "connection-refused"},
		{"fake-proxy-secret", "transport-failed"},
	} {
		d := &Diagnostics{}
		o := d.Begin()
		d.Logf("derphttp: CONNECT dial to b.invalid:443: %s", tc.text)
		d.Logf("derphttp: CONNECT dial to a.invalid:443: %s", tc.text)
		detail := o.Failure(context.DeadlineExceeded)
		if !detail.Valid() || detail.Stage != "derp" || detail.Cause != tc.cause || len(detail.Attempts) != 2 || strings.Contains(detail.Message(), "fake-proxy-secret") {
			t.Fatalf("unsafe or absent classification: %+v", detail)
		}
		if detail.Attempts[0].Node != "a.invalid:443" || detail.Attempts[1].Cause != tc.cause {
			t.Fatal("unstable or missing attempts")
		}
		d.Logf("derphttp: CONNECT dial to a.invalid:443: 200 OK")
		detail = o.Failure(context.DeadlineExceeded)
		if detail.Stage != "control" || len(detail.Attempts) != 1 || detail.Attempts[0].Node != "b.invalid:443" {
			t.Fatal("success failed to clear only its own node")
		}
		o.Close()
	}
}

func TestDiagnosticRejectsCredentialBearingNode(t *testing.T) {
	d := &Diagnostics{}
	o := d.Begin()
	defer o.Close()
	d.Logf("derphttp: CONNECT dial to http://user:fake-proxy-secret@relay.invalid:443: 407")
	if detail := o.Failure(context.DeadlineExceeded); !detail.Valid() || len(detail.Attempts) != 0 || strings.Contains(detail.Message(), "fake-proxy-secret") {
		t.Fatal("unsafe node escaped")
	}
}

func TestDiagnosticProbeHasExplicitSourceAndSafeTLSCause(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer server.Close()
	address := server.Listener.Addr().(*net.TCPAddr)
	region := &tailcfg.DERPRegion{RegionID: 1, Nodes: []*tailcfg.DERPNode{{
		HostName: "127.0.0.1", DERPPort: address.Port,
	}}}
	detail := ControlFailure(context.DeadlineExceeded)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	detail.ProbeMissing(ctx, []*tailcfg.DERPRegion{region})
	if !detail.Valid() || len(detail.Attempts) != 1 || detail.Attempts[0].Cause != "tls-failed" || detail.Attempts[0].Source != "diagnostic-probe" {
		t.Fatalf("missing safe probe result: %+v", detail)
	}
	if !strings.Contains(detail.Message(), "diagnostic probe") {
		t.Fatal("probe indistinguishable from transport observation")
	}
}

func TestDiagnosticProbeRequiresBudgetAndSkipsKnownNodes(t *testing.T) {
	detail := ControlFailure(context.DeadlineExceeded)
	region := &tailcfg.DERPRegion{RegionID: 1, Nodes: []*tailcfg.DERPNode{{
		HostName: "unresolvable.invalid", DERPPort: 443,
	}}}
	detail.ProbeMissing(context.Background(), []*tailcfg.DERPRegion{region})
	if len(detail.Attempts) != 0 {
		t.Fatal("unbounded probe started")
	}
	detail.Attempts = []Attempt{{Node: "unresolvable.invalid:443", Cause: "proxy-rejected"}}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	detail.ProbeMissing(ctx, []*tailcfg.DERPRegion{region})
	if len(detail.Attempts) != 1 || detail.Attempts[0].Source != "" {
		t.Fatal("known result was reprobed")
	}
}
