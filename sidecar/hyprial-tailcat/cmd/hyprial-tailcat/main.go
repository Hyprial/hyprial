// hyprial-tailcat is the hyprial Tailcat sidecar: version, genkey, and
// the forward stdio protocol v3. See
// docs/design/tailnet-cutover-architecture-2026-10-03.md §3.
//
// Secrets never enter argv or stdout: genkey takes only an output path,
// and forward reads the key file path and peer addresses from stdin.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/buildinfo"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/keys"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/proto"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/serve"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/servicehost"
)

const usage = "usage: hyprial-tailcat version|genkey --out <abs path> [--force]|forward|host --config <abs path>"

// run is the testable core of main; it returns the process exit code.
func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprintln(stderr, usage)
		return 2
	}
	switch args[0] {
	case "version":
		if len(args) != 1 {
			fmt.Fprintln(stderr, usage)
			return 2
		}
		_ = json.NewEncoder(stdout).Encode(map[string]any{
			"v":       proto.Version,
			"sidecar": buildinfo.SidecarVersion,
			"tailcat": buildinfo.TailcatCommit,
		})
		return 0
	case "genkey":
		return genkey(args[1:], stdout, stderr)
	case "forward":
		if len(args) != 1 {
			fmt.Fprintln(stderr, usage)
			return 2
		}
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		defer stop()
		if err := serve.Run(ctx, stdin, stdout, stderr); err != nil {
			// Never print nested runtime errors: protocol events already
			// carry a bounded, non-secret code and message.
			fmt.Fprintln(stderr, "hyprial-tailcat: forward failed")
			return 1
		}
		return 0
	case "host":
		return host(args[1:], stderr)
	default:
		fmt.Fprintln(stderr, usage)
		return 2
	}
}

func host(args []string, stderr io.Writer) int {
	flags := flag.NewFlagSet("host", flag.ContinueOnError)
	flags.SetOutput(stderr)
	config := flags.String("config", "", "absolute path of the host config")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	if *config == "" || !filepath.IsAbs(*config) || flags.NArg() != 0 {
		fmt.Fprintln(stderr, "host: --config requires an absolute path")
		return 2
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := servicehost.Run(ctx, *config, stderr); err != nil {
		fmt.Fprintln(stderr, "hyprial-tailcat: host failed")
		return 1
	}
	return 0
}

// genkey implements `genkey --out <abs path> [--force]`. stdout carries
// only the public keys; private keys and the PSK stay in the key file.
func genkey(args []string, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("genkey", flag.ContinueOnError)
	flags.SetOutput(stderr)
	out := flags.String("out", "", "absolute path of the device key file to create")
	force := flags.Bool("force", false, "overwrite an existing key file")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	if *out == "" || !filepath.IsAbs(*out) {
		fmt.Fprintln(stderr, "genkey: --out requires an absolute path")
		return 2
	}
	public, err := keys.Generate(*out, *force)
	if errors.Is(err, keys.ErrExists) {
		// Idempotent: callers re-run genkey on every login/republish, so
		// an existing key still yields its public keys (exit 3 = "kept").
		material, loadErr := keys.Load(*out)
		if loadErr != nil {
			fmt.Fprintln(stderr, "genkey: key file exists but is invalid (use --force to overwrite)")
			return 1
		}
		fmt.Fprintln(stderr, "genkey: key file already exists; kept it (use --force to overwrite)")
		writePublic(stdout, keys.PublicKeys{
			ServerPublic: material.ServerPublic().String(),
			ClientPublic: material.ClientPublic().String(),
		})
		return 3
	}
	if err != nil {
		fmt.Fprintln(stderr, "genkey: cannot create key file")
		return 1
	}
	writePublic(stdout, public)
	return 0
}

// writePublic prints the only genkey output: the two public keys.
func writePublic(stdout io.Writer, public keys.PublicKeys) {
	_ = json.NewEncoder(stdout).Encode(map[string]any{
		"v":            proto.Version,
		"serverPublic": public.ServerPublic,
		"clientPublic": public.ClientPublic,
	})
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}
