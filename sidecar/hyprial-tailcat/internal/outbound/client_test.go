package outbound

import (
	"context"
	"errors"
	"net"
	"slices"
	"sync/atomic"
	"testing"
	"time"

	"github.com/tailscale/tailcat"
	"tailscale.com/tailcfg"
	"tailscale.com/types/key"
)

type fakeTransport struct {
	ping  func(context.Context) error
	close func() error
}

func (f *fakeTransport) Ping(ctx context.Context) (tailcat.PingResult, error) {
	return tailcat.PingResult{}, f.ping(ctx)
}
func (f *fakeTransport) Close() error                   { return f.close() }
func (f *fakeTransport) DrainTCP(context.Context) error { return nil }
func (f *fakeTransport) DialTCPPort(context.Context, uint16) (net.Conn, error) {
	return nil, errors.New("port refused")
}

func regionalTestClient() *Client {
	info := tailcat.ConnInfo{ServerPublic: tailcat.NodePublic{NodePublic: key.NewNode().Public()}, Region: []*tailcfg.DERPRegion{
		{RegionID: 2, Nodes: []*tailcfg.DERPNode{{HostName: "b.invalid", RegionID: 2}}},
		{RegionID: 1, Nodes: []*tailcfg.DERPNode{{HostName: "a.invalid", RegionID: 1}}},
	}}
	return NewClient(key.NewNode(), info.Addr(), info, &Diagnostics{})
}

func TestRegionHintAvoidsRepeatedFallback(t *testing.T) {
	info := tailcat.ConnInfo{ServerPublic: tailcat.NodePublic{NodePublic: key.NewNode().Public()}}
	for i, host := range []string{"a.invalid", "b.invalid", "c.invalid", "d.invalid"} {
		id := tailcfg.DERPRegionID(i + 1)
		info.Region = append(info.Region, &tailcfg.DERPRegion{RegionID: id, Nodes: []*tailcfg.DERPNode{{HostName: host, RegionID: id}}})
	}
	var hints RegionHints
	var attempts []string
	for range 3 {
		client := hints.NewClient(key.NewNode(), info.Addr(), info, &Diagnostics{})
		client.makeTransport = func(address tailcat.Addr) transport {
			parsed, err := tailcat.ParseAddr(address)
			if err != nil {
				t.Fatal(err)
			}
			host := parsed.Region[0].Nodes[0].HostName
			return &fakeTransport{ping: func(context.Context) error {
				attempts = append(attempts, host)
				if host != "d.invalid" {
					return errors.New("rejected")
				}
				return nil
			}, close: func() error { return nil }}
		}
		client.current = client.makeTransport(client.addresses[0])
		if _, err := client.Ping(context.Background()); err != nil {
			t.Fatal(err)
		}
		client.Close()
	}
	if want := []string{"a.invalid", "b.invalid", "c.invalid", "d.invalid", "d.invalid", "d.invalid"}; !slices.Equal(attempts, want) {
		t.Fatalf("later exchanges repeated rejected regions: %v", attempts)
	}
}

