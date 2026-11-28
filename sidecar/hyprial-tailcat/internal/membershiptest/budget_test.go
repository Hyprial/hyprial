// Invite redemption performs two separately bounded exchanges. Measure each
// exchange through the real membership Dial seam, not the sum of both legs.
package membershiptest

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/keys"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/membership"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/outbound"
	"github.com/tailscale/tailcat"
	"tailscale.com/types/key"
)

func TestReviewProxyBudget(t *testing.T) {
	proxyFallback(t, "fourth-region-budget")
}

type budgetConn struct {
	net.Conn
	once   sync.Once
	finish func()
}

func (c *budgetConn) Close() error {
	err := c.Conn.Close()
	c.once.Do(c.finish)
	return err
}

func redeemWithExchangeMargin(t *testing.T, b *node, invite membership.InviteV2) {
	t.Helper()
	material, err := keys.Load(b.cfg.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	derive := func(server string) (key.NodePrivate, error) {
		var pub key.NodePublic
		if err := pub.UnmarshalText([]byte(server)); err != nil {
			return key.NodePrivate{}, err
		}
		return keys.DeriveClientPrivate(material.ClientPrivate, pub)
	}
	exchanges := 0
	var hints outbound.RegionHints
	dial := func(ctx context.Context, authority membership.Authority) (net.Conn, error) {
		started := time.Now()
		deadline, ok := ctx.Deadline()
		if !ok || time.Until(deadline) > 30*time.Second {
			t.Fatal("exchange has no thirty-second bound")
		}
		info, err := tailcat.ParseAddr(tailcat.Addr(authority.Address))
		if err != nil {
			return nil, err
		}
		k, err := derive(authority.ServerPublic)
		if err != nil {
			return nil, err
		}
		client := hints.NewClient(k, tailcat.Addr(authority.Address), info, &outbound.Diagnostics{})
		if _, err := client.Ping(ctx); err != nil {
			client.Close()
			return nil, err
		}
		conn, err := client.DialTCPPort(ctx, uint16(authority.ControlPort))
		if err != nil {
			client.Close()
			return nil, err
		}
		return &budgetConn{Conn: conn, finish: func() {
			elapsed, margin := time.Since(started), time.Until(deadline)
			client.Close()
			exchanges++
			fmt.Printf("exchange elapsed=%s margin=%s leg=%d\n", elapsed, margin, exchanges)
			if margin < 5*time.Second {
				t.Errorf("control exchange lacks five-second headroom: elapsed=%s margin=%s", elapsed, margin)
			}
		}}, nil
	}
	self := membership.Sender{Owner: b.row.Owner, DeviceID: b.row.DeviceID, MasterPublic: b.row.MasterPublic, ServerPublic: b.row.ServerPublic, Address: b.row.Address}
	manager, err := membership.Open(filepath.Join(t.TempDir(), "private", "membership.json"), self, func(server string) (string, error) {
		k, err := derive(server)
		if err != nil {
			return "", err
		}
		return k.Public().String(), nil
	}, dial)
	if err != nil {
		t.Fatal(err)
	}
	defer manager.Close()
	params, _ := json.Marshal(membership.RedeemParams{OperationID: "budget-redeem", Invite: invite})
	if _, err := manager.Execute(context.Background(), membership.Request{V: 7, Op: "invite-redeem", RequestID: "budget", Params: params}); err != nil {
		t.Fatal(err)
	}
	if exchanges != 2 {
		t.Fatalf("expected redemption and roster exchanges, got %d", exchanges)
	}
}
