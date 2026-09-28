package node

import (
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tsnet/internal/proto"
	"context"
	"errors"
	"os"
	"path/filepath"
	"tailscale.com/ipn"
	"tailscale.com/ipn/ipnstate"
	"testing"
	"time"
)

func TestPromotionRestartPreservesIdentityAndWaitsForRunning(t *testing.T) {
	first := &fakeBackend{watcher: newFakeWatcher(), status: completeStatus(t)}
	second := &fakeBackend{watcher: newFakeWatcher(), status: completeStatus(t)}
	dir := t.TempDir()
	events := make(chan proto.Event, 16)
	failures := make(chan Failure, 1)
	calls := 0
	manager := NewManager(func(config Config) Backend {
		calls++
		if calls == 1 {
			if err := os.WriteFile(filepath.Join(config.Dir, "identity-fixture"), []byte("same-node"), 0600); err != nil {
				t.Error(err)
			}
			return first
		}
		if first.closeCount() == 0 {
			t.Error("recreated before close")
		}
		if config.Dir != filepath.Join(dir, "node") || config.HasAuthKey || config.AuthKey != "" {
			t.Error("incorrect resume config")
		}
		data, err := os.ReadFile(filepath.Join(config.Dir, "identity-fixture"))
		if err != nil || string(data) != "same-node" {
			t.Error("identity lost")
		}
		return second
	}, func(e proto.Event) error { events <- e; return nil }, func(f Failure) { failures <- f })
	manager.restartForPromotion = true
	if err := manager.Start(proto.UpRequest{Hostname: "short", Dir: dir, Join: "interactive"}); err != nil {
		t.Fatal(err)
	}
	defer manager.Stop()
	first.watcher.items <- notifyState(ipn.Running)
	waitFor(t, func() bool { manager.mu.Lock(); defer manager.mu.Unlock(); return manager.backend == second })
	for len(events) > 0 {
		if (<-events).Event == "ready" {
			t.Fatal("premature ready")
		}
	}
	second.watcher.items <- notifyState(ipn.Running)
	deadline := time.After(3 * time.Second)
	for {
		select {
		case e := <-events:
			if e.Event == "ready" {
				return
			}
		case f := <-failures:
			t.Fatal(f)
		case <-deadline:
			t.Fatal("no resumed ready")
		}
	}
}
func TestPromotionRestartFailureRetainsDurableState(t *testing.T) {
	first := &fakeBackend{watcher: newFakeWatcher(), status: completeStatus(t)}
	second := &fakeBackend{watcher: newFakeWatcher(), startErr: errors.New("fixture failure")}
	calls := 0
	failures := make(chan Failure, 1)
	dir := t.TempDir()
	manager := NewManager(func(Config) Backend {
		calls++
		if calls == 1 {
			return first
		}
		return second
	}, func(proto.Event) error { return nil }, func(f Failure) { failures <- f })
	manager.restartForPromotion = true
	if err := manager.Start(proto.UpRequest{Hostname: "short", Dir: dir, Join: "interactive"}); err != nil {
		t.Fatal(err)
	}
	defer manager.Stop()
	first.watcher.items <- notifyState(ipn.Running)
	select {
	case f := <-failures:
		if f.Code != CodeStateDir {
			t.Fatal(f)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no failure")
	}
	if info, err := os.Stat(filepath.Join(dir, "node")); err != nil || !info.IsDir() {
		t.Fatal("lost enrolled state", err)
	}
}

type delayedStatusBackend struct {
	*fakeBackend
	reads int
}

func (b *delayedStatusBackend) Status(context.Context) (*ipnstate.Status, error) {
	b.reads++
	if b.reads < 3 {
		return &ipnstate.Status{BackendState: ipn.Running.String()}, nil
	}
	return b.status, nil
}
func TestReadyWaitsForCompleteStatusSnapshot(t *testing.T) {
	b := &delayedStatusBackend{fakeBackend: &fakeBackend{status: completeStatus(t)}}
	_, event, err := waitReadyStatus(context.Background(), b, "https://network.test")
	if err != nil || event.Event != "ready" || b.reads < 3 {
		t.Fatalf("ready=%v error=%v reads=%v", event, err, b.reads)
	}
}
func TestReadyStatusWaitHonorsCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	b := &delayedStatusBackend{fakeBackend: &fakeBackend{status: completeStatus(t)}}
	if _, _, err := waitReadyStatus(ctx, b, ""); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if b.reads != 0 {
		t.Fatal("queried cancelled backend")
	}
}
