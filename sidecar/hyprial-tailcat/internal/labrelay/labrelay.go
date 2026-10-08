// Package labrelay runs a local DERP+STUN relay for tests, mirroring the
// evidence harness in docs/evidence/tailcat-casdoor-2026-10-02/. It exists
// so engine and serve tests exercise real Tailcat data-plane I/O without
// touching Tailscale's production DERP relays.
package labrelay

import (
	"fmt"
	"net"
	"net/http/httptest"
	"testing"

	"tailscale.com/derp/derpserver"
	"tailscale.com/envknob"
	"tailscale.com/net/stun"
	"tailscale.com/types/key"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/proto"
)

func quiet(string, ...any) {}

const defaultAddress = "127.0.0.1"

// Options controls where the relay listens and which host clients use.
type Options struct {
	BindAddress   string
	AdvertiseHost string
}

// Relay is a running local DERP+STUN pair.
type Relay struct {
	derp *derpserver.Server
	tls  *httptest.Server
	stun *net.UDPConn
	host string
}

// New launches the relay outside a test (the standalone dev command); the
// caller owns Close.
func New() (*Relay, error) {
	return NewWithOptions(Options{
		BindAddress:   defaultAddress,
		AdvertiseHost: defaultAddress,
	})
}

// NewWithOptions launches a relay with explicit listen and advertised hosts.
func NewWithOptions(options Options) (*Relay, error) {
	options, err := normalizeOptions(options)
	if err != nil {
		return nil, err
	}

	// Same knobs as the reference evidence harness: force the DERP path so
	// tests never depend on host NAT behavior.
	envknob.Setenv("IN_TS_TEST", "true")
	envknob.Setenv("TS_DEBUG_ALWAYS_USE_DERP", "true")

	relay := &Relay{host: options.AdvertiseHost}
	relay.derp = derpserver.New(key.NewNode(), quiet)
	relay.tls = httptest.NewUnstartedServer(derpserver.Handler(relay.derp))
	_ = relay.tls.Listener.Close()
	tlsListener, err := net.Listen(networkForBind("tcp", options.BindAddress), net.JoinHostPort(options.BindAddress, "0"))
	if err != nil {
		relay.derp.Close()
		return nil, fmt.Errorf("DERP listen on %q: %w", options.BindAddress, err)
	}
	relay.tls.Listener = tlsListener
	relay.tls.StartTLS()

	udpNetwork := networkForBind("udp", options.BindAddress)
	udpAddress, err := net.ResolveUDPAddr(udpNetwork, net.JoinHostPort(options.BindAddress, "0"))
	if err != nil {
		relay.tls.Close()
		relay.derp.Close()
		return nil, fmt.Errorf("resolve STUN bind address %q: %w", options.BindAddress, err)
	}
	udp, err := net.ListenUDP(udpNetwork, udpAddress)
	if err != nil {
		relay.tls.Close()
		relay.derp.Close()
		return nil, fmt.Errorf("STUN listen on %q: %w", options.BindAddress, err)
	}
	relay.stun = udp
	go func() {
		buf := make([]byte, 65536)
		for {
			n, src, err := udp.ReadFromUDPAddrPort(buf)
			if err != nil {
				return
			}
			if tx, err := stun.ParseBindingRequest(buf[:n]); err == nil {
				_, _ = udp.WriteToUDPAddrPort(stun.Response(tx, src), src)
			}
		}
	}()
	return relay, nil
}

func normalizeOptions(options Options) (Options, error) {
	if options.BindAddress == "" {
		options.BindAddress = defaultAddress
	}
	if options.AdvertiseHost == "" {
		bindIP := net.ParseIP(options.BindAddress)
		if bindIP == nil {
			return Options{}, fmt.Errorf(
				"advertise host is required when bind address %q is not an IP",
				options.BindAddress,
			)
		}
		if bindIP.IsUnspecified() {
			return Options{}, fmt.Errorf(
				"advertise host is required when bind address is %q",
				options.BindAddress,
			)
		}
		options.AdvertiseHost = bindIP.String()
	}
	if advertisedIP := net.ParseIP(options.AdvertiseHost); advertisedIP != nil && advertisedIP.IsUnspecified() {
		return Options{}, fmt.Errorf("advertise host must not be %q", options.AdvertiseHost)
	}
	return options, nil
}

func networkForBind(network, bindAddress string) string {
	bindIP := net.ParseIP(bindAddress)
	if bindIP == nil {
		return network
	}
	if bindIP.To4() != nil {
		return network + "4"
	}
	return network + "6"
}

// Close stops the relay.
func (r *Relay) Close() {
	r.tls.Close()
	r.derp.Close()
	r.stun.Close()
}

// Start launches the relay and registers cleanup with t.
func Start(t testing.TB) *Relay {
	t.Helper()
	relay, err := New()
	if err != nil {
		t.Fatalf("stun listen: %v", err)
	}
	t.Cleanup(relay.Close)
	return relay
}

// Config returns the up command's derp object pointing at this relay.
func (r *Relay) Config() *proto.DERPConfig {
	return &proto.DERPConfig{
		Host:             r.host,
		DERPPort:         r.tls.Listener.Addr().(*net.TCPAddr).Port,
		STUNPort:         r.stun.LocalAddr().(*net.UDPAddr).Port,
		InsecureForTests: true,
	}
}
