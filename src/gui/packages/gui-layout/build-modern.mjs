import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';

// npm latest's resolved UI graph uses the split client-store and keyed main
// panels. Keep that upstream contract intact while adding our workspace seats.
export async function modernFactory(extension) {
  let source = await readFile(new URL('./vendor/dsh-ui-layout-0.1.5-rc.2.js', import.meta.url), 'utf8');
  if (createHash('sha256').update(source).digest('hex') !== '930c10a9bed1094e7bca6242276c22ba7020fd58bac47d70a0fef174544508ef') throw new Error('Modern vendored layout digest mismatch');
  const replace = (before, after) => {
    if (source.split(before).length !== 2) throw new Error('Modern layout anchor missing or ambiguous: ' + before.slice(0, 80));
    source = source.replace(before, after);
  };
  source = source.replaceAll('@deepseek-ai/dsh-client-ui-layout', '@hyprial/dsh-gui-layout');
  replace('function computeColumns(viewport, sidebar, rightbar) {', 'function computeColumns(viewport, sidebar, rightbar, hideSidebar = false, objectPanel = false) {');
  replace('clampWidth(sidebar, 264, 420)', 'clampWidth(sidebar, objectPanel ? 200 : 264, objectPanel ? 480 : 420)');
  replace('const s = sidebar === 0 ? 56 :', 'const s = hideSidebar ? 0 : sidebar === 0 ? 56 :');
  replace('sidebarPreference, rightbarPreference);', 'sidebarPreference, rightbarPreference, sidebarTrackHidden, Boolean(objectPanel));');
  replace('layoutInfo.rightbarTrack ? rightbarPreference : 0);', 'layoutInfo.rightbarTrack ? rightbarPreference : 0, sidebarTrackHidden, Boolean(objectPanel));');
  replace('const layoutInfo = useStore((state) => state.layoutInfo);', `const state = useStore(state => state);
            const layoutInfo = state.layoutInfo;
            const configuration = state.workspaceConfiguration;
            const workspaceVisible = state.workspaceVisible;
            const topNavigation = configuration?.navigation === 'top';
            const row = topNavigation ? 2 : 1;`);
  replace('const sidebarCollapsed = narrow ? !layoutInfo.narrowExpanded : layoutInfo.sidebar === 0;', `const objectPanel = workspaceObjectPanel(configuration, viewport);
            const sidebarHidden = objectPanel ? !objectPanel.open : configuration?.nativeSidebar === false;
            const sidebarTrackHidden = objectPanel ? !objectPanel.open || objectPanel.mode === 'drawer' : sidebarHidden;
            const sidebarCollapsed = objectPanel ? false : sidebarHidden || (narrow ? !layoutInfo.narrowExpanded : layoutInfo.sidebar === 0);`);
  replace('const sidebarPreference = sidebarCollapsed ? 0 :', 'const sidebarPreference = objectPanel ? objectPanel.width : sidebarCollapsed ? 0 :');
  replace('!layoutInfo.rightbarShown && narrow ? 0 : sidebarPreference', '!objectPanel && !layoutInfo.rightbarShown && narrow ? 0 : sidebarPreference');
  replace('width: cols.sidebar', 'width: objectPanel ? objectPanel.width : cols.sidebar');
  replace('cols.sidebar\n\t\t\t]);', 'cols.sidebar, objectPanel?.open, objectPanel?.width\n\t\t\t]);');
  replace('!sidebarCollapsed && (0, react_jsx_runtime.jsx)(DragHandle,', '!objectPanel && !sidebarCollapsed && (0, react_jsx_runtime.jsx)(DragHandle,');
  replace('const colsRef = (0, react.useRef)(cols);', `const split = workspaceVisible === 'split' && cols.center >= 640;
            const canvasWidth = split ? cols.center * (configuration?.workspaceRatio ?? 40) / 100 : cols.center;
            const nativeWidth = split ? cols.center - canvasWidth : cols.center;
            const requestedNative = state.nativeSurfaceRect;
            const frameBox = frameRef.current?.getBoundingClientRect() || { left: 0, top: 0, width: viewport, height: window.innerHeight };
            const nativeX = requestedNative ? Math.max(0, requestedNative.x - frameBox.left + (requestedNative.clip?.left || 0)) : 0;
            const nativeY = requestedNative ? Math.max(0, requestedNative.y - frameBox.top + (requestedNative.clip?.top || 0)) : 0;
            const nativeRight = requestedNative ? Math.min(frameBox.width, requestedNative.x + requestedNative.width - frameBox.left - (requestedNative.clip?.right || 0)) : 0;
            const nativeBottom = requestedNative ? Math.min(frameBox.height, requestedNative.y + requestedNative.height - frameBox.top - (requestedNative.clip?.bottom || 0)) : 0;
            const nativeHidden = requestedNative ? !(requestedNative.visible && nativeRight > nativeX && nativeBottom > nativeY) : Boolean(workspaceVisible && !split);
            const nativeClip = requestedNative ? [Math.max(0, nativeY - (requestedNative.y - frameBox.top)),
              Math.max(0, requestedNative.x + requestedNative.width - frameBox.left - nativeRight),
              Math.max(0, requestedNative.y + requestedNative.height - frameBox.top - nativeBottom),
              Math.max(0, nativeX - (requestedNative.x - frameBox.left))].map(value => value + 'px').join(' ') : '';
            const nativeStyle = requestedNative ? { position: 'absolute', left: requestedNative.x - frameBox.left, top: requestedNative.y - frameBox.top,
              width: requestedNative.width, height: requestedNative.height, zIndex: 4, clipPath: 'inset(' + nativeClip + ')', minHeight: 0, display: nativeHidden ? 'none' : undefined }
              : { gridColumn: 2, gridRow: row, minHeight: 0, width: split ? nativeWidth : undefined, display: nativeHidden ? 'none' : undefined };
            Object.assign(nativeStyle, state.nativePresentation || {});
            const colsRef = (0, react.useRef)(cols);`);
  replace('className: AppFrame_module_css_default.centerCol,', 'className: AppFrame_module_css_default.centerCol, style: props.style, hidden: props.hidden, "data-native-conversation": true, "data-gui-styled": props.style?.["--gui-style-background"] ? "true" : undefined,');
  replace('className: AppFrame_module_css_default.rightbarCol,', 'className: AppFrame_module_css_default.rightbarCol, style: props.style,');
  replace('style: { gridTemplateColumns: `${cols.sidebar}px minmax(0, 1fr) ${cols.rightbar}px` },', `style: { gridTemplateColumns: \`\${cols.sidebar}px minmax(0, 1fr) \${cols.rightbar}px\`, gridTemplateRows: topNavigation ? '48px minmax(0,1fr)' : '100%' },
                "data-gui-layout": "1", "data-gui-surface": split ? 'split' : workspaceVisible ? 'page' : 'native',
                "data-gui-navigation": configuration?.navigation, "data-gui-styled": workspaceHasStyle(configuration?.theme) ? "true" : undefined, "data-gui-density": configuration?.theme?.density,`);
  replace('className: AppFrame_module_css_default.sidebarCol,', `className: AppFrame_module_css_default.sidebarCol, "data-native-sidebar": true, hidden: sidebarHidden, "data-gui-object-panel": objectPanel ? objectPanel.open ? objectPanel.mode : 'closed' : undefined, style: workspaceSidebarStyle(configuration, objectPanel, row),`);
  replace('(CenterColumn, { children: main })', '(CenterColumn, { style: nativeStyle, hidden: nativeHidden, children: main })');
  replace('(RightbarColumn, { children:', '(RightbarColumn, { style: { gridColumn: 3, gridRow: row }, children:');
  replace('style: { left: props.left },', 'style: { left: props.left, top: props.top || 0 },');
  replace('side: "sidebar",', 'side: "sidebar", top: topNavigation ? 48 : 0,');
  replace('side: "rightbar",', 'side: "rightbar", top: topNavigation ? 48 : 0,');
  replace('onEnd: onDragEnd\n\t\t\t\t\t})\n\t\t\t\t]', `onEnd: onDragEnd
                    }),
                    (0, react_jsx_runtime.jsx)("div", {
                      "data-workspace-navigation": true, hidden: !topNavigation,
                      style: { display: topNavigation ? 'flex' : 'none', position: 'absolute', top: 0, left: 0, width: '100%', height: 48,
                        minWidth: 0, overflow: 'auto', background: 'var(--dsw-specific-sidebar-fill)', zIndex: 3 },
                      children: renderSlot('workspace.navigation', { configuration, visible: topNavigation })
                    }),
                    (0, react_jsx_runtime.jsx)("div", {
                      "data-workspace-surface": true, hidden: !workspaceVisible,
                      style: { display: workspaceVisible ? 'flex' : 'none', flexDirection: 'column', position: 'absolute',
                        top: topNavigation ? 48 : 0, bottom: 0, left: cols.sidebar + (split ? nativeWidth : 0), width: canvasWidth,
                        minWidth: 0, overflow: 'auto', background: 'var(--dsw-alias-bg-base)' },
                      children: renderSlot('workspace', { configuration, visible: workspaceVisible })
                    })
                ]`);
  replace('panelInfo: { activePanelId: null },', 'panelInfo: { activePanelId: null }, workspaceConfiguration: null, workspaceVisible: false, nativeGeometry: null, nativeSurfaceRect: null, nativePresentation: null,');
  replace('actions: {\n\t\t\t\t\tselectPanel:', `actions: {
                    configureWorkspace: (d, configuration) => {
                      if (!d.nativeGeometry) d.nativeGeometry = { sidebar: d.layoutInfo.sidebar, rightbar: d.layoutInfo.rightbar, narrowExpanded: d.layoutInfo.narrowExpanded };
                      const openingNativeSidebar = d.workspaceConfiguration?.nativeSidebar === false && configuration.nativeSidebar;
                      d.workspaceConfiguration = configuration; d.layoutInfo.sidebar = configuration.objectPanel ? configuration.objectPanel.open ? configuration.objectPanel.width : 0 : configuration.nativeSidebar ? configuration.sidebarWidth : 0;
                      if (configuration.objectPanel) d.layoutInfo.narrowExpanded = configuration.objectPanel.open;
                      else if (!configuration.nativeSidebar) d.layoutInfo.narrowExpanded = false;
                      else if (openingNativeSidebar) d.layoutInfo.narrowExpanded = true;
                      if (d.layoutInfo.rightbarShown) d.layoutInfo.rightbar = configuration.detailsWidth;
                    },
                    resetWorkspace: d => {
                      if (d.nativeGeometry) Object.assign(d.layoutInfo, d.nativeGeometry);
                      d.workspaceConfiguration = null; d.workspaceVisible = false; d.nativeGeometry = null; d.nativeSurfaceRect = null; d.nativePresentation = null;
                    },
                    setNativePresentation: (d, presentation) => { d.nativePresentation = presentation; },
                    setNativeSurfaceRect: (d, rect) => { d.nativeSurfaceRect = rect; },
                    setWorkspaceVisible: (d, visible) => { d.workspaceVisible = visible; },
                    selectPanel:`);
  replace('d.layoutInfo.sidebar = clampWidth(px, 264, 420);', 'd.layoutInfo.sidebar = clampWidth(px, d.workspaceConfiguration ? 200 : 264, d.workspaceConfiguration ? 480 : 420);');
  replace('function apply(ctx) {', 'function apply(ctx) {\n            let workspaceLayout;');
  replace('const layout = new LayoutController(instance.actions, (id) => ctx.slots.entries("main").some((entry) => entry.options.key === id));', `const layout = new WorkspaceLayoutController(instance.actions, (id) => ctx.slots.entries("main").some((entry) => entry.options.key === id));
                layout.attachPanels(instance.actions); workspaceLayout = layout;`);
  replace('"sidebar": {\n\t\t\t\t\t\t\tkind:', `"workspace": { kind: 'single', scope: 'root' },
                        "workspace.navigation": { kind: 'single', scope: 'root' },
                        "sidebar": {\n\t\t\t\t\t\t\tkind:`);
  replace('presenter.apply(ctx.theme.getTheme());\n\t\t\t\tconst off = ctx.on("theme/change", (snapshot) => {\n\t\t\t\t\tpresenter.apply(snapshot);\n\t\t\t\t});', `const media = typeof matchMedia === 'function' ? matchMedia('(prefers-color-scheme: dark)') : null;
                const refresh = () => presenter.apply(workspaceThemeSnapshot(ctx.theme.getTheme(), workspaceLayout.getWorkspaceConfiguration(), media?.matches || false));
                refresh();
                const off = ctx.on("theme/change", refresh);
                const offWorkspace = workspaceLayout.subscribeWorkspace(refresh);
                media?.addEventListener('change', refresh);`);
  replace('off();\n\t\t\t\t\tpresenter.dispose();', `off(); offWorkspace(); media?.removeEventListener('change', refresh);
                    presenter.dispose();`);
  replace('exports.LayoutController = LayoutController;', extension.replace('super.attachPanels(actions);', 'super.attachPanels?.(actions);') + '\n        exports.LayoutController = WorkspaceLayoutController;');
  const match = source.match(/factory: (\(require\) => \{[\s\S]*\n\t\})\n\}\);/);
  if (!match) throw new Error('Modern layout factory envelope changed');
  return match[1];
}
