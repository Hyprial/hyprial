"""Patch only the selected member intent through the existing Cell CAS service."""

from hyprial.error import codes
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.cells.model import thaw_json
from hyprial.daemon.impl.cells.structure import parse_structure
from hyprial.daemon.impl.application.actors.cell_admin.service import (
    _is_user_agent_cell, _require_agent_cell, update_agent_cell,
)


def refuse(code, reason, phase="bind"):
    raise DaemonRequestError(code, f"{code}: {phase}/{reason}",
                             data={"phase": phase, "reason": reason})


class CellIntent:
    def __init__(self, app, agent, member):
        self.app, self.agent, self.member = app, agent, member
        self.cell_id = agent.cell_id

    def read(self):
        try:
            current_agent = self.app.agents.get(self.agent.actor)
            if current_agent is None or current_agent.agent_id != self.agent.agent_id:
                refuse(codes.MODEL_CONFIGURATION_CONFLICT, "agent-incarnation-changed")
            cell = _require_agent_cell(self.app._cell_runtime, current_agent)
            structure = parse_structure(cell)
            if _is_user_agent_cell(current_agent, cell):
                raise DaemonRequestError(codes.USER_AGENT_IMMUTABLE,
                                         "The user agent has no configurable model binding.")
            if structure.problems or not any(
                member.name == self.member and member.role == "harness"
                for member in structure.members
            ):
                refuse(codes.MODEL_CONFIGURATION_CONFLICT, "cell-intent-mismatch")
            raw = thaw_json(cell.params).get("cell.members", {}).get(self.member, {})
            return cell, {key: raw[key] for key in ("kind", "model") if key in raw}
        except DaemonRequestError:
            raise
        except Exception:
            refuse(codes.MODEL_CONFIGURATION_INCOMPLETE, "cell-outcome-unknown")

    def write(self, current, intent):
        params = thaw_json(current.params)
        member = params.setdefault("cell.members", {}).setdefault(self.member, {})
        for key in ("kind", "model"):
            member.pop(key, None)
        member.update(intent)
        try:
            update_agent_cell(self.app, {
                "name": self.agent.actor,
                "expectedRevision": current.revision,
                "params": params,
            }, expected_agent_id=self.agent.agent_id)
        except DaemonRequestError as error:
            if error.code == codes.INVALID_ARGUMENT and error.data and "current" in error.data:
                refuse(codes.MODEL_CONFIGURATION_CONFLICT, "cell-revision-mismatch")
            raise
        except Exception:
            refuse(codes.MODEL_CONFIGURATION_INCOMPLETE, "cell-outcome-unknown")
