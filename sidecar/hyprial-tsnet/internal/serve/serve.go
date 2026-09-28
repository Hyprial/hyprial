// Package serve implements the protocol-v1 stdio process lifecycle.
package serve

import (
	"bufio"
	"context"
	"io"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tsnet/internal/buildinfo"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tsnet/internal/node"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tsnet/internal/proto"
)

const maxRequestLine = 1024 * 1024

func Run(ctx context.Context, input io.Reader, output io.Writer, factory node.BackendFactory) error {
	writer := proto.NewWriter(output)
	if err := writer.Write(proto.Hello(buildinfo.SidecarVersion, buildinfo.TailscaleVersion)); err != nil {
		return err
	}

	fatal := make(chan node.Failure, 1)
	manager := node.NewManager(factory, writer.Write, func(failure node.Failure) {
		select {
		case fatal <- failure:
		default:
		}
	})

	lines := make(chan []byte)
	scanErr := make(chan error, 1)
	go scanLines(input, lines, scanErr)

	fail := func(code, message string) error {
		manager.Stop()
		if err := writer.Write(proto.Error(code, message)); err != nil {
			return err
		}
		return writer.Write(proto.Exited())
	}

	for {
		select {
		case <-ctx.Done():
			manager.Stop()
			_ = writer.Write(proto.Exited())
			return ctx.Err()
		case failure := <-fatal:
			// fail() already wrote the `error` event before its slow backend
			// Close (card 6cdcc406); Emitted pins that so this branch never
			// writes a second one — it only writes `exited`.
			if !failure.Emitted {
				if err := writer.Write(proto.Error(failure.Code, failure.Message)); err != nil {
					return err
				}
			}
			return writer.Write(proto.Exited())
		case err := <-scanErr:
			if err == nil {
				manager.Stop()
				return writer.Write(proto.Exited())
			}
			return fail("PROTOCOL", "invalid JSON-lines input")
		case line := <-lines:
			command, err := proto.ParseCommand(line)
			if err != nil {
				return fail("PROTOCOL", "invalid protocol-v1 request")
			}
			switch command.Op {
			case "up":
				if err := node.SetRequestProxy(command.Up.Proxy); err != nil {
					return fail(node.CodeControlUnreachable, "cannot configure request proxy")
				}
				if err := manager.Start(*command.Up); err != nil {
					if code, message, ok := node.ErrorDetails(err); ok {
						return fail(code, message)
					}
					return fail("PROTOCOL", "invalid up lifecycle")
				}
			case "status":
				event, err := manager.Status(ctx)
				if err != nil {
					if code, message, ok := node.ErrorDetails(err); ok {
						return fail(code, message)
					}
					return fail("PROTOCOL", "status before up")
				}
				if err := writer.Write(event); err != nil {
					manager.Stop()
					return err
				}
			case "down":
				manager.Stop()
				return writer.Write(proto.Exited())
			}
		}
	}
}

// RunForwarding is the protocol-v2 stdio lifecycle. Login keeps Run's closed
// v1 contract; forwarding adds map-peer/unmap-peer and peer-to-local-port
// events without pretending old clients can understand them.
func RunForwarding(ctx context.Context, input io.Reader, output io.Writer, factory node.BackendFactory) error {
	writer := proto.NewWriter(output)
	if err := writer.Write(proto.ForwardHello(buildinfo.SidecarVersion, buildinfo.TailscaleVersion)); err != nil {
		return err
	}

	fatal := make(chan node.Failure, 1)
	manager := node.NewManager(factory, writer.Write, func(failure node.Failure) {
		select {
		case fatal <- failure:
		default:
		}
	})
	lines := make(chan []byte)
	scanErr := make(chan error, 1)
	go scanLines(input, lines, scanErr)

	fail := func(code, message string) error {
		manager.Stop()
		if err := writer.Write(proto.ForwardError(code, message)); err != nil {
			return err
		}
		return writer.Write(proto.ForwardExited())
	}
	writeResult := func(event proto.Event, err error) error {
		if err != nil {
			if code, message, ok := node.ErrorDetails(err); ok {
				return fail(code, message)
			}
			return fail("PROTOCOL", "invalid forwarding lifecycle")
		}
		return writer.Write(event)
	}

	for {
		select {
		case <-ctx.Done():
			manager.Stop()
			_ = writer.Write(proto.ForwardExited())
			return ctx.Err()
		case failure := <-fatal:
			if !failure.Emitted {
				if err := writer.Write(proto.ForwardError(failure.Code, failure.Message)); err != nil {
					return err
				}
			}
			return writer.Write(proto.ForwardExited())
		case err := <-scanErr:
			if err == nil {
				manager.Stop()
				return writer.Write(proto.ForwardExited())
			}
			return fail("PROTOCOL", "invalid JSON-lines input")
		case line := <-lines:
			command, err := proto.ParseForwardCommand(line)
			if err != nil {
				return fail("PROTOCOL", "invalid protocol-v2 request")
			}
			switch command.Op {
			case "up":
				if err := node.SetRequestProxy(command.Up.Proxy); err != nil {
					return fail(node.CodeControlUnreachable, "cannot configure request proxy")
				}
				if err := manager.Start(*command.Up); err != nil {
					if code, message, ok := node.ErrorDetails(err); ok {
						return fail(code, message)
					}
					return fail("PROTOCOL", "invalid forwarding up lifecycle")
				}
			case "status":
				if err := writeResult(manager.ForwardStatus(ctx)); err != nil {
					return err
				}
			case "map-peer":
				if err := writeResult(manager.MapPeer(command.Peer)); err != nil {
					return err
				}
			case "unmap-peer":
				if err := writeResult(manager.UnmapPeer(command.Peer)); err != nil {
					return err
				}
			case "down":
				manager.Stop()
				return writer.Write(proto.ForwardExited())
			}
		}
	}
}

func scanLines(input io.Reader, lines chan<- []byte, result chan<- error) {
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, 4096), maxRequestLine)
	for scanner.Scan() {
		line := append([]byte(nil), scanner.Bytes()...)
		lines <- line
	}
	result <- scanner.Err()
}
