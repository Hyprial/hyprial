import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { modernFactory } from './build-modern.mjs';

const root = new URL('./', import.meta.url);
let source = await readFile(new URL('vendor/dsh-ui-layout-rc.2.js', root), 'utf8');
const originalHash = createHash('sha256').update(source).digest('hex');
const expectedHash = (await readFile(new URL('vendor/SHA256SUMS', root), 'utf8')).trim().split(/\s+/)[0];
if (originalHash !== expectedHash) throw new Error('Vendored DSH layout changed; audit and repin before building');
function replace(before, after) {
  if (source.split(before).length !== 2) throw new Error('DSH layout anchor missing or ambiguous: ' + before.slice(0, 100));
  source = source.replace(before, after);
}

replace('id: "@deepseek-ai/dsh-client-ui-layout"', 'id: "@hyprial/dsh-gui-layout"');
// Keep upstream CSS self-contained, but give this provider its own ownership id.
source = source.replaceAll('@deepseek-ai/dsh-client-ui-layout/AppFrame.module.css', '@hyprial/dsh-gui-layout/AppFrame.module.css');
replace('tag.dataset.plugin = "@deepseek-ai/dsh-client-ui-layout"', 'tag.dataset.plugin = "@hyprial/dsh-gui-layout"');
replace('function computeColumns(viewport, sidebar, details) {', 'function computeColumns(viewport, sidebar, details, custom = false, hideSidebar = false) {');
replace('const s = sidebar === 0 ? 56 :', 'const s = hideSidebar ? 0 : sidebar === 0 ? 56 :');
replace('clampWidth(sidebar, 264, 420)', 'clampWidth(sidebar, custom ? 200 : 264, custom ? 480 : 420)');
replace('clampWidth(details, 300, 520)', 'clampWidth(details, custom ? 240 : 300, custom ? 640 : 520)');
replace('Math.max(300, viewport - s - 640)', 'Math.max(custom ? 240 : 300, viewport - s - 640)');
replace('className: AppFrame_module_css_default.centerCol,\n\t\t\t\tchildren: props.children', 'className: AppFrame_module_css_default.centerCol,\n                style: props.style, "data-native-conversation": true, "data-gui-styled": props.style?.["--gui-style-background"] ? "true" : undefined, hidden: props.hidden,\n                children: props.children');
replace('className: AppFrame_module_css_default.detailsCol,\n\t\t\t\tchildren: props.children', 'className: AppFrame_module_css_default.detailsCol,\n                style: props.style,\n                children: props.children');
replace('const panels = useStore((s) => s);', `const panels = useStore((s) => s);
            const configuration = panels.workspaceConfiguration;
            const workspaceVisible = panels.workspaceVisible;
            const hasNavigation = configuration !== null && configuration.navigation === 'top';
            const topNavigation = hasNavigation;
            const row = topNavigation ? 2 : 1;`);
replace('const sidebarCollapsed = narrow ? !panels.narrowExpanded : panels.sidebar === 0;', `const objectPanel = workspaceObjectPanel(configuration, viewport);
            const sidebarHidden = objectPanel ? !objectPanel.open : configuration?.nativeSidebar === false;
            const sidebarTrackHidden = objectPanel ? !objectPanel.open || objectPanel.mode === 'drawer' : sidebarHidden;
            const sidebarCollapsed = objectPanel ? false : sidebarHidden || (narrow ? !panels.narrowExpanded : panels.sidebar === 0);`);
replace('detailsSession === void 0 ? 0 : panels.details);', 'detailsSession === void 0 ? 0 : panels.details, configuration !== null, sidebarTrackHidden);');
replace('sidebarCollapsed ? 0 : panels.sidebar === 0 ? 280 : panels.sidebar,', 'objectPanel ? objectPanel.width : sidebarCollapsed ? 0 : panels.sidebar === 0 ? 280 : panels.sidebar,');
replace('width: cols.sidebar', 'width: objectPanel ? objectPanel.width : cols.sidebar');
replace('!sidebarCollapsed && (0, react_jsx_runtime.jsx)(DragHandle,', '!objectPanel && !sidebarCollapsed && (0, react_jsx_runtime.jsx)(DragHandle,');
replace('const colsRef = (0, react.useRef)(cols);', `const split = workspaceVisible === 'split' && cols.center >= 640;
            const workspaceFraction = (configuration?.workspaceRatio ?? 40) / 100;
            const canvasWidth = split ? cols.center * workspaceFraction : cols.center;
            const nativeWidth = split ? cols.center - canvasWidth : cols.center;
            const requestedNative = panels.nativeSurfaceRect;
            const frameBox = frameRef.current?.getBoundingClientRect() || { left: 0, top: 0, width: viewport, height: window.innerHeight };
            const nativeX = requestedNative ? Math.max(0, requestedNative.x - frameBox.left + (requestedNative.clip?.left || 0)) : 0;
            const nativeY = requestedNative ? Math.max(0, requestedNative.y - frameBox.top + (requestedNative.clip?.top || 0)) : 0;
            const nativeRight = requestedNative ? Math.min(frameBox.width, requestedNative.x + requestedNative.width - frameBox.left - (requestedNative.clip?.right || 0)) : 0;
            const nativeBottom = requestedNative ? Math.min(frameBox.height, requestedNative.y + requestedNative.height - frameBox.top - (requestedNative.clip?.bottom || 0)) : 0;
            const placedNativeVisible = requestedNative && requestedNative.visible && nativeRight > nativeX && nativeBottom > nativeY;
            const nativeHidden = requestedNative ? !placedNativeVisible : Boolean(workspaceVisible && !split);
            const nativeClip = requestedNative ? [
                Math.max(0, nativeY - (requestedNative.y - frameBox.top)),
                Math.max(0, requestedNative.x + requestedNative.width - frameBox.left - nativeRight),
                Math.max(0, requestedNative.y + requestedNative.height - frameBox.top - nativeBottom),
                Math.max(0, nativeX - (requestedNative.x - frameBox.left))].map(value => value + 'px').join(' ') : '';
            const nativeStyle = requestedNative ? { position: 'absolute', left: requestedNative.x - frameBox.left, top: requestedNative.y - frameBox.top,
                width: requestedNative.width, height: requestedNative.height, zIndex: 4, clipPath: 'inset(' + nativeClip + ')',
                minHeight: 0, display: nativeHidden ? 'none' : undefined } : {
                gridColumn: 2, gridRow: row, minHeight: 0, width: split ? nativeWidth : undefined,
                display: nativeHidden ? 'none' : undefined };
            Object.assign(nativeStyle, panels.nativePresentation || {});
            const colsRef = (0, react.useRef)(cols);`);
