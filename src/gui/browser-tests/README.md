# GUI browser verification tools

Locked development tools shared by the DSH release gate, GUI Studio/editor and
module browser regressions. This directory is not an application or server.

Prepare with `node scripts/install-browser-deps.mjs` from `src/gui`; this checks
the rolldown native binding after `npm ci`. `scripts/ci-browser-install.mjs`
prepares the pinned Playwright Chromium. CI uses the `gui-browser` job.
User installation currently prepares these tools for its existing release gate;
removing install-time build/test is a separate desktop packaging change.
