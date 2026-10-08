"""Keep business saga commands and translate only their port failures."""

from hyprial.biz import (
    AgentCreationCompensation,
    RoutineCoordinator,
    RoutineCoordinatorError,
    RoutineCoordinatorTimeout,
    add_command,
    remove_command,
)
from hyprial.daemon import RoutinePortError, RoutinePortTimeout


class RoutineCoordinatorAdapter:
    def __init__(self, coordinator: RoutineCoordinator) -> None:
        self.coordinator = coordinator

    @staticmethod
    def _call(operation, *arguments, **keywords):
        try:
            return operation(*arguments, **keywords)
        except RoutineCoordinatorTimeout as error:
            raise RoutinePortTimeout(error.operation_id) from error
        except RoutineCoordinatorError as error:
            raise RoutinePortError(error.code, str(error), error.data) from error

    def stats(self):
        return self._call(self.coordinator.stats)

    def begin_add(self, *, operation_id, name, yaml_text, owner, produces, agent_compensation=None):
        compensation = None if agent_compensation is None else AgentCreationCompensation(
            actor=agent_compensation["actor"],
            expected_entity_token=agent_compensation["expected_entity_token"],
            settlement_id=agent_compensation["settlement_id"],
        )
        command = add_command(
            operation_id=operation_id, name=name, yaml_text=yaml_text,
            owner=owner, produces=produces, agent_compensation=compensation,
        )
        return self._call(self.coordinator.begin, command)

    def begin_remove(self, *, operation_id, name, enforce_last):
        command = remove_command(operation_id=operation_id, name=name, enforce_last=enforce_last)
        return self._call(self.coordinator.begin, command)

    def wait(self, operation_id, *, timeout):
        return self._call(self.coordinator.wait, operation_id, timeout=timeout)

    def close(self, timeout=5.0):
        return self._call(self.coordinator.close, timeout=timeout)