replace('style: { gridTemplateColumns: `${cols.sidebar}px minmax(0, 1fr) ${cols.details}px` },', `style: { gridTemplateColumns: \`\${cols.sidebar}px minmax(0, 1fr) \${cols.details}px\`, gridTemplateRows: topNavigation ? '48px minmax(0,1fr)' : '100%' },
                "data-gui-layout": "1", "data-gui-surface": split ? 'split' : workspaceVisible ? 'page' : 'native',
                "data-gui-navigation": configuration?.navigation, "data-gui-styled": workspaceHasStyle(configuration?.theme) ? "true" : undefined,
                "data-gui-density": configuration?.theme?.density,`);
replace('className: AppFrame_module_css_default.sidebarCol,\n\t\t\t\t\t\tchildren:', `className: AppFrame_module_css_default.sidebarCol,
                        "data-native-sidebar": true, hidden: sidebarHidden, "data-gui-object-panel": objectPanel ? objectPanel.open ? objectPanel.mode : 'closed' : undefined,
                        style: workspaceSidebarStyle(configuration, objectPanel, row),
                        children:`);
replace('(0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [(0, react_jsx_runtime.jsx)(CenterColumn, { children: renderSlot("conversation", {}) }), (0, react_jsx_runtime.jsx)(DetailsColumn, { children: renderSlot("details", {}) })] }),', `(0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [
                        (0, react_jsx_runtime.jsx)(CenterColumn, {
                            style: nativeStyle,
                            hidden: nativeHidden, children: renderSlot("conversation", {})
                        }),
                        (0, react_jsx_runtime.jsx)(DetailsColumn, { style: { gridColumn: 3, gridRow: row, minHeight: 0 }, children: renderSlot("details", {}) })
                    ] }),`);
replace('style: { left: props.left },', 'style: { left: props.left, top: props.top || 0 },');
replace('side: "sidebar",\n\t\t\t\t\t\tleft:', 'side: "sidebar",\n                        top: topNavigation ? 48 : 0,\n                        left:');
replace('side: "details",\n\t\t\t\t\t\tleft:', 'side: "details",\n                        top: topNavigation ? 48 : 0,\n                        left:');
replace('onEnd: onDragEnd\n\t\t\t\t\t})\n\t\t\t\t]', `onEnd: onDragEnd
                    }),
                    (0, react_jsx_runtime.jsx)("div", {
                        "data-workspace-navigation": true, hidden: !hasNavigation,
                        style: { display: hasNavigation ? 'flex' : 'none', position: 'absolute', top: 0, left: 0,
                            width: topNavigation ? '100%' : cols.sidebar, height: 48, minWidth: 0, overflow: 'auto',
                            background: 'var(--dsw-specific-sidebar-fill)', zIndex: 3 },
                        children: renderSlot('workspace.navigation', { configuration, visible: hasNavigation })
                    }),
                    (0, react_jsx_runtime.jsx)("div", {
                        "data-workspace-surface": true, hidden: !workspaceVisible,
                        style: { display: workspaceVisible ? 'flex' : 'none', flexDirection: 'column', position: 'absolute',
                            top: topNavigation ? 48 : 0, bottom: 0, left: cols.sidebar + (split ? nativeWidth : 0),
                            width: canvasWidth, minWidth: 0, overflow: 'auto', background: 'var(--dsw-alias-bg-base)' },
                        children: renderSlot('workspace', { configuration, visible: workspaceVisible })
                    })
                ]`);
