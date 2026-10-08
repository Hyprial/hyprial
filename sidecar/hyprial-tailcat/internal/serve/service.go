package serve

import (
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/engine"
	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/proto"
)

func handleService(eng *engine.Engine, command proto.Command, writer *proto.Writer) error {
	switch command.Op {
	case "map-service":
		mapping, err := eng.MapService(engine.ServiceRequest{
			Name: command.Name, DeviceID: command.DeviceID, Address: command.Address,
			ServerPublic: command.ServerPublic, RemotePort: command.RemotePort,
			RecordGeneration: command.RecordGeneration, LocalPort: command.LocalPort,
		})
		if err != nil {
			return writeServiceError(writer, command, err)
		}
		return writer.Write(proto.ServiceMapped(command.RequestID, toServiceMapping(mapping)))
	case "unmap-service":
		removed, err := eng.UnmapService(command.Name)
		if err != nil {
			return writeServiceError(writer, command, err)
		}
		return writer.Write(proto.ServiceUnmapped(command.RequestID, command.Name, removed))
	case "service-status":
		mappings, err := eng.ServiceStatus()
		if err != nil {
			return writeServiceError(writer, command, err)
		}
		return writer.Write(proto.ServiceStatus(command.RequestID, toServiceMappings(mappings)))
	default:
		return writer.Write(proto.Error(proto.CodeBadRequest, "unknown op"))
	}
}

func toServiceMappings(mappings []engine.ServiceMapping) []proto.ServiceMapping {
	result := make([]proto.ServiceMapping, 0, len(mappings))
	for _, mapping := range mappings {
		result = append(result, toServiceMapping(mapping))
	}
	return result
}

func toServiceMapping(mapping engine.ServiceMapping) proto.ServiceMapping {
	return proto.ServiceMapping{
		Name: mapping.Name, DeviceID: mapping.DeviceID,
		RemotePort: mapping.RemotePort, RecordGeneration: mapping.RecordGeneration,
		LocalPort: mapping.LocalPort, ActiveConnections: mapping.ActiveConnections,
		Path: mapping.Path, PathDetail: mapping.PathDetail, ObservedAtMs: mapping.ObservedAtMs,
		LastDialState: mapping.LastDialState, LastDialMs: mapping.LastDialMs,
		LastError: mapping.LastError, LastErrorAtMs: mapping.LastErrorAtMs,
	}
}

func writeServiceError(writer *proto.Writer, command proto.Command, err error) error {
	var name *string
	if command.Op != "service-status" {
		name = &command.Name
	}
	message := "service operation failed"
	if codeFor(err, proto.CodeInternal) == proto.CodeBadRequest {
		message = "invalid service request"
	}
	return writer.Write(proto.ServiceFailed(
		command.RequestID, command.Op, name, codeFor(err, proto.CodeInternal), message,
	))
}
