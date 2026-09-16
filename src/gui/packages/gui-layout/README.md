# DSH GUI layout provider

This package replaces the **active** `ui-layout` profile contribution at boot.
Do not register it beside the upstream layout or hot-replace a running root.
The installer disables the upstream row and inserts this package before the
browser starts. Native sidebar, conversation, details and overlay contributions
retain their original names, scopes, owners and React tree positions.

`ctx.layout` retains `toggleSidebar`, `openDetails` and `closeDetails`, and adds:

```js
layout.workspaceVersion === 1
layout.configureWorkspace({
  navigation: 'left', // or 'top'
  nativeSidebar: true, // false hides the native rail without unmounting its contents
  sidebarWidth: 280, // 200..480
  detailsWidth: 360, // 240..640, applies when details is open
  workspaceRatio: 40, // 20..80 percent, used by split view
  theme: { mode: 'system', accent: '#4575dc', density: 'comfortable' }
});
layout.setWorkspaceVisible(false); // native conversation
layout.setWorkspaceVisible(true); // custom page
layout.setWorkspaceVisible('split'); // native + custom page
layout.resetWorkspace(); // prior native geometry and theme
```

The persistent module runtime can place the original conversation alongside
other whole modules without changing its slot owner:

```js
layout.moduleSurfaceVersion === 1
layout.setWorkspaceVisible(true);
layout.setNativeSurfaceRect({ x: 340, y: 100, width: 600, height: 700, visible: true });
layout.setNativeSurfaceRect(null); // return to the ordinary visibility/layout
```

Rectangles use viewport coordinates, measured from an empty authored placeholder.
The frame converts them to its owned coordinate space. Optional bounded
`clip: {top,right,bottom,left}` offsets clip nested scrolling ancestors without
resizing or remounting the conversation. Offscreen rectangles are hidden. The
same `CenterColumn` and native slot stay at their original React tree position.
`resetWorkspace()` also clears any placement. This is a placement contract only;
the caller still selects the native session through DSH's normal session service.

`workspace` and `workspace.navigation` are additive single/root-scoped slots.
Their owner props are `{configuration, visible}`. They stay mounted when hidden.
The navigation slot is visible only for top navigation; left navigation uses
the retained native sidebar's application rail, avoiding duplicate menus.
Native conversation is always rendered at its original tree position, including
when the custom page occupies the center. Split view shares the center 60/40 by
default; below 640 pixels of center space it shows the custom page, and the
protected native entry switches back to the intact conversation. The original
responsive sidebar and details concession behavior remains active.

Theme mode resolves registered light/dark definitions through the native theme
service and presenter. It does not persist global theme settings during trial.
Only a validated hex accent may override the fixed accent token. Density is
exposed on the owned frame as `data-gui-density`; feature modules decide their
supported density presentation. No Agent HTML, code, selectors or token names
are executed.

The upstream 0.1.1-rc.2 distributed browser bundle is vendored verbatim at
`vendor/dsh-ui-layout-rc.2.js`, with its MIT license and SHA256SUMS. It contains
the native drag/concession/store/theme behavior. `build.mjs` checks the pin and
applies uniquely matched, reviewed changes plus `workspace-extension.js`.
`client.js` is generated; do not edit it. No external build dependencies needed.

The split runtime used by npm latest is also pinned from
`@deepseek-ai/dsh-client-ui-layout@0.1.5-rc.2` in
`vendor/dsh-ui-layout-0.1.5-rc.2.js` (the CLI latest's caret dependency resolves
to this UI version). `build-modern.mjs` verifies its SHA-256 and adds workspace
seats while retaining keyed `main` panels, rightbar state and navigation.
The generated factory selects the legacy implementation only when its runtime
module exists; otherwise it uses the split `dsh-client-store` implementation.
Both use the same validated workspace controller and own exactly one root.

```sh
node packages/gui-layout/build.mjs
node packages/gui-layout/build.mjs --check
node --test tests/gui-layout.test.mjs
```

Upstream source: https://github.com/deepseek-ai/deepseek-harness/tree/main/packages/client/ui-layout

Full business modules rendered inside a custom page still own their behavior.
This package does not relocate native session/input/tool/subagent components
into arbitrary layout nodes or change any business record.

Workspace themes also accept the bounded shared `shared/gui-style.mjs` contract:
semantic light/dark palettes, named presets/fonts, and numeric typography,
spacing, radius and border controls. New presentation fields project the shared
variables and a fixed set of native background/foreground aliases through the
existing theme presenter. Legacy mode/accent/density-only declarations preserve
the previous appearance. Error, success, warning and inverted tooltip tokens are
not rewritten; reset removes all transient theme overrides.

`layout.setNativePresentation({ theme, mode: 'light' | 'dark', appearance? })` applies a
validated page theme only to the existing native conversation container.
`setNativePresentation(null)` clears it; `resetWorkspace()` also clears it.
Optional appearance uses the same bounded node contract; `appearanceChain` instead accepts up to 14 ancestor-to-node declarations, resolved in order. These forms are mutually exclusive; authored wrappers own padding, so it is not applied twice.
This API accepts no arbitrary CSS or token names and does not replace the native
owner, alter its rectangle, or save the global theme preference. Callers resolve
system mode and clear the local presentation when leaving the themed page.
