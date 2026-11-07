// Diagnostics observe failures; only the control transport owns its lifetime.
// Each observation covers one control dial, including optional netcheck logs.
// Raw errors can contain proxy credentials or Tailcat PSKs and are never emitted.
package outbound

import (
	"context"
	"errors"
	"fmt"
	"net"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
)

type Attempt struct {
	Node   string `json:"node"`
	Cause  string `json:"cause"`
	Source string `json:"source,omitempty"`
}

type Detail struct {
	Stage         string    `json:"stage"`
	Cause         string    `json:"cause"`
	Proxy         string    `json:"proxy"`
	Attempts      []Attempt `json:"attempts,omitempty"`
	BytesReceived *int      `json:"bytesReceived,omitempty"`
}

var causes = []string{"proxy-rejected", "connection-refused", "dns-failed", "tls-failed", "deadline-exceeded", "transport-failed", "response-timeout"}
var hostPattern = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}$`)
var portPattern = regexp.MustCompile(`^[0-9]{1,5}$`)

func validNode(node string) bool {
	host, port, err := net.SplitHostPort(node)
	n, portErr := strconv.Atoi(port)
	return err == nil && portErr == nil && portPattern.MatchString(port) && n > 0 && n <= 65535 &&
		(hostPattern.MatchString(host) || net.ParseIP(host) != nil)
}

func (d Detail) Valid() bool {
	if d.BytesReceived != nil && (d.Stage != "control" ||
		(d.Cause != "deadline-exceeded" && d.Cause != "transport-failed") ||
		*d.BytesReceived < 0 || *d.BytesReceived > (1<<20)+1) {
		return false
	}
	if !slices.Contains([]string{"control", "derp", "sidecar"}, d.Stage) ||
		!slices.Contains(causes, d.Cause) ||
		!slices.Contains([]string{"configured", "not-configured"}, d.Proxy) {
		return false
	}
	for _, a := range d.Attempts {
		if !validNode(a.Node) || !slices.Contains(causes, a.Cause) || (a.Source != "" && a.Source != "diagnostic-probe") {
			return false
		}
	}
	return true
}

func (d Detail) Message() string {
	message := d.Stage + " unreachable: " + d.Cause + " (proxy " + d.Proxy + ")."
	for _, a := range d.Attempts {
		message += " " + a.Node + ": " + a.Cause
		if a.Source == "diagnostic-probe" {
			message += " (diagnostic probe)"
		}
		message += "."
	}
	return message
}

type Diagnostics struct {
	mu        sync.Mutex
	observers map[*Observation]struct{}
}

type Observation struct {
	d         *Diagnostics
	cause     string
	connected bool
	attempts  map[string]string
}

func (d *Diagnostics) Begin() *Observation {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.observers == nil {
		d.observers = make(map[*Observation]struct{})
	}
	o := &Observation{d: d, attempts: make(map[string]string)}
	d.observers[o] = struct{}{}
	return o
}

func (o *Observation) Close() {
	o.d.mu.Lock()
	defer o.d.mu.Unlock()
	delete(o.d.observers, o)
}

func classify(text string) string {
	switch {
	case strings.Contains(text, "connection refused"):
		return "connection-refused"
	case strings.Contains(text, "no such host"):
		return "dns-failed"
	case strings.Contains(text, "certificate") || strings.Contains(text, "tls: ") || strings.Contains(text, "tls handshake"):
		return "tls-failed"
	case strings.Contains(text, "timeout") || strings.Contains(text, "deadline exceeded"):
		return "deadline-exceeded"
	}
	return ""
}

func (d *Diagnostics) Logf(format string, args ...any) {
	text := strings.ToLower(fmt.Sprintf(format, args...))
	if !strings.Contains(text, "derp") {
		return
	}
	cause := classify(text)
	node := ""
	connected := false
	if _, rest, ok := strings.Cut(text, "derphttp: connect dial to "); ok {
		target, status, _ := strings.Cut(rest, ": ")
		if validNode(target) {
			node = target
		}
		code, _, _ := strings.Cut(status, " ")
		n, _ := strconv.Atoi(code)
		switch {
		case n == 200:
			connected = true
			cause = ""
		case n >= 100 && n <= 599:
			cause = "proxy-rejected"
		case cause == "":
			cause = "transport-failed"
		}
	}
	if cause == "" && !connected {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	for o := range d.observers {
		if connected {
			o.connected = true
			delete(o.attempts, node)
		} else {
			o.cause = cause
			if node != "" {
				o.attempts[node] = cause
			}
		}
	}
}

func ProxyMode() string {
	if len(environment.auth) > 0 {
		return "configured"
	}
	return "not-configured"
}

func ControlFailure(err error) Detail {
	result := Detail{Stage: "control", Cause: "transport-failed", Proxy: ProxyMode()}
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
		result.Cause = "deadline-exceeded"
	}
	var op *net.OpError
	if errors.As(err, &op) && op.Timeout() {
		result.Cause = "deadline-exceeded"
	}
	return result
}

func (o *Observation) Failure(err error) Detail {
	result := ControlFailure(err)
	o.d.mu.Lock()
	defer o.d.mu.Unlock()
	if o.cause != "" && !o.connected {
		result.Stage = "derp"
		result.Cause = o.cause
	}
	for node, cause := range o.attempts {
		result.Attempts = append(result.Attempts, Attempt{Node: node, Cause: cause})
	}
	sort.Slice(result.Attempts, func(i, j int) bool { return result.Attempts[i].Node < result.Attempts[j].Node })
	return result
}
