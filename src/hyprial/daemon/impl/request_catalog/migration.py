"""Which method names have left the legacy string router.

Decision: daemon-first-z4b-router-only-refusal-20261117.  The complement is
recorded too: ``ROUTER_ONLY_METHODS`` lists the names the serving router still
answers from its own branches and that no declaration covers.  Both the
bootstrap dispatcher (to refuse them with ``ACCOUNT_REQUIRED``) and the serving
router (as its admission guard) read this one table; a name leaves it in the
commit that registers it or deletes its branch, and ``ping``,
``daemon.catalog`` and ``shutdown`` are never in it.  A test reads the
router's source and fails when the table and the branches disagree.

Migration is recorded per method name.  A family may hold both declared and
legacy-only members for a long time (``org`` and ``agent`` do), so the record
cannot be the family name: a partially migrated family that entered
``MIGRATED_FAMILIES`` would refuse every name it has not declared yet.
"""

from __future__ import annotations

from collections.abc import Iterable

from hyprial.kernel import MethodSpec

#: Precise record of the method names the catalog owns.  A name in this set is
#: served by a declaration and a binding or not at all: the router refuses it
#: with METHOD_NOT_FOUND instead of handing it to a compatibility branch.
MIGRATED_METHODS = frozenset(
    {
        'service.connect',
        'service.disconnect',
        'service.guide',
        'service.list',
        'adapter.identities.list',
        'adapter.identities.sync',
        'adapter.list',
        'management.adapter.remove',
        'management.adapter.remove.preview',
        'adapter.reload',
        'adapter.register',
        'adapter.onboard.begin',
        'adapter.onboard.operation',
        'adapter.onboard.cancel',
        'adapter.routes.list',
        'adapter.routes.candidates',
        'adapter.routes.add',
        'adapter.routes.remove',
        'adapter.routes.send',
        'adapter.authorization.status',
        'adapter.authorization.begin',
        'adapter.authorization.operation',
        'adapter.authorization.cancel',
        'adapter.start',
        'adapter.status',
        'adapter.stop',
        'agent.attach',
        'agent.cell.get',
        'agent.cell.update',
        'agent.create',
        'agent.detach',
        'agent.destroy',
        'agent.destroy.preview',
        'agent.home-census',
        'agent.grant.check',
        'agent.grant.visibility',
        'agent.harness-args.clear',
        'agent.harness-args.get',
        'agent.harness-args.set',
        'agent.harness-args.validate',
        'agent.list',
        'agent.resolve',
        'agent.provider.allow',
        'agent.provider.revoke',
        'agent.provider.status',
        'agent.provider.default',
        'agent.provider.login',
        'agent.provider.logout',
        'agent.provider.auth.list',
        'agent.rename',
        'agent.secret.grant',
        'agent.secret.list',
        'agent.secret.provider-write',
        'agent.secret.provider-list',
        'agent.secret.provider-configure',
        'agent.secret.provider-test',
        'agent.secret.revoke',
        'agent.start',
        'agent.stop',
        'autoupdate.status',
        'conversations.history',
        'conversations.list',
        'conversations.create',
        'conversations.get',
        'conversations.members.add',
        'conversations.members.remove',
        'conversations.post',
        'daemon.catalog',
        'daemon.status',
        'hosts',
        'identity.whoami',
        'identity.users.add',
        'identity.users.list',
        'log.page',
        'message.history',
        'message.pending.batch',
        'message.query',
        'message.send',
        'message.send.key_lookup',
        'message.send.lookup',
        'message.status',
        'network.expose',
        'network.exposures',
        'network.peerKey',
        'network.unexpose',
        'auth.login.begin',
        'auth.logout',
        'auth.operation.cancel',
        'auth.operation.get',
        'auth.relogin',
        'onboarding.device.ensure',
        'onboarding.firstRun.begin',
        'onboarding.firstRun.bind',
        'onboarding.firstRun.cancel',
        'onboarding.firstRun.confirm',
        'onboarding.firstRun.skip',
        'onboarding.initialize',
        'onboarding.status',
        'org.acl.grant',
        'org.acl.ls',
        'org.acl.revoke',
        'org.create',
        'org.delete',
        'org.execute',
        'org.fetch',
        'org.invite',
        'org.invites.accept',
        'org.invites.list',
        'org.join',
        'org.leave',
        'org.list',
        'org.network',
        'org.publish',
        'org.remove',
        'org.repair',
        'org.resolve',
        'org.use',
        'orgfs.checkout',
        'orgfs.create',
        'orgfs.export',
        'orgfs.history',
        'orgfs.import',
        'orgfs.invite',
        'orgfs.join',
        'orgfs.ls',
        'orgfs.members',
        'orgfs.mkdir',
        'orgfs.move',
        'orgfs.purge',
        'orgfs.purge_plan',
        'orgfs.purge_status',
        'orgfs.read',
        'orgfs.read_at',
        'orgfs.remove',
        'orgfs.remove_member',
        'orgfs.resolve',
        'orgfs.restore',
        'orgfs.serve',
        'orgfs.space_purge',
        'orgfs.spaces',
        'orgfs.stat',
        'orgfs.stat_at',
        'orgfs.status',
        'orgfs.trash',
        'orgfs.unban',
        'orgfs.watch',
        'orgfs.write',
        'outbox.list',
        'pac.gc.preview',
        'progress.list',
        'routine.audit',
        'routine.pause',
        'routine.resume',
        'routine.plan',
        'routine.list',
        'routine.status',
        'session.turn.refresh',
        'targets',
        'teleport.run',
        'top.inspect',
        'top.snapshot',
        'user.provider.list',
        'user.provider.login',
        'user.provider.logout',
        'user.provider.rotate',
        'userKey.renew',
        'userKey.revoke',
        'userKey.rotate',
        'userKey.sign',
        'userKey.status',
        'upgrade.steps.run',
        'upgrade.steps.status',
        'workflow.history.list',
        'workflow.list',
        'workflow.node.context',
    }
)

