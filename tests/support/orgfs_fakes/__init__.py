"""OrgFS and clock/log test doubles shared by tests beyond the send-link suite.

Decision: T0 zero-behaviour move 2026-11-19 (send-link/thread removal plan).
``BlockPoint``, ``FakeClock``, ``LogRecorder``, ``FakeOrgFs`` (org.py) and
``OrgFsWithMeta``, ``stub_app``, ``join_test_org`` (app.py) moved verbatim from
``tests/support/links/``, which still imports them from here until it is
deleted with the links package (plan T1c).
"""
