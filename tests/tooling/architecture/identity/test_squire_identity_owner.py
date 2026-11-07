"""The daemon owns profile admission, identity owns the canonical profile store."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]


def test_profile_types_have_one_identity_owner():
    from hyprial import identity
    from hyprial.identity.impl.users import profile
    for name in ('NotificationRule', 'OwnerOpenId', 'PreferredReceiver', 'RuntimeCapability',
                 'UserProfile', 'UserProfileError', 'UserProfileStore'):
        assert getattr(identity, name) is getattr(profile, name)
    assert not (ROOT / 'src/hyprial/daemon/impl/squire/profile.py').exists()


def test_profile_actor_stores_and_reads_the_canonical_identity_model(tmp_path):
    from hyprial.identity import UserProfile, UserProfileStore
    from hyprial.daemon.impl.squire.actors.profile import UserProfileAuthority
    actor = UserProfileAuthority(tmp_path / 'users.json')
    try:
        assert type(actor._store) is UserProfileStore
        result, _ = actor.ensure(owner='alice', owner_key='owner-a', login_name='alice',
                                 machine='fixture-host', machine_key='machine-a')
        assert type(result) is UserProfile
        assert UserProfileStore(actor.path).get('owner-a') == result
    finally:
        actor.close()


def test_probe_command_has_one_canonical_kernel_binding():
    from hyprial.kernel import build_probe_command
    from hyprial.daemon.impl.squire import probe
    assert probe.build_probe_command is build_probe_command
