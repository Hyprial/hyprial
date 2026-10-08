package servicehost

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/engine"
)

type lifecycle interface {
	Up(context.Context, engine.UpConfig) (string, string, error)
	Allow([]string, bool) (int, error)
	Expose(context.Context, engine.Exposure) (engine.Exposure, error)
	Unexpose(int) error
	Down()
}

// Run supervises one engine from a config file. SIGHUP validates and applies
// a replacement config; invalid replacements leave the current config live.
// SIGTERM and context cancellation close all listeners and active streams.
func Run(ctx context.Context, configPath string, diag io.Writer) error {
	cfg, err := LoadConfig(configPath)
	if err != nil {
		return fmt.Errorf("invalid host config")
	}
	return runWithSignals(ctx, &engine.Engine{}, cfg, configPath, diag)
}

func runWithEngine(ctx context.Context, eng lifecycle, cfg Config, diag io.Writer) error {
	if err := applyConfig(ctx, eng, cfg, nil); err != nil {
		return err
	}
	defer eng.Down()
	<-ctx.Done()
	return nil
}

func runWithSignals(ctx context.Context, eng lifecycle, cfg Config, configPath string, diag io.Writer) error {
	if err := applyConfig(ctx, eng, cfg, nil); err != nil {
		return fmt.Errorf("host start failed")
	}
	defer eng.Down()

	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGHUP, syscall.SIGTERM)
	defer signal.Stop(signals)
	current := cfg
	for {
		select {
		case <-ctx.Done():
			return nil
		case sig := <-signals:
			if sig == syscall.SIGTERM {
				return nil
			}
			next, err := LoadConfig(configPath)
			if err != nil {
				writeDiag(diag, "service host: reload rejected; keeping current configuration")
				continue
			}
			if err := applyConfig(ctx, eng, next, &current); err != nil {
				writeDiag(diag, "service host: reload failed; keeping current configuration")
				continue
			}
			current = next
		}
	}
}

func applyConfig(ctx context.Context, eng lifecycle, cfg Config, previous *Config) error {
	if previous == nil {
		if _, _, err := eng.Up(ctx, engine.UpConfig{
			KeyFile: cfg.KeyFile, AddressFile: cfg.AddressFile,
			Allow: cfg.ClientPublic, AllowAny: false,
		}); err != nil {
			return fmt.Errorf("engine start failed")
		}
		for _, exposure := range cfg.Exposures {
			if _, err := eng.Expose(ctx, toEngineExposure(exposure)); err != nil {
				eng.Down()
				return fmt.Errorf("exposure setup failed")
			}
		}
		return nil
	}
	if cfg.KeyFile != previous.KeyFile || cfg.AddressFile != previous.AddressFile {
		return fmt.Errorf("key and address paths require restart")
	}
	// Admission is an independent safety boundary. Apply it before touching
	// exposures so a revoked caller cannot remain admitted merely because a
	// later exposure update or rollback fails.
	if _, err := eng.Allow(cfg.ClientPublic, false); err != nil {
		return fmt.Errorf("admission update failed")
	}

	old := exposuresByPort(previous.Exposures)
	next := exposuresByPort(cfg.Exposures)
	removed := make([]engine.Exposure, 0)
	for port, oldExposure := range old {
		newExposure, exists := next[port]
		if !exists || newExposure != oldExposure {
			if err := eng.Unexpose(port); err != nil {
				return fmt.Errorf("exposure removal failed")
			}
			removed = append(removed, toEngineExposure(oldExposure))
		}
	}
	added := make([]engine.Exposure, 0)
	for port, exposure := range next {
		if oldExposure, exists := old[port]; exists && oldExposure == exposure {
			continue
		}
		converted := toEngineExposure(exposure)
		if _, err := eng.Expose(ctx, converted); err != nil {
			for _, addedExposure := range added {
				_ = eng.Unexpose(addedExposure.Port)
			}
			for _, removedExposure := range removed {
				_, _ = eng.Expose(ctx, removedExposure)
			}
			return fmt.Errorf("exposure setup failed")
		}
		added = append(added, converted)
	}
	return nil
}

func exposuresByPort(exposures []Exposure) map[int]Exposure {
	result := make(map[int]Exposure, len(exposures))
	for _, exposure := range exposures {
		result[exposure.Port] = exposure
	}
	return result
}

func toEngineExposure(exposure Exposure) engine.Exposure {
	return engine.Exposure{Port: exposure.Port, Target: exposure.Target, ProxyProtocol: exposure.ProxyProtocol}
}

func writeDiag(writer io.Writer, message string) {
	if writer != nil {
		_, _ = io.WriteString(writer, message+"\n")
	}
}