func TestRegionHintScopeExpiryAndFailure(t *testing.T) {
	info := tailcat.ConnInfo{ServerPublic: tailcat.NodePublic{NodePublic: key.NewNode().Public()}, Region: []*tailcfg.DERPRegion{
		{RegionID: 1, Nodes: []*tailcfg.DERPNode{{HostName: "a.invalid", RegionID: 1}}},
		{RegionID: 2, Nodes: []*tailcfg.DERPNode{{HostName: "b.invalid", RegionID: 2}}},
	}}
	var hints RegionHints
	create := func(candidate tailcat.ConnInfo) *Client {
		client := hints.NewClient(key.NewNode(), candidate.Addr(), candidate, &Diagnostics{})
		t.Cleanup(func() { client.Close() })
		return client
	}
	initial := create(info)
	hints.remember(initial.hintKey, initial.hintEpoch, 2)
	if create(info).regionIDs[0] != 2 {
		t.Fatal("healthy region was not preferred")
	}
	otherAuthority := info
	otherAuthority.ServerPublic = tailcat.NodePublic{NodePublic: key.NewNode().Public()}
	if create(otherAuthority).regionIDs[0] != 1 {
		t.Fatal("hint crossed authority boundary")
	}
	otherMap := info
	otherMap.Region = slices.Clone(info.Region)
	otherMap.Region[1] = &tailcfg.DERPRegion{RegionID: 2, Nodes: []*tailcfg.DERPNode{{HostName: "changed.invalid", RegionID: 2}}}
	if create(otherMap).regionIDs[0] != 1 {
		t.Fatal("hint crossed DERP map boundary")
	}
	hints.entries[initial.hintKey] = regionHint{region: 2, expires: time.Now().Add(-time.Second)}
	if create(info).regionIDs[0] != 1 {
		t.Fatal("expired region hint reused")
	}
	hints.remember(initial.hintKey, initial.hintEpoch, 2)
	failed := create(info)
	failed.makeTransport = func(tailcat.Addr) transport {
		return &fakeTransport{ping: func(context.Context) error { return errors.New("failed") }, close: func() error { return nil }}
	}
	failed.current = failed.makeTransport(failed.addresses[0])
	if _, err := failed.Ping(context.Background()); err == nil || create(info).regionIDs[0] != 1 {
		t.Fatal("failed hint was not cleared")
	}
	hints.remember(initial.hintKey, initial.hintEpoch, 2)
	failed = create(info)
	failed.current = &fakeTransport{ping: func(context.Context) error { return nil }, close: func() error { return nil }}
	if _, err := failed.DialTCPPort(context.Background(), 1); err == nil || create(info).regionIDs[0] != 1 {
		t.Fatal("TCP failure retained hint")
	}
	hints.Clear()
	if _, err := failed.Ping(context.Background()); err != nil || len(hints.entries) != 0 {
		t.Fatal("retired generation repopulated cleared hints")
	}
	for i := range 200 {
		var k [32]byte
		k[0] = byte(i)
		hints.remember(k, hints.epoch, 1)
	}
	if len(hints.entries) != 128 {
		t.Fatal("region hint memory is unbounded")
	}
}

func TestRegionalIdentityAndOrdering(t *testing.T) {
	c := regionalTestClient()
	defer c.Close()
	first := c.current.(*tailcat.Client)
	second := c.makeTransport(c.addresses[1]).(*tailcat.Client)
	defer second.Close()
	if !first.Key.Equal(second.Key) {
		t.Fatal("fallback changed derived client identity")
	}
	for i, address := range c.addresses {
		info, err := tailcat.ParseAddr(address)
		if err != nil || len(info.Region) != 1 || info.Region[0].Nodes[0].HostName != []string{"a.invalid", "b.invalid"}[i] {
			t.Fatalf("regions are not isolated and sorted: index=%d regions=%+v error=%v", i, info.Region, err)
		}
	}
}

func TestRegionalFallbackClosesBeforeIdentityReuse(t *testing.T) {
	c := regionalTestClient()
	defer c.Close()
	var events []string
	refused := errors.New("refused")
	c.current = &fakeTransport{
		ping:  func(context.Context) error { events = append(events, "ping1"); return refused },
		close: func() error { events = append(events, "close1"); return nil },
	}
	c.makeTransport = func(tailcat.Addr) transport {
		events = append(events, "create2")
		return &fakeTransport{
			ping:  func(context.Context) error { events = append(events, "ping2"); return nil },
			close: func() error { return nil },
		}
	}
	if _, err := c.Ping(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(events, []string{"ping1", "close1", "create2", "ping2"}) {
		t.Fatal(events)
	}
}

func TestRegionalEstablishedTransportIsRetained(t *testing.T) {
	c := regionalTestClient()
	defer c.Close()
	var fail bool
	c.current = &fakeTransport{
		ping: func(context.Context) error {
			if fail {
				return errors.New("later failure")
			}
			return nil
		},
		close: func() error { return nil },
	}
	c.makeTransport = func(tailcat.Addr) transport { t.Fatal("replaced registered transport"); return nil }
	if _, err := c.Ping(context.Background()); err != nil {
		t.Fatal(err)
	}
	fail = true
	if _, err := c.Ping(context.Background()); err == nil {
		t.Fatal("lost failure")
	}
	if _, err := c.DialTCPPort(context.Background(), 1); err == nil {
		t.Fatal("lost port failure")
	}
}

func TestRegionalCloseCancelsPingWithoutFallback(t *testing.T) {
	c := regionalTestClient()
	started := make(chan struct{})
	var closed atomic.Int32
	c.current = &fakeTransport{
		ping:  func(ctx context.Context) error { close(started); <-ctx.Done(); return ctx.Err() },
		close: func() error { closed.Add(1); return nil },
	}
	c.makeTransport = func(tailcat.Addr) transport { t.Error("created transport after close"); return nil }
	done := make(chan error, 1)
	go func() { _, err := c.Ping(context.Background()); done <- err }()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("Ping did not start")
	}
	c.Close()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("Close did not stop Ping")
	}
	c.Close()
	if closed.Load() != 1 {
		t.Fatal("duplicate close")
	}
}

