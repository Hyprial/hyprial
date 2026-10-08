// Package serve implements the protocol-v3 stdio lifecycle of
// `hyprial-tailcat forward` (design doc §3.3): emit hello, consume
// newline-delimited JSON commands, emit events, and on stdin EOF or down
// shut everything down, emit exited, and return.
//
// Protocol violations (bad JSON, unknown op or field, wrong version) are
// answered with a BAD_REQUEST error event and the loop continues; only
// output failures and down/EOF end the process. Diagnostics go to the diag
// writer (stderr) and never contain secrets.
package serve

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/buildinfo"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/engine"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/keys"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/proto"
)

const maxRequestLine = 1024 * 1024

// Run drives the forward protocol until down, EOF, or ctx cancellation.
// It always emits hello first and exited last.
func Run(ctx context.Context, input io.Reader, output io.Writer, diag io.Writer) error {
	writer := proto.NewWriter(output)
	if err := writer.Write(proto.Hello(buildinfo.SidecarVersion, buildinfo.TailcatCommit)); err != nil {
		return err
	}
	eng := &engine.Engine{}
	logf := func(format string, args ...any) {
		if diag != nil {
			fmt.Fprintf(diag, "hyprial-tailcat: "+format+"\n", args...)
		}
	}

	lines := make(chan []byte)
	scanErr := make(chan error, 1)
	go scanLines(input, lines, scanErr)

	shutdown := func() error {
		eng.Down()
		return writer.Write(proto.Exited())
	}

	for {
		select {
		case <-ctx.Done():
			return shutdown()
		case err := <-scanErr:
			if err == nil {
				return shutdown()
			}
			if werr := writer.Write(proto.Error(proto.CodeBadRequest, "invalid JSON-lines input")); werr != nil {
				eng.Down()
				return werr
			}
			return shutdown()
		case line := <-lines:
			command, err := proto.ParseCommand(line)
			if err != nil {
				logf("rejected malformed request")
				event := proto.Error(proto.CodeBadRequest, "invalid protocol-v3 request")
				event.RequestID = parseErrorRequestID(line)
				if err := writer.Write(event); err != nil {
					eng.Down()
					return err
				}
				continue
			}
			if command.Op == "down" {
				return shutdown()
			}
			if err := handle(ctx, eng, command, writer); err != nil {
				eng.Down()
				return err
			}
		}
	}
}

func parseErrorRequestID(line []byte) string {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(line, &raw); err != nil || raw == nil {
		return ""
	}
	var requestID string
	if err := json.Unmarshal(raw["requestId"], &requestID); err != nil ||
		requestID == "" || len(requestID) > proto.MaxRequestIDLength {
		return ""
	}
	return requestID
}

// handle executes one parsed command and writes its success or error
// event. A nil return means the loop continues.
func handle(ctx context.Context, eng *engine.Engine, command proto.Command, writer *proto.Writer) error {
	switch command.Op {
	case "up":
		if err := writer.Write(proto.State("Starting")); err != nil {
			return err
		}
		serverPublic, clientPublic, err := eng.Up(ctx, upConfig(command.Up))
		if err != nil {
			return writeError(writer, err, proto.CodeStartFailed, command.RequestID)
		}
		return writer.Write(proto.RunningState(serverPublic, clientPublic, nil))
	case "status":
		state, serverPublic, clientPublic, peers := eng.Status()
		if state != "Running" {
			return writeResponse(writer, proto.State("Down"), command.RequestID)
		}
		return writeResponse(writer, proto.RunningState(serverPublic, clientPublic, toPeerMappings(peers)), command.RequestID)
	case "map-peer":
		mapping, err := eng.MapPeer(command.Peer, command.Address, command.Port)
		if err != nil {
			return writeError(writer, err, proto.CodeInternal, command.RequestID)
		}
		return writeResponse(writer, proto.PeerMapped(mapping.Peer, mapping.LocalPort), command.RequestID)
	case "map-service", "unmap-service", "service-status":
		return handleService(eng, command, writer)
	case "unmap-peer":
		if err := eng.UnmapPeer(command.Peer); err != nil {
			return writeError(writer, err, proto.CodeInternal, command.RequestID)
		}
		return writeResponse(writer, proto.PeerUnmapped(command.Peer), command.RequestID)
	case "allow":
		count, err := eng.Allow(command.Keys, command.AllowAny)
		if err != nil {
			return writeError(writer, err, proto.CodeInternal, command.RequestID)
		}
		return writeResponse(writer, proto.Allowed(count, command.AllowAny), command.RequestID)
	case "expose":
		exposure, err := eng.Expose(ctx, engine.Exposure{
			Port: command.Port, Target: command.Target, ProxyProtocol: command.ProxyProtocol,
		})
		if err != nil {
			return writeError(writer, err, proto.CodeInternal, command.RequestID)
		}
		return writeResponse(writer, proto.Exposed(exposure.Port, exposure.Target, exposure.ProxyProtocol), command.RequestID)
	case "unexpose":
		if err := eng.Unexpose(command.Port); err != nil {
			return writeError(writer, err, proto.CodeInternal, command.RequestID)
		}
		return writeResponse(writer, proto.Unexposed(command.Port), command.RequestID)
	case "peer-key":
		found, public, err := eng.PeerKey(command.Addr)
		if err != nil {
			return writeError(writer, err, proto.CodeInternal, command.RequestID)
		}
		return writeResponse(writer, proto.PeerKey(command.Addr, found, public), command.RequestID)
	default:
		return writer.Write(proto.Error(proto.CodeBadRequest, "unknown op"))
	}
}

