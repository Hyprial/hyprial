"""Construct the real business routine authorities from daemon inputs."""

from hyprial.biz import (
    PacRoutineDispatch,
    RoutineCoordinator,
    RoutineSchemaError,
    RoutineService,
    RoutineStore,
    load_routine_text,
    render_template,
)
from hyprial.daemon import RoutineRuntime, RoutineRuntimeDeps, RoutineSchemaPortError

from .routine_coordinator import RoutineCoordinatorAdapter
from .routine_service import RoutineServiceAdapter


def routine_text_loader(text):
    try:
        return load_routine_text(text)
    except RoutineSchemaError as error:
        raise RoutineSchemaPortError("ROUTINE_SCHEMA_ERROR", str(error)) from error


def routine_template_renderer(template_name, **substitutions):
    return render_template(template_name, **substitutions)


def routine_runtime_factory(deps: RoutineRuntimeDeps):
    if deps.workflow_service is None:
        return None
    dispatch = PacRoutineDispatch(
        state_dir=deps.state_dir, deliver=deps.deliver_task,
        clock_ms=deps.clock_ms, resolve_principal=deps.resolve_principal,
        workflow=deps.workflow_service, graph_authority=deps.graph_authority,
    )
    service = RoutineService(
        pac=dispatch, alarm=deps.alarm, state_dir=deps.state_dir,
        clock_ms=deps.clock_ms, migrate_address=deps.migrate_address,
        logger=deps.logger,
    )
    try:
        coordinator = None if deps.graph_authority is None else RoutineCoordinator(
            state_dir=deps.state_dir,
            # Coordinator's business protocol includes registration_id and
            # enforce_last. It must receive the real service, not its port.
            routines=service, ensure_coordinator=deps.ensure_coordinator,
            retire_coordinator=deps.retire_coordinator, close_graph=deps.close_graph,
            compensate_agent=deps.compensate_agent, clock_ms=deps.clock_ms,
        )
    except BaseException:
        # The daemon cannot close a service whose factory never returned.
        # Cleanup must not replace the original construction failure.
        try:
            service.close()
        except BaseException:
            pass
        raise

    def routine_exists(name):
        store = RoutineStore(deps.state_dir / "routines.sqlite3")
        try:
            return store.get_routine(name) is not None
        finally:
            store.close()

    return RoutineRuntime(
        service=RoutineServiceAdapter(service),
        coordinator=None if coordinator is None else RoutineCoordinatorAdapter(coordinator),
        routine_exists=routine_exists,
    )