func TestRegionalCloseFailurePreventsReplacement(t *testing.T) {
	c := regionalTestClient()
	defer c.Close()
	c.current = &fakeTransport{
		ping:  func(context.Context) error { return errors.New("failed") },
		close: func() error { return errors.New("close failed") },
	}
	c.makeTransport = func(tailcat.Addr) transport { t.Fatal("overlapping client identities"); return nil }
	if _, err := c.Ping(context.Background()); err == nil {
		t.Fatal("lost close failure")
	}
}

func TestRegionalBudgetReachesLaterRegion(t *testing.T) {
	c := regionalTestClient()
	defer c.Close()
	c.addresses = append(c.addresses, c.addresses...)
	c.regionIDs = []tailcfg.DERPRegionID{1, 2, 3, 4}
	attempts := 0
	c.makeTransport = func(tailcat.Addr) transport {
		attempts++
		index := attempts
		return &fakeTransport{
			ping: func(ctx context.Context) error {
				if index == 4 {
					deadline, _ := ctx.Deadline()
					if time.Until(deadline) < 150*time.Millisecond {
						t.Fatal("healthy fourth region has no handshake/control reserve")
					}
					return nil
				}
				<-ctx.Done()
				return ctx.Err()
			},
			close: func() error { return nil },
		}
	}
	c.current = c.makeTransport(c.addresses[0])
	ctx, cancel := context.WithTimeout(context.Background(), 400*time.Millisecond)
	defer cancel()
	if _, err := c.Ping(ctx); err != nil {
		t.Fatalf("healthy fourth region starved after %d attempts: %v", attempts, err)
	}
	if attempts != 4 || ctx.Err() != nil {
		t.Fatalf("attempts=%d context=%v", attempts, ctx.Err())
	}
}

