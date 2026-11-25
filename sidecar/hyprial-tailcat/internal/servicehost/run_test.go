package servicehost

import (
	"bytes"
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/engine"
)

type fakeEngine struct {
	up        bool
	allow     [][]string
	exposed   []engine.Exposure
	unexposed []int
	downed    bool
	allowErr  error
	exposeErr error
}

func (f *fakeEngine) Up(_ context.Context, cfg engine.UpConfig) (string, string, error) {
	f.up = true
	f.allow = append(f.allow, append([]string(nil), cfg.Allow...))
	return "server", "client", nil
}

func (f *fakeEngine) Allow(keys []string, allowAny bool) (int, error) {
	if f.allowErr != nil {
		return 0, f.allowErr
	}
	if allowAny {
		return 0, errors.New("allowAny must be false")
	}
	f.allow = append(f.allow, append([]string(nil), keys...))
	return len(keys), nil
}

func (f *fakeEngine) Expose(_ context.Context, exposure engine.Exposure) (engine.Exposure, error) {
	if f.exposeErr != nil {
		return engine.Exposure{}, f.exposeErr
	}
	f.exposed = append(f.exposed, exposure)
	return exposure, nil
}

func (f *fakeEngine) Unexpose(port int) error {
	f.unexposed = append(f.unexposed, port)
	return nil
}

func (f *fakeEngine) Down() { f.downed = true }

func TestApplyConfigUsesClosedAdmissionAndExposures(t *testing.T) {
	fake := &fakeEngine{}
	cfg, err := LoadConfig(writeConfig(t, validConfigJSON()))
	if err != nil {
		t.Fatal(err)
	}
	if err := applyConfig(context.Background(), fake, cfg, nil); err != nil {
		t.Fatalf("applyConfig: %v", err)
	}
	if len(fake.allow) != 1 || len(fake.allow[0]) != 0 || len(fake.exposed) != 1 {
		t.Fatalf("fake lifecycle = %+v", fake)
	}
}

func TestApplyConfigFailureRollsBackNewExposure(t *testing.T) {
	fake := &fakeEngine{exposeErr: errors.New("synthetic failure")}
	cfg, err := LoadConfig(writeConfig(t, validConfigJSON()))
	if err != nil {
		t.Fatal(err)
	}
	if err := applyConfig(context.Background(), fake, cfg, nil); err == nil {
		t.Fatal("applyConfig accepted failed exposure")
	}
	if !fake.downed {
		t.Fatal("failed initial apply did not close the engine")
	}
}

func TestApplyConfigAdmissionFailureLeavesOldExposureUntouched(t *testing.T) {
	fake := &fakeEngine{}
	old, err := LoadConfig(writeConfig(t, validConfigJSON()))
	if err != nil {
		t.Fatal(err)
	}
	if err := applyConfig(context.Background(), fake, old, nil); err != nil {
		t.Fatal(err)
	}
	fake.allowErr = errors.New("synthetic admission failure")
	next := old
	next.Exposures = []Exposure{{Port: 18081, Target: "tcp:127.0.0.1:8081"}}
	if err := applyConfig(context.Background(), fake, next, &old); err == nil {
		t.Fatal("reload accepted failed admission")
	}
	if len(fake.exposed) != 1 || len(fake.unexposed) != 0 {
		t.Fatalf("admission failure changed exposures: %+v", fake)
	}
}

func TestReloadAppliesRevocationBeforeIndependentExposureFailure(t *testing.T) {
	fake := &fakeEngine{}
	old, err := LoadConfig(writeConfig(t, validConfigJSON()))
	if err != nil {
		t.Fatal(err)
	}
	old.ClientPublic = []string{"nodekey:" + strings.Repeat("1", 64)}
	if err := applyConfig(context.Background(), fake, old, nil); err != nil {
		t.Fatal(err)
	}
	fake.exposeErr = errors.New("synthetic exposure failure")
	next := old
	next.ClientPublic = nil
	next.Exposures = []Exposure{{Port: 18081, Target: "tcp:127.0.0.1:8081"}}

	if err := applyConfig(context.Background(), fake, next, &old); err == nil {
		t.Fatal("reload accepted failed exposure")
	}
	if len(fake.allow) != 2 || len(fake.allow[1]) != 0 {
		t.Fatalf("revocation did not apply before exposure failure: %+v", fake.allow)
	}
}

func TestApplyConfigRejectsKeyRotationDuringReload(t *testing.T) {
	fake := &fakeEngine{}
	old, err := LoadConfig(writeConfig(t, validConfigJSON()))
	if err != nil {
		t.Fatal(err)
	}
	next := old
	next.KeyFile = "/var/lib/hyprial-tailcat/new.key"
	if err := applyConfig(context.Background(), fake, next, &old); err == nil {
		t.Fatal("reload accepted key rotation")
	}
	if fake.up {
		t.Fatal("key rotation changed engine state")
	}
}

func TestRunStopsOnContextAndDoesNotWriteSecrets(t *testing.T) {
	fake := &fakeEngine{}
	cfg, err := LoadConfig(writeConfig(t, validConfigJSON()))
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	var diag bytes.Buffer
	go func() { done <- runWithEngine(ctx, fake, cfg, &diag) }()
	time.Sleep(10 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("runWithEngine: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("runWithEngine did not stop")
	}
	if !fake.downed {
		t.Fatal("context cancellation did not call Down")
	}
	if bytes.Contains(diag.Bytes(), []byte("privkey:")) || bytes.Contains(diag.Bytes(), []byte("psk:")) {
		t.Fatal("diagnostics leaked secret material")
	}
}
