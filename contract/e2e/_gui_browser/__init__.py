"""Shared pieces for E2E scenarios that start ``hyprial gui`` and drive a browser.

Decision record (E2E-032, 2026-11-21):
- ``session`` owns the processes: a GUI-ready throwaway home prepared by
  ``tests.support.gui_home``, the real daemon as a plain child of the runner,
  ``hyprial gui --no-open --json --port 0 --stdin-stop`` as a second child,
  and one Node browser script as a third. Every pid it acts on is one it
  started or one that a child reported about itself.
- ``page.mjs`` owns the browser: Chromium from ``gui/tests/support/browser.mjs``
  driven by the ``playwright-core`` that ``gui/package.json`` locks, the
  one-time handoff read from the private ``launch.url`` file, and a request
  recorder over ``page.on('request'|'response')`` for ``/daemon/request/*``.
  Nothing is answered for the page (no ``route``/``fulfill``) and the product
  gets no test hook.
- The browser part is Node because the repository has no Python browser
  driver and must not grow one; the runner part is Python like every other
  scenario under ``contract/e2e``.
- Not a framework: the scenario-specific steps live in the scenario's own
  package, this package holds only what a second browser scenario would copy.
"""
