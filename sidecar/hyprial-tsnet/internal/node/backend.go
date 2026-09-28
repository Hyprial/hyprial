package node

import (
	"context"
	"net"

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

func TSNetFactory(config Config) Backend {
	server := &tsnet.Server{
		ControlURL: config.ControlURL,
		Hostname:   config.Hostname,
		Dir:        config.Dir,
		Ephemeral:  config.Ephemeral,
		// Deliberately install non-logging callbacks. Upstream diagnostic text
		// can contain URLs and registration material; protocol events provide
		// the bounded operator-visible state without leaking those strings.
		Logf:     func(string, ...any) {},
		UserLogf: func(string, ...any) {},
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