replace('narrowExpanded: false\n\t\t\t\t}),', 'narrowExpanded: false, workspaceConfiguration: null, workspaceVisible: false, nativeGeometry: null, nativeSurfaceRect: null, nativePresentation: null\n                }),');
replace('d.sidebar = clampWidth(px, 264, 420);', 'd.sidebar = clampWidth(px, d.workspaceConfiguration ? 200 : 264, d.workspaceConfiguration ? 480 : 420);');
replace('d.details = clampWidth(px, 300, 520);', 'd.details = clampWidth(px, d.workspaceConfiguration ? 240 : 300, d.workspaceConfiguration ? 640 : 520);');
replace('else d.sidebar = d.sidebar === 0 ? 280 : 0;', 'else d.sidebar = d.sidebar === 0 ? (d.workspaceConfiguration?.sidebarWidth ?? 280) : 0;');
replace('if (d.details === 0) d.details = 360;', 'if (d.details === 0) d.details = d.workspaceConfiguration?.detailsWidth ?? 360;');
replace('actions: {\n\t\t\t\t\tsetSidebar:', `actions: {
                    configureWorkspace: (d, configuration) => {
                        if (!d.nativeGeometry) d.nativeGeometry = { sidebar: d.sidebar, details: d.details, narrowExpanded: d.narrowExpanded };
                        const openingNativeSidebar = d.workspaceConfiguration?.nativeSidebar === false && configuration.nativeSidebar;
                        d.workspaceConfiguration = configuration;
                        d.sidebar = configuration.objectPanel ? configuration.objectPanel.open ? configuration.objectPanel.width : 0 : configuration.nativeSidebar ? configuration.sidebarWidth : 0;
                        if (configuration.objectPanel) d.narrowExpanded = configuration.objectPanel.open;
                        else if (!configuration.nativeSidebar) d.narrowExpanded = false;
                        else if (openingNativeSidebar) d.narrowExpanded = true;
                        if (d.details !== 0) d.details = configuration.detailsWidth;
                    },
                    resetWorkspace: d => {
                        if (d.nativeGeometry) { d.sidebar = d.nativeGeometry.sidebar; d.details = d.nativeGeometry.details; d.narrowExpanded = d.nativeGeometry.narrowExpanded; }
                        d.workspaceConfiguration = null; d.workspaceVisible = false; d.nativeGeometry = null; d.nativeSurfaceRect = null; d.nativePresentation = null;
                    },
                    setNativePresentation: (d, presentation) => { d.nativePresentation = presentation; },
                    setNativeSurfaceRect: (d, rect) => { d.nativeSurfaceRect = rect; },
                    setWorkspaceVisible: (d, visible) => { d.workspaceVisible = visible; },
                    setSidebar:`);
replace('const layout = new LayoutController();', 'const layout = new WorkspaceLayoutController();');
replace('"sidebar": {\n\t\t\t\t\t\t\tkind:', `"workspace": { kind: 'single', scope: 'root' },
                        "workspace.navigation": { kind: 'single', scope: 'root' },
                        "sidebar": {
                            kind:`);
replace('presenter.apply(ctx.theme.getTheme());\n\t\t\t\tconst off = ctx.on("theme/change", (snapshot) => {\n\t\t\t\t\tpresenter.apply(snapshot);\n\t\t\t\t});', `const media = typeof matchMedia === 'function' ? matchMedia('(prefers-color-scheme: dark)') : null;
                const refresh = () => presenter.apply(workspaceThemeSnapshot(ctx.theme.getTheme(), layout.getWorkspaceConfiguration(), media?.matches || false));
                refresh();
                const off = ctx.on("theme/change", refresh);
                const offWorkspace = layout.subscribeWorkspace(refresh);
                media?.addEventListener('change', refresh);`);
replace('off();\n\t\t\t\t\tpresenter.dispose();', `off();
                    offWorkspace();
                    media?.removeEventListener('change', refresh);
                    presenter.dispose();`);
const styleSource = (await readFile(new URL('../../shared/gui-style.mjs', root), 'utf8')).replace(/^export\s+/gm, '');
const extension = styleSource + '\n' + await readFile(new URL('workspace-extension.js', root), 'utf8');
replace('exports.LayoutController = LayoutController;', extension + '\n        exports.LayoutController = WorkspaceLayoutController;');
const modern = await modernFactory(extension);
replace('factory: (require) => {', `factory: (require) => {
        let legacyRuntime;
        try { legacyRuntime = require('@deepseek-ai/dsh-client-runtime/client'); } catch {}
        if (!legacyRuntime) return (${modern})(require);`);
source = '// Generated by packages/gui-layout/build.mjs. Do not edit. MIT upstream attribution: vendor/LICENSE.\n' + source.replace('\n//# sourceMappingURL=client.js.map', '');
const target = new URL('client.js', root);
if (process.argv.includes('--check')) {
  if (await readFile(target, 'utf8') !== source) throw new Error('Stale GUI layout client; run node packages/gui-layout/build.mjs');
} else await writeFile(target, source);
console.log('GUI layout bundle verified: ' + fileURLToPath(target));
