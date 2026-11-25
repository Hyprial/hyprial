package labrelay

import (
	"net"
	"testing"
)

func TestNewDefaultsToLoopback(t *testing.T) {
	relay, err := New()
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(relay.Close)

	assertRelayAddresses(t, relay, "127.0.0.1", "127.0.0.1")
}

func TestNewWithOptionsBindsAndAdvertises(t *testing.T) {
	relay, err := NewWithOptions(Options{
		BindAddress:   "0.0.0.0",
		AdvertiseHost: "relay.example",
	})
	if err != nil {
		t.Fatalf("NewWithOptions: %v", err)
	}
	t.Cleanup(relay.Close)

	assertRelayAddresses(t, relay, "0.0.0.0", "relay.example")
}

func TestNewWithOptionsInfersConcreteBindAddress(t *testing.T) {
	got, err := normalizeOptions(Options{BindAddress: "192.0.2.1"})
	if err != nil {
		t.Fatalf("normalizeOptions: %v", err)
	}
	if got.AdvertiseHost != "192.0.2.1" {
		t.Fatalf("AdvertiseHost = %q, want 192.0.2.1", got.AdvertiseHost)
	}
}

func TestNewWithOptionsRejectsWildcardAdvertisement(t *testing.T) {
	relay, err := NewWithOptions(Options{BindAddress: "0.0.0.0"})
	if relay != nil {
		relay.Close()
		t.Fatal("NewWithOptions returned a relay")
	}
	if err == nil {
		t.Fatal("NewWithOptions accepted 0.0.0.0 without an advertised host")
	}
}

func TestNewWithOptionsRejectsExplicitWildcardAdvertisement(t *testing.T) {
	relay, err := NewWithOptions(Options{
		BindAddress:   "127.0.0.1",
		AdvertiseHost: "0.0.0.0",
	})
	if relay != nil {
		relay.Close()
		t.Fatal("NewWithOptions returned a relay")
	}
	if err == nil {
		t.Fatal("NewWithOptions accepted 0.0.0.0 as the advertised host")
	}
}

func TestNewWithOptionsRequiresAdvertisementForNamedBind(t *testing.T) {
	relay, err := NewWithOptions(Options{BindAddress: "localhost"})
	if relay != nil {
		relay.Close()
		t.Fatal("NewWithOptions returned a relay")
	}
	if err == nil {
		t.Fatal("NewWithOptions accepted a named bind address without an advertised host")
	}
}

func assertRelayAddresses(t *testing.T, relay *Relay, wantBind, wantHost string) {
	t.Helper()

	tcpAddress := relay.tls.Listener.Addr().(*net.TCPAddr)
	if got := tcpAddress.IP.String(); got != wantBind {
		t.Errorf("DERP bind address = %q, want %q", got, wantBind)
	}
	udpAddress := relay.stun.LocalAddr().(*net.UDPAddr)
	if got := udpAddress.IP.String(); got != wantBind {
		t.Errorf("STUN bind address = %q, want %q", got, wantBind)
	}
	if got := relay.Config().Host; got != wantHost {
		t.Errorf("Config().Host = %q, want %q", got, wantHost)
	}
}
