// Package mobileclient provides a bounded, single-service native dial boundary.
// It owns an injected Tailcat-compatible transport; it does not create identity,
// discover targets, expose a local listener, or authenticate application requests.
package mobileclient

import (
	"context"
	"errors"
	"net"
	"sync"
	"time"
)

var (
	ErrConfig      = errors.New("invalid native dial configuration")
	ErrClosed      = errors.New("native dial client closed")
	ErrBusy        = errors.New("native dial connection limit reached")
	ErrUnavailable = errors.New("native dial transport unavailable")
)

// Transport is implemented by *tailcat.Client. Only a trusted native composition
// root may supply it, after obtaining identity and admission through official APIs.
// Ownership transfers to Client on successful New. Do not share it with a sidecar.
// DialTCPPort must honor context cancellation; Close must release its resources.
type Transport interface {
	DialTCPPort(context.Context, uint16) (net.Conn, error)
	Close() error
}

// Client binds one transport generation to one approved service port. Its zero
// value is not usable. It is not a gomobile/FFI ABI and must not be exposed directly
// to a WebView: net.Conn/context are native Go interfaces, not JSON parameters.
type Client struct {
	transport Transport
	port      uint16
	timeout   time.Duration
	limit     int
	mu        sync.Mutex
	closed    bool
	pending   map[*dial]struct{}
	streams   map[*stream]struct{}
	closeDone chan struct{}
	closeErr  error
}
type dial struct{ cancel context.CancelFunc }

// New takes a non-nil transport, a trusted fixed port, a dial timeout in (0,30s],
// and a capacity in [1,32] counting pending dials and open streams. No arbitrary
// address/network/port parameter is accepted by the later DialContext call.
func New(transport Transport, port uint16, timeout time.Duration, limit int) (*Client, error) {
	if transport == nil || port == 0 || timeout <= 0 || timeout > 30*time.Second || limit < 1 || limit > 32 {
		return nil, ErrConfig
	}
	return &Client{transport: transport, port: port, timeout: timeout, limit: limit,
		pending: make(map[*dial]struct{}), streams: make(map[*stream]struct{}), closeDone: make(chan struct{})}, nil
}

// DialContext opens only the bound service. Failures never automatically retry:
// a later application send may already have been accepted and needs its original
// idempotency key. Underlying dial errors are redacted because they may embed a
// Tailcat secret address. Caller cancellation/deadline retain their stable codes.
func (c *Client) DialContext(ctx context.Context) (net.Conn, error) {
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return nil, ErrClosed
	}
	if err := ctx.Err(); err != nil {
		c.mu.Unlock()
		return nil, err
	}
	if len(c.pending)+len(c.streams) >= c.limit {
		c.mu.Unlock()
		return nil, ErrBusy
	}
	dialCtx, cancel := context.WithTimeout(ctx, c.timeout)
	attempt := &dial{cancel: cancel}
	c.pending[attempt] = struct{}{}
	c.mu.Unlock()
	conn, err := c.transport.DialTCPPort(dialCtx, c.port)
	contextErr := dialCtx.Err()
	cancel()
	c.mu.Lock()
	delete(c.pending, attempt)
	switch {
	case c.closed:
		err = ErrClosed
	case contextErr != nil:
		err = contextErr
	case err != nil || conn == nil:
		err = ErrUnavailable
	}
	if err != nil {
		c.mu.Unlock()
		if conn != nil {
			_ = conn.Close()
		}
		return nil, err
	}
	owned := &stream{Conn: conn, owner: c}
	c.streams[owned] = struct{}{}
	c.mu.Unlock()
	return owned, nil
}

// Close is terminal and idempotent: cancel pending dials, close active streams,
// close the owned transport exactly once. A misbehaving transport that returns a
// stream after cancellation has that stream closed by DialContext. Construct a
// fresh Client/transport generation to resume after suspension or revocation.
// No Go runtime, identity registration, daemon, or harness is started here.
func (c *Client) Close() error {
	c.mu.Lock()
	if c.closed {
		done := c.closeDone
		c.mu.Unlock()
		<-done
		return c.closeErr
	}
	c.closed = true
	attempts := make([]*dial, 0, len(c.pending))
	for p := range c.pending {
		attempts = append(attempts, p)
	}
	streams := make([]*stream, 0, len(c.streams))
	for s := range c.streams {
		streams = append(streams, s)
	}
	c.mu.Unlock()
	for _, p := range attempts {
		p.cancel()
	}
	for _, s := range streams {
		_ = s.Close()
	}
	if err := c.transport.Close(); err != nil {
		c.closeErr = ErrUnavailable
	}
	close(c.closeDone)
	return c.closeErr
}

type stream struct {
	net.Conn
	owner    *Client
	once     sync.Once
	closeErr error
}

func (s *stream) Close() error {
	s.once.Do(func() {
		s.closeErr = s.Conn.Close()
		s.owner.mu.Lock()
		delete(s.owner.streams, s)
		s.owner.mu.Unlock()
	})
	return s.closeErr
}
