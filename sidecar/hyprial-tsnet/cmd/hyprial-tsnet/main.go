package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tsnet/internal/buildinfo"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tsnet/internal/node"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tsnet/internal/serve"
)

func terminationContext() (context.Context, context.CancelFunc) {
	return signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
}

func main() {
	// This is intentionally the first side effect: ambient credentials and
	// proxies cannot influence tsnet and cannot survive into child activity.
	node.ClearAmbientCredentialsAndProxy()

	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: hyprial-tsnet serve|forward|version")
		os.Exit(2)
	}
	ctx, stop := terminationContext()
	defer stop()
	switch os.Args[1] {
	case "version":
		_ = json.NewEncoder(os.Stdout).Encode(map[string]string{
			"sidecar":   buildinfo.SidecarVersion,
			"tailscale": buildinfo.TailscaleVersion,
		})
	case "serve":
		if err := serve.Run(ctx, os.Stdin, os.Stdout, node.TSNetFactory); err != nil {
			// Never print nested runtime errors: they may contain control-plane
			// URLs or registration material. Protocol events already carry a
			// bounded, non-secret error code/message.
			fmt.Fprintln(os.Stderr, "hyprial-tsnet: serve failed")
			os.Exit(1)
		}
	case "forward":
		if err := serve.RunForwarding(ctx, os.Stdin, os.Stdout, node.TSNetFactory); err != nil {
			fmt.Fprintln(os.Stderr, "hyprial-tsnet: forwarding failed")
			os.Exit(1)
		}
	default:
		fmt.Fprintln(os.Stderr, "usage: hyprial-tsnet serve|forward|version")
		os.Exit(2)
	}
}
