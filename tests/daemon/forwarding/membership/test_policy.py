from types import SimpleNamespace

from hyprial.daemon.impl.orgfs.mesh import OrgFsMesh
from hyprial.daemon.impl.orgfs.network.ingress import MeshIngress


def test_transitional_supplier_gate_does_not_read_directory():
    mesh = object.__new__(OrgFsMesh)
    mesh._trust_tailnet_members = True
    mesh._supplier_owner = lambda _: (_ for _ in ()).throw(AssertionError('directory consulted'))
    assert mesh._supplier_member_allowed('peer')


def test_unknown_supplier_is_not_a_second_bootstrap_exception():
    mesh = object.__new__(OrgFsMesh)
    mesh._trust_tailnet_members = False
    mesh._supplier_owner = lambda _: None
    assert not mesh._supplier_member_allowed('peer')


def test_transitional_requester_keeps_format_checks():
    mesh = SimpleNamespace(_trust_tailnet_members=True,
                           _member_mode=lambda _: None)
    assert MeshIngress._requester_allowed(mesh, {'requester': {'author': 'user:alice'}})
    assert not MeshIngress._requester_allowed(mesh, {'requester': {'author': 1}})
