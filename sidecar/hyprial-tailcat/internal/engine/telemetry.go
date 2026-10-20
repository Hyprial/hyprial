package engine

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"reflect"
	"strings"
	"syscall"
	"time"
	"unicode"
)

const connectionHistoryCapacity = 16

type copyOutcome struct {
	bytes int64
	err   error
}

type spliceResult struct {
	bytesIn    int64
	bytesOut   int64
	durationMs int64
	closeSide  string
	errorIn    string
	errorOut   string
}

// ConnectionClose is one completed map-peer splice. In is peer-to-local;
// out is local-to-peer.
type ConnectionClose struct {
	Peer       string
	MapPort    int
	BytesIn    int64
	BytesOut   int64
	DurationMs int64
	CloseSide  string
	ErrorIn    string
	ErrorOut   string
}

// SlotConnectionTelemetry is the bounded status projection for one map-peer listener.
type SlotConnectionTelemetry struct {
	Peer        string
	MapPort     int
	ClosedCount int64
	ErrorCounts map[string]int64
	Recent      []ConnectionClose
}

// ConnectionTelemetry is the complete bounded map-peer splice projection.
type ConnectionTelemetry struct {
	RingCapacity int
	Slots        []SlotConnectionTelemetry
}

type connectionHistory struct {
	peer        string
	mapPort     int
	closedCount int64
	errorCounts map[string]int64
	recent      []ConnectionClose
}

func newConnectionHistory(peer string, mapPort int) *connectionHistory {
	return &connectionHistory{
		peer: peer, mapPort: mapPort,
		errorCounts: make(map[string]int64),
		recent:      make([]ConnectionClose, 0, connectionHistoryCapacity),
	}
}

func (h *connectionHistory) record(result spliceResult) {
	h.closedCount++
	h.errorCounts[result.errorIn]++
	h.errorCounts[result.errorOut]++
	record := ConnectionClose{
		Peer: h.peer, MapPort: h.mapPort,
		BytesIn: result.bytesIn, BytesOut: result.bytesOut,
		DurationMs: result.durationMs, CloseSide: result.closeSide,
		ErrorIn: result.errorIn, ErrorOut: result.errorOut,
	}
	if len(h.recent) == connectionHistoryCapacity {
		copy(h.recent, h.recent[1:])
		h.recent[len(h.recent)-1] = record
		return
	}
	h.recent = append(h.recent, record)
}

func (h *connectionHistory) snapshot() SlotConnectionTelemetry {
	counts := make(map[string]int64, len(h.errorCounts))
	for class, count := range h.errorCounts {
		counts[class] = count
	}
	return SlotConnectionTelemetry{
		Peer: h.peer, MapPort: h.mapPort, ClosedCount: h.closedCount,
		ErrorCounts: counts, Recent: append([]ConnectionClose(nil), h.recent...),
	}
}

type copyFunc func(io.Writer, io.Reader) (int64, error)

type directedCopy struct {
	direction string
	outcome   copyOutcome
}

func collectSplice(pair *connPair, copier copyFunc) spliceResult {
	started := time.Now()
	done := make(chan directedCopy, 2)
	go func() {
		count, err := copier(pair.left, pair.right)
		done <- directedCopy{direction: "in", outcome: copyOutcome{bytes: count, err: err}}
	}()
	go func() {
		count, err := copier(pair.right, pair.left)
		done <- directedCopy{direction: "out", outcome: copyOutcome{bytes: count, err: err}}
	}()
	first := <-done
	pair.close()
	second := <-done
	outcomes := map[string]copyOutcome{first.direction: first.outcome, second.direction: second.outcome}
	classIn := normalizeCopyError(outcomes["in"].err)
	classOut := normalizeCopyError(outcomes["out"].err)
	return spliceResult{
		bytesIn: outcomes["in"].bytes, bytesOut: outcomes["out"].bytes,
		durationMs: max(0, time.Since(started).Milliseconds()),
		closeSide:  closeSide(classIn, classOut),
		errorIn:    classIn, errorOut: classOut,
	}
}

func closeSide(classIn, classOut string) string {
	peerClosed := classIn != "canceled"
	localClosed := classOut != "canceled"
	switch {
	case peerClosed && localClosed:
		return "both"
	case peerClosed:
		return "peer"
	case localClosed:
		return "local"
	default:
		return "both"
	}
}

func normalizeCopyError(err error) string {
	switch {
	case err == nil || errors.Is(err, io.EOF):
		return "eof"
	case errors.Is(err, context.Canceled), errors.Is(err, net.ErrClosed):
		return "canceled"
	case errors.Is(err, os.ErrDeadlineExceeded):
		return "timeout"
	case errors.Is(err, syscall.ECONNRESET), errors.Is(err, syscall.EPIPE):
		return "reset"
	}
	var netError net.Error
	if errors.As(err, &netError) && netError.Timeout() {
		return "timeout"
	}
	name := strings.TrimPrefix(reflect.TypeOf(err).String(), "*")
	name = strings.Map(func(r rune) rune {
		if unicode.IsLetter(r) || unicode.IsDigit(r) || strings.ContainsRune("._-", r) {
			return unicode.ToLower(r)
		}
		return -1
	}, name)
	if name == "" {
		name = "error"
	}
	if len(name) > 40 {
		name = name[:40]
	}
	return fmt.Sprintf("other:%s", name)
}

func (e *Engine) splice(pair *connPair, cleanup func(spliceResult)) {
	e.wg.Add(1)
	go func() {
		defer e.wg.Done()
		result := collectSplice(pair, io.Copy)
		cleanup(result)
	}()
}
