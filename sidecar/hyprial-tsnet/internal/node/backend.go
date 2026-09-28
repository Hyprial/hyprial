package node

import (
	"context"
	"fmt"
	"io"
	"net"
	"os"
	"regexp"
	"strings"

	"tailscale.com/client/local"
	"tailscale.com/ipn"
	"tailscale.com/ipn/ipnstate"
	"tailscale.com/tsnet"
)

// Watcher and Backend retain the real WatchIPNBus/ipn.Notify shape at the
// test seam. Tests inject notifications, not a second invented state model.
type Watcher interface {
	Next() (ipn.Notify, error)
	Close() error
}

type Backend interface {
	Start() error
	WatchIPNBus(context.Context, ipn.NotifyWatchOpt) (Watcher, error)
	Status(context.Context) (*ipnstate.Status, error)
	Close() error
}

type DataBackend interface {
	Backend
	Listen(network, address string) (net.Listener, error)
	Dial(context.Context, string, string) (net.Conn, error)
}

type Config struct {
	ControlURL string
	Hostname   string
	Dir        string
	Ephemeral  bool
	AuthKey    string
	HasAuthKey bool
}

type BackendFactory func(Config) Backend

var (
	tsKeyPattern   = regexp.MustCompile(`(?i)\btskey-[a-z0-9_-]+`)
	authKeyPattern = regexp.MustCompile(`(?i)((?:authentication|auth)[_ -]?key\s*[:=]\s*)\S+`)
)

func redactUserLog(text string) string {
	text = tsKeyPattern.ReplaceAllString(text, "[REDACTED]")
	return authKeyPattern.ReplaceAllString(text, "${1}[REDACTED]")
}

func userLogf(writer io.Writer) func(string, ...any) {
	return func(format string, args ...any) {
		line := strings.TrimRight(fmt.Sprintf(format, args...), "\r\n")
		_, _ = fmt.Fprintln(writer, redactUserLog(line))
	}
}

func TSNetFactory(config Config) Backend {
	server := &tsnet.Server{
		ControlURL: config.ControlURL,
		Hostname:   config.Hostname,
		Dir:        config.Dir,
		Ephemeral:  config.Ephemeral,
		// Verbose logs stay disabled. UserLogf carries the operator-facing join
		// URL and must stay on stderr because stdout is the JSON protocol.
		Logf:     func(string, ...any) {},
		UserLogf: userLogf(os.Stderr),
	}
	if config.HasAuthKey {
		server.AuthKey = config.AuthKey
	}
	return &tsBackend{server: server}
}

type tsBackend struct {
	server *tsnet.Server
	client *local.Client
}

func (b *tsBackend) Start() error {
	if err := b.server.Start(); err != nil {
		return err
	}
	client, err := b.server.LocalClient()
	if err != nil {
		return err
	}
	b.client = client
	return nil
}

func (b *tsBackend) WatchIPNBus(ctx context.Context, mask ipn.NotifyWatchOpt) (Watcher, error) {
	return b.client.WatchIPNBus(ctx, mask)
}

func (b *tsBackend) Status(ctx context.Context) (*ipnstate.Status, error) {
	return b.client.Status(ctx)
}

func (b *tsBackend) Close() error { return b.server.Close() }

func (b *tsBackend) Listen(network, address string) (net.Listener, error) {
	return b.server.Listen(network, address)
}

func (b *tsBackend) Dial(ctx context.Context, network, address string) (net.Conn, error) {
	return b.server.Dial(ctx, network, address)
}
