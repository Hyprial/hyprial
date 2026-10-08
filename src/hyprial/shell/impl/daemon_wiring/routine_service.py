"""Translate business routine failures at the explicit daemon port."""

from hyprial.biz import RoutineService, RoutineServiceError
from hyprial.daemon import RoutinePortError
from hyprial.kernel import PortAdmission


class RoutineServiceAdapter:
    def __init__(self, service: RoutineService) -> None:
        self.service = service

    @staticmethod
    def _call(operation, **arguments):
        try:
            return operation(**arguments)
        except RoutineServiceError as error:
            raise RoutinePortError(error.code, str(error), error.data) from error

    @property
    def migrated_u3(self):
        try:
            return self.service.migrated_u3
        except RoutineServiceError as error:
            raise RoutinePortError(error.code, str(error), error.data) from error

    def recover(self):
        return self._call(self.service.recover)

    def submit_timer(self, observed_at_ms: int | None = None) -> PortAdmission:
        return self._call(self.service.submit_timer, observed_at_ms=observed_at_ms)

    def close(self):
        # The real service returns None; do not manufacture a bool receipt.
        return self._call(self.service.close)

    def list(self):
        return self._call(self.service.list)

    def status(self, *, name):
        return self._call(self.service.status, name=name)

    def add(self, *, yaml_text, owner, enabled):
        return self._call(self.service.add, yaml_text=yaml_text, owner=owner, enabled=enabled)

    def remove(self, *, name, reservation_id=None):
        return self._call(self.service.remove, name=name, reservation_id=reservation_id)

    def reserve_remove(self, *, name, reservation_id, enforce_last):
        return self._call(self.service.reserve_remove, name=name, reservation_id=reservation_id, enforce_last=enforce_last)

    def cancel_remove(self, *, name, reservation_id):
        return self._call(self.service.cancel_remove, name=name, reservation_id=reservation_id)

    def pause(self, *, name):
        return self._call(self.service.pause, name=name)

    def resume(self, *, name, align_schedule=False):
        return self._call(self.service.resume, name=name, align_schedule=align_schedule)

    def set(self, *, name, yaml_text):
        return self._call(self.service.set, name=name, yaml_text=yaml_text)

    def address_migrations(self):
        return self._call(self.service.address_migrations)
