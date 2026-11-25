"""Construct the real business routine authorities from daemon inputs."""

from hyprial.biz import (
    PacRoutineDispatch,
    RoutineCoordinator,
    RoutineSchemaError,
    RoutineService,
    RoutineStore,
    load_routine_text,
    migrate_routine_agent_uris,
    render_template,
)
from hyprial.daemon import RoutineRuntime, RoutineRuntimeDeps, RoutineSchemaPortError
from hyprial.kernel import ipc_errors

from .routine_coordinator import RoutineCoordinatorAdapter
from .routine_service import RoutineServiceAdapter


def routine_text_loader(text, *, label="routine"):
    try:
        return load_routine_text(text, label=label)
    except RoutineSchemaError as error:
        raise RoutineSchemaPortError(ipc_errors.ROUTINE_SCHEMA_ERROR, str(error)) from error


def routine_template_renderer(template_name, **substitutions):
    return render_template(template_name, **substitutions)


def migrate_stored_routine_agent_uris(deps: RoutineRuntimeDeps) -> int | None:
    """Run the routine store's one-time UUID-addressing P4 migration.

    Only when the daemon supplied the id-form mapping, and before the service
    opens the store.  The rewrite count is logged under the same event the
    daemon logs for its own stores.  A failure follows the daemon stores'
    policy: it is logged as ``agent_uri.store_migration_failed``, leaves the
    marker unwritten (so the next start retries) and never stops the routine
    runtime from opening.
    """

    if deps.stored_agent_uri is None:
        return None
    try:
        rewritten = migrate_routine_agent_uris(
            deps.state_dir / "routines.sqlite3", deps.stored_agent_uri,
            at_ms=deps.clock_ms(),
        )
    except Exception as error:  # noqa: BLE001 - retried at the next start
        if deps.logger is not None:
            deps.logger.log(
                "error", "agent_uri.store_migration_failed", store="routines",
                error=f"{type(error).__name__}: {error}"[:500],
            )
        return None
    if rewritten is not None and deps.logger is not None:
        deps.logger.log("info", "agent_uri.stores_migrated", stores={"routines": rewritten})
    return rewritten


def routine_runtime_factory(deps: RoutineRuntimeDeps):
    if deps.workflow_service is None:
        return None
    migrate_stored_routine_agent_uris(deps)
    dispatch = PacRoutineDispatch(
        state_dir=deps.state_dir, deliver=deps.deliver_task,
        clock_ms=deps.clock_ms, resolve_principal=deps.resolve_principal,
        workflow=deps.workflow_service, graph_authority=deps.graph_authority,
        resolve_routine_target=deps.resolve_routine_target,
    )
    service = RoutineService(
        pac=dispatch, alarm=deps.alarm, state_dir=deps.state_dir,
        clock_ms=deps.clock_ms, migrate_address=deps.migrate_address,
        logger=deps.logger, binding_key=deps.binding_key,
        actor_host=deps.actor_host,
    )
    try:
        coordinator = None if deps.graph_authority is None else RoutineCoordinator(
            state_dir=deps.state_dir,
            # Coordinator's business protocol includes registration_id and
            # enforce_last. It must receive the real service, not its port.
            routines=service, ensure_coordinator=deps.ensure_coordinator,
            retire_coordinator=deps.retire_coordinator, close_graph=deps.close_graph,
            compensate_agent=deps.compensate_agent, clock_ms=deps.clock_ms,
            binding_key=deps.binding_key, actor_host=deps.actor_host,
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
