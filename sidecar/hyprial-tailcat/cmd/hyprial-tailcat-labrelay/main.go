// Command hyprial-tailcat-labrelay runs a local DERP+STUN relay for isolated
// end-to-end tests (two daemons on one machine) so they never touch
// Tailscale's production DERP relays. It is a development tool: it is not
// built into the wheel and must never be used as a production relay
// (TLS verification is disabled for its clients via insecureForTests).
//
// It prints one JSON line, the `up` command's `derp` object, then serves
// until stdin closes or it receives SIGINT/SIGTERM.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/labrelay"
)

func main() {
	if err := run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr); err != nil {
		fmt.Fprintln(os.Stderr, "hyprial-tailcat-labrelay:", err)
		os.Exit(1)
	}
}

func run(args []string, stdin io.Reader, stdout, stderr io.Writer) error {
	options, err := parseOptions(args, stderr)
	if err != nil {
		return err
	}
	relay, err := labrelay.NewWithOptions(options)
	if err != nil {
		return err
	}
	defer relay.Close()
	if err := json.NewEncoder(stdout).Encode(map[string]any{"v": 3, "derp": relay.Config()}); err != nil {
		return fmt.Errorf("encode relay config: %w", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	stdinClosed := make(chan struct{}, 1)
	go func() {
		_, _ = io.Copy(io.Discard, stdin)
		stdinClosed <- struct{}{}
	}()
	select {
	case <-ctx.Done():
	case <-stdinClosed:
	}
	return nil
}

func parseOptions(args []string, stderr io.Writer) (labrelay.Options, error) {
	options := labrelay.Options{}
	flags := flag.NewFlagSet("hyprial-tailcat-labrelay", flag.ContinueOnError)
	flags.SetOutput(stderr)
	flags.StringVar(&options.BindAddress, "bind", "127.0.0.1", "address for DERP and STUN listeners")
	flags.StringVar(&options.AdvertiseHost, "advertise-host", "127.0.0.1", "host advertised to relay clients")
	if err := flags.Parse(args); err != nil {
		return labrelay.Options{}, err
	}
	if flags.NArg() != 0 {
		return labrelay.Options{}, fmt.Errorf("unexpected arguments: %v", flags.Args())
	}
	return options, nil
}
