package forward

import (
	"context"
	"io"
	"net"
	"strconv"
	"sync"
	"testing"
	"time"
)

type loopbackOverlay struct {
	mu       sync.Mutex
	inbound  net.Listener
	dialAddr string
}

func (o *loopbackOverlay) Listen(_ string, _ string) (net.Listener, error) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err == nil {
		o.mu.Lock()
		o.inbound = listener
		o.mu.Unlock()
	}
	return listener, err
}

func (o *loopbackOverlay) Dial(ctx context.Context, network, _ string) (net.Conn, error) {
	dialer := net.Dialer{}
	return dialer.DialContext(ctx, network, o.dialAddr)
}

func (o *loopbackOverlay) inboundAddr(t *testing.T) string {
	t.Helper()
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.inbound == nil {
		t.Fatal("overlay inbound listener was not created")
	}
	return o.inbound.Addr().String()
}

func echoServer(t *testing.T) (string, func()) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				_, _ = io.Copy(conn, conn)
			}()
		}
	}()
	return listener.Addr().String(), func() {
		_ = listener.Close()
		<-done
	}
}

func localPort(address string) int {
	_, raw, _ := net.SplitHostPort(address)
	port, _ := strconv.Atoi(raw)
	return port
}

func assertEcho(t *testing.T, address, payload string) net.Conn {
	t.Helper()
	conn, err := net.DialTimeout("tcp", address, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := conn.Write([]byte(payload)); err != nil {
		t.Fatal(err)
	}
	read := make([]byte, len(payload))
	if _, err := io.ReadFull(conn, read); err != nil {
		t.Fatal(err)
	}
	if string(read) != payload {
		t.Fatalf("echo = %q, want %q", read, payload)
	}
	return conn
}

func TestInboundAcceptDialsConfiguredTargetAndSplicesBothWays(t *testing.T) {
	echo, stopEcho := echoServer(t)
	defer stopEcho()
	overlay := &loopbackOverlay{}
	forwarder, err := New(overlay, Config{
		InboundTarget: echo,
		PeerPort:      7447,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer forwarder.Close()
	conn := assertEcho(t, overlay.inboundAddr(t), "inbound-real-bytes")
	_ = conn.Close()
}

func TestOutboundMapUsesKernelPortAndOverlayDial(t *testing.T) {
	echo, stopEcho := echoServer(t)
	defer stopEcho()
	overlay := &loopbackOverlay{dialAddr: echo}
	forwarder, err := New(overlay, Config{
		InboundTarget: "127.0.0.1:1",
		PeerPort:      7447,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer forwarder.Close()

	first, err := forwarder.MapPeer("100.64.0.2")
	if err != nil {
		t.Fatal(err)
	}
	second, err := forwarder.MapPeer("100.64.0.3")
	if err != nil {
		t.Fatal(err)
	}
	if first.LocalPort <= 0 || second.LocalPort <= 0 || first.LocalPort == second.LocalPort {
		t.Fatalf("kernel ports = %d, %d", first.LocalPort, second.LocalPort)
	}
	if again, err := forwarder.MapPeer("100.64.0.2"); err != nil || again != first {
		t.Fatalf("idempotent map = %#v, %v; want %#v", again, err, first)
	}
	conn := assertEcho(
		t,
		net.JoinHostPort("127.0.0.1", strconv.Itoa(first.LocalPort)),
		"outbound-real-bytes",
	)
	_ = conn.Close()
}

func TestUnmapClosesListenerAndActiveConnection(t *testing.T) {
	echo, stopEcho := echoServer(t)
	defer stopEcho()
	forwarder, err := New(&loopbackOverlay{dialAddr: echo}, Config{
		InboundTarget: "127.0.0.1:1",
		PeerPort:      7447,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer forwarder.Close()
	mapping, err := forwarder.MapPeer("100.64.0.2")
	if err != nil {
		t.Fatal(err)
	}
	address := net.JoinHostPort("127.0.0.1", strconv.Itoa(mapping.LocalPort))
	conn := assertEcho(t, address, "before-unmap")
	if !forwarder.UnmapPeer("100.64.0.2") {
		t.Fatal("existing mapping was not removed")
	}
	_ = conn.SetReadDeadline(time.Now().Add(time.Second))
	if _, err := conn.Read(make([]byte, 1)); err == nil {
		t.Fatal("active connection stayed open after unmap")
	}
	if next, err := net.DialTimeout("tcp", address, 100*time.Millisecond); err == nil {
		next.Close()
		t.Fatal("listener stayed open after unmap")
	}
}

func TestCloseEndsMidstreamConnectionWithoutHanging(t *testing.T) {
	echo, stopEcho := echoServer(t)
	defer stopEcho()
	forwarder, err := New(&loopbackOverlay{dialAddr: echo}, Config{
		InboundTarget: "127.0.0.1:1",
		PeerPort:      7447,
	})
	if err != nil {
		t.Fatal(err)
	}
	mapping, err := forwarder.MapPeer("100.64.0.2")
	if err != nil {
		t.Fatal(err)
	}
	conn := assertEcho(
		t,
		net.JoinHostPort("127.0.0.1", strconv.Itoa(mapping.LocalPort)),
		"midstream",
	)
	done := make(chan struct{})
	go func() { forwarder.Close(); close(done) }()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("Close hung with an active splice")
	}
	_ = conn.SetReadDeadline(time.Now().Add(time.Second))
	if _, err := conn.Read(make([]byte, 1)); err == nil {
		t.Fatal("peer connection remained open after forwarder close")
	}
}