func TestRegionalFastFailureReleasesUnusedBudget(t *testing.T) {
	c := regionalTestClient()
	defer c.Close()
	c.current = &fakeTransport{
		ping:  func(context.Context) error { return errors.New("refused") },
		close: func() error { return nil },
	}
	c.makeTransport = func(tailcat.Addr) transport {
		return &fakeTransport{
			ping: func(ctx context.Context) error {
				deadline, ok := ctx.Deadline()
				if !ok || time.Until(deadline) < 800*time.Millisecond {
					t.Fatal("failed region retained its unused budget")
				}
				return nil
			},
			close: func() error { return nil },
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if _, err := c.Ping(ctx); err != nil {
		t.Fatal(err)
	}
}

func TestRegionalExhaustionRetriesRecoveredFirstRegion(t *testing.T) {
	c := regionalTestClient()
	defer c.Close()
	recovered := false
	c.makeTransport = func(address tailcat.Addr) transport {
		return &fakeTransport{
			ping: func(context.Context) error {
				if recovered && address == c.addresses[0] {
					return nil
				}
				return errors.New("unreachable")
			},
			close: func() error { return nil },
		}
	}
	c.current = c.makeTransport(c.addresses[0])
	if _, err := c.Ping(context.Background()); err == nil {
		t.Fatal("expected exhaustion")
	}
	recovered = true
	if _, err := c.Ping(context.Background()); err != nil {
		t.Fatal("recovered first region was not retried")
	}
}

func TestMultiNodeRegionSplitsIntoNodeCandidates(t *testing.T) {
	info := tailcat.ConnInfo{ServerPublic: tailcat.NodePublic{NodePublic: key.NewNode().Public()}, Region: []*tailcfg.DERPRegion{
		{RegionID: 1, Nodes: []*tailcfg.DERPNode{{Name: "hung", HostName: "hung.invalid", RegionID: 1}, {Name: "good", HostName: "good.invalid", RegionID: 1}}},
	}}
	c := newClient(key.NewNode(), info.Addr(), info, &Diagnostics{}, nil, allProxied)
	var hosts []string
	for _, address := range c.addresses {
		parsed, err := tailcat.ParseAddr(address)
		if err != nil {
			t.Fatal(err)
		}
		if len(parsed.Region) != 1 || len(parsed.Region[0].Nodes) != 1 {
			t.Fatalf("candidate is not a single node: %+v", parsed.Region)
		}
		hosts = append(hosts, parsed.Region[0].Nodes[0].HostName)
	}
	if !slices.Equal(hosts, []string{"hung.invalid", "good.invalid"}) || !slices.Equal(c.regionIDs, []tailcfg.DERPRegionID{1, 1}) {
		t.Fatalf("node candidates %v regions %v", hosts, c.regionIDs)
	}
}

func TestHungFirstNodeLeavesBudgetForHealthySecondNode(t *testing.T) {
	info := tailcat.ConnInfo{ServerPublic: tailcat.NodePublic{NodePublic: key.NewNode().Public()}, Region: []*tailcfg.DERPRegion{
		{RegionID: 1, Nodes: []*tailcfg.DERPNode{{Name: "hung", HostName: "hung.invalid", RegionID: 1}, {Name: "good", HostName: "good.invalid", RegionID: 1}}},
	}}
	c := newClient(key.NewNode(), info.Addr(), info, &Diagnostics{}, nil, allProxied)
	c.makeTransport = func(address tailcat.Addr) transport {
		parsed, err := tailcat.ParseAddr(address)
		if err != nil {
			t.Fatal(err)
		}
		host := parsed.Region[0].Nodes[0].HostName
		return &fakeTransport{close: func() error { return nil }, ping: func(ctx context.Context) error {
			if host == "hung.invalid" {
				<-ctx.Done()
				return ctx.Err()
			}
			return nil
		}}
	}
	c.current = c.makeTransport(c.addresses[0])
	started := time.Now()
	if _, err := c.Ping(context.Background()); err != nil {
		t.Fatalf("healthy node unreachable behind hung node after %s: %v", time.Since(started), err)
	}
	if elapsed := time.Since(started); elapsed > 21*time.Second {
		t.Fatalf("hung node consumed %s", elapsed)
	}
}

func allProxied(*tailcfg.DERPNode) bool  { return true }
func noneProxied(*tailcfg.DERPNode) bool { return false }

func twoNodeRegion(hosts ...string) tailcat.ConnInfo {
	region := &tailcfg.DERPRegion{RegionID: 1}
	for _, host := range hosts {
		region.Nodes = append(region.Nodes, &tailcfg.DERPNode{Name: host, HostName: host, RegionID: 1})
	}
	return tailcat.ConnInfo{ServerPublic: tailcat.NodePublic{NodePublic: key.NewNode().Public()}, Region: []*tailcfg.DERPRegion{region}}
}

func TestDirectMultiNodeRegionKeepsTheOriginalSingleCandidate(t *testing.T) {
	info := twoNodeRegion("hung-a.invalid", "hung-b.invalid")
	address := info.Addr()
	c := newClient(key.NewNode(), address, info, &Diagnostics{}, nil, noneProxied)
	if !slices.Equal(c.addresses, []tailcat.Addr{address}) || !slices.Equal(c.regionIDs, []tailcfg.DERPRegionID{1}) {
		t.Fatalf("direct region was split: %d candidates, regions %v", len(c.addresses), c.regionIDs)
	}
	var made, pings atomic.Int32
	c.makeTransport = func(a tailcat.Addr) transport {
		made.Add(1)
		parsed, err := tailcat.ParseAddr(a)
		if err != nil || len(parsed.Region) != 1 || len(parsed.Region[0].Nodes) != 2 {
			t.Fatalf("direct transport lost its nodes: %v", err)
		}
		return &fakeTransport{close: func() error { return nil }, ping: func(context.Context) error {
			pings.Add(1)
			return errors.New("both nodes down")
		}}
	}
	c.current = c.makeTransport(c.addresses[0])
	if _, err := c.Ping(context.Background()); err == nil {
		t.Fatal("expected the single direct Ping to fail")
	}
	if made.Load() != 1 || pings.Load() != 1 {
		t.Fatalf("direct path changed: %d transports, %d pings", made.Load(), pings.Load())
	}
}

func TestOnlyProxiedNodesBecomeSingleNodeCandidates(t *testing.T) {
	info := twoNodeRegion("proxied.invalid", "direct-a.invalid", "direct-b.invalid")
	proxied := func(n *tailcfg.DERPNode) bool { return n.HostName == "proxied.invalid" }
	c := newClient(key.NewNode(), info.Addr(), info, &Diagnostics{}, nil, proxied)
	var shapes [][]string
	for _, address := range c.addresses {
		parsed, err := tailcat.ParseAddr(address)
		if err != nil {
			t.Fatal(err)
		}
		var hosts []string
		for _, node := range parsed.Region[0].Nodes {
			hosts = append(hosts, node.HostName)
		}
		shapes = append(shapes, hosts)
	}
	want := [][]string{{"proxied.invalid"}, {"direct-a.invalid", "direct-b.invalid"}}
	if len(shapes) != len(want) || !slices.Equal(shapes[0], want[0]) || !slices.Equal(shapes[1], want[1]) {
		t.Fatalf("candidates %v, want %v", shapes, want)
	}
}