func upConfig(up *proto.UpRequest) engine.UpConfig {
	cfg := engine.UpConfig{
		KeyFile:       up.KeyFile,
		Allow:         up.Allow,
		AllowAny:      up.AllowAny,
		InboundTarget: up.InboundTarget,
		PeerPort:      up.PeerPort,
		AddressFile:   up.AddressFile,
	}
	if up.DERP != nil {
		cfg.DERP = &engine.DERPOverride{
			Host:             up.DERP.Host,
			DERPPort:         up.DERP.DERPPort,
			STUNPort:         up.DERP.STUNPort,
			InsecureForTests: up.DERP.InsecureForTests,
		}
	}
	return cfg
}

func toPeerMappings(mappings []engine.Mapping) []proto.PeerMapping {
	peers := make([]proto.PeerMapping, 0, len(mappings))
	for _, mapping := range mappings {
		peers = append(peers, proto.PeerMapping{Peer: mapping.Peer, LocalPort: mapping.LocalPort})
	}
	return peers
}

// codeFor maps engine sentinel errors to protocol error codes; fallback
// applies to uncoded runtime failures (START_FAILED for up, INTERNAL for
// the other ops).
func codeFor(err error, fallback string) string {
	switch {
	case errors.Is(err, engine.ErrBadInput):
		return proto.CodeBadRequest
	case errors.Is(err, engine.ErrNotUp):
		return proto.CodeNotUp
	case errors.Is(err, engine.ErrAlreadyUp):
		return proto.CodeAlreadyUp
	case errors.Is(err, engine.ErrPeerUnknown):
		return proto.CodePeerUnknown
	case errors.Is(err, engine.ErrPortInUse):
		return proto.CodePortInUse
	case errors.Is(err, keys.ErrInvalid):
		return proto.CodeKeyFileInvalid
	default:
		return fallback
	}
}

// secretPattern matches the long base64-ish tokens that library error
// strings may embed (keys, addresses); such text never reaches an event.
var secretPattern = regexp.MustCompile(`[A-Za-z0-9+/_=-]{40,}`)

func writeError(writer *proto.Writer, err error, fallback, requestID string) error {
	message := secretPattern.ReplaceAllString(err.Error(), "[redacted]")
	runes := []rune(message)
	if len(runes) > 256 {
		runes = runes[:256]
	}
	return writeResponse(writer, proto.Error(codeFor(err, fallback), string(runes)), requestID)
}

func writeResponse(writer *proto.Writer, event proto.Event, requestID string) error {
	event.RequestID = requestID
	return writer.Write(event)
}

func scanLines(input io.Reader, lines chan<- []byte, scanErr chan<- error) {
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, 64*1024), maxRequestLine)
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		lines <- line
	}
	if err := scanner.Err(); err != nil {
		scanErr <- err
		return
	}
	scanErr <- nil
}