#: Families whose *every* member has migrated.  Only a family with no legacy
#: member left may enter here: once it does, even an undeclared name in that
#: family is refused rather than handled by the legacy chain.
MIGRATED_FAMILIES = frozenset({"daemon", "identity", "orgfs", "user", "userKey"})


ROUTER_ONLY_METHODS = frozenset(
    {
        'agent.config.rebind',
        'agent.ensure-home',
        'agent.get',
        'agent.grant',
        'agent.grants',
        'agent.migrate.execute',
        'agent.migrate.preflight',
        'agent.migrate.rollback',
        'agent.readiness',
        'agent.restore-policy',
        'agent.restore-threshold',
        'agent.resume.restore',
        'agent.revoke',
        'agent.runtime-context',
        'agent.runtime-launch.acquire',
        'agent.runtime-launch.custody',
        'agent.runtime-launch.release',
        'agent.start.preflight',
        'agent.unblock',
        'autoupdate.trigger',
        'dispatch.matrix.resolve',
        'down',
        'lifecycle.resume.preflight',
        'lifecycle.start',
        'message.ack',
        'message.pending.list',
        'message.pending.wait',
        'message.read_body',
        'message.reply',
        'message.settle_hint',
        'outbox.prune',
        'pac.actor.stop',
        'pac.flag.reset',
        'pac.flag.set',
        'pac.graph.activate',
        'pac.graph.close',
        'ps',
        'routine.add',
        'routine.remove',
        'routine.set',
        'session.heartbeat',
        'session.refresh',
        'session.register',
        'session.register_guarded',
        'session.turn.ended',
        'session.turn.started',
        'session.unregister',
        'worker.channel.request',
        'workflow.cancel',
        'workflow.complete',
        'workflow.expansion.plan',
        'workflow.fail',
        'workflow.history.status',
        'workflow.node.inspect',
        'workflow.plan',
        'workflow.remote.current',
        'workflow.start',
        'workflow.status',
        'workflow.worker.restart',
        'workflow.worker.stop',
    }
)


def validate_migration_coverage(
    specs: Iterable[MethodSpec],
    *,
    methods: Iterable[str] = MIGRATED_METHODS,
    families: Iterable[str] = MIGRATED_FAMILIES,
) -> None:
    """Refuse a declaration the migration record does not own.

    A binding without a marker would be served by the catalog while the
    router's refusal test still treats the name as legacy, so the two halves
    must agree before the registry freezes.
    """

    migrated_methods = frozenset(methods)
    migrated_families = frozenset(families)
    missing = sorted(
        spec.name
        for spec in specs
        if spec.name not in migrated_methods
        and spec.name.split(".", 1)[0] not in migrated_families
    )
    if missing:
        raise ValueError(
            "declared method(s) without a migration marker: " + ", ".join(missing)
        )
