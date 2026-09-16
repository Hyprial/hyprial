// This source is injected inside the vendored ModuleLoader factory by build.mjs.
function freezeWorkspaceValue(value) {
  if (value && typeof value === 'object') { Object.values(value).forEach(freezeWorkspaceValue); Object.freeze(value); }
  return value;
}
function validateWorkspaceConfiguration(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid workspace configuration');
  if (!['left', 'top'].includes(value.navigation)) throw new Error('Invalid workspace navigation');
  if (value.nativeSidebar !== undefined && typeof value.nativeSidebar !== 'boolean') throw new Error('Invalid workspace nativeSidebar');
  const result = { navigation: value.navigation, nativeSidebar: value.nativeSidebar ?? true };
  if (value.objectPanel !== undefined) {
    const panel = value.objectPanel;
    if (!panel || typeof panel !== 'object' || Array.isArray(panel) ||
        Object.keys(panel).some(key => !['mode', 'open', 'width'].includes(key)) ||
        !['inline', 'drawer'].includes(panel.mode) || typeof panel.open !== 'boolean' ||
        !Number.isInteger(panel.width) || panel.width < 200 || panel.width > 480) throw new Error('Invalid workspace objectPanel');
    result.objectPanel = Object.freeze({ mode: panel.mode, open: panel.open, width: panel.width });
  }
  const workspaceRatio = value.workspaceRatio ?? 40;
  if (!Number.isInteger(workspaceRatio) || workspaceRatio < 20 || workspaceRatio > 80) throw new Error('Invalid workspace ratio');
  result.workspaceRatio = workspaceRatio;
  for (const [key, min, max, fallback] of [['sidebarWidth', 200, 480, 280], ['detailsWidth', 240, 640, 360]]) {
    const width = value[key] ?? fallback;
    if (!Number.isInteger(width) || width < min || width > max) throw new Error('Invalid workspace ' + key);
    result[key] = width;
  }
  if (value.theme !== undefined) {
    result.theme = freezeWorkspaceValue(guiValidateTheme(value.theme));
  }
  return Object.freeze(result);
}

// The same sidebar owner can act as an object list without restoring the
// global navigation. Responsive placement is transient, never a user preference.
function workspaceObjectPanel(configuration, viewport) {
  const panel = configuration?.objectPanel;
  if (!panel) return null;
  return { ...panel, mode: viewport < 700 ? 'drawer' : panel.mode,
    width: Math.min(panel.width, Math.max(0, viewport - 48)) };
}
function workspaceSidebarStyle(configuration, objectPanel, row) {
  const hidden = objectPanel ? !objectPanel.open : configuration?.nativeSidebar === false;
  const base = { display: hidden ? 'none' : undefined, gridColumn: 1, gridRow: row, boxSizing: 'border-box', paddingTop: 0 };
  if (objectPanel?.open && objectPanel.mode === 'drawer') Object.assign(base, {
    // An absolute grid item must use the whole frame as its containing block,
    // not the zero-width sidebar track or the second navigation row.
    gridColumn: 'auto', gridRow: 'auto',
    position: 'absolute', left: 0, top: configuration.navigation === 'top' ? 48 : 0, bottom: 0,
    width: objectPanel.width, maxWidth: 'calc(100% - 48px)', zIndex: 20
  });
  return base;
}

// Registry-backed theme choice is transient: trial must not write global theme
// preferences. The existing presenter remains the sole DOM theme owner.
function workspaceThemeSnapshot(snapshot, configuration, systemDark) {
  const theme = configuration?.theme;
  if (!theme) return snapshot;
  const mode = theme.mode === 'system' ? (systemDark ? 'dark' : 'light') : theme.mode;
  const active = mode && snapshot.active.colorScheme !== mode
    ? snapshot.themes.find(entry => entry.id === mode) || snapshot.active
    : snapshot.active;
  const tokens = { ...active.tokens };
  // GUI accent has one bounded role; no Agent-selected token names or CSS.
  if (theme.accent) tokens['--dsw-static-blue-500'] = theme.accent;
  if (workspaceHasStyle(theme)) {
    const variables = guiStyleVariables(theme, active.colorScheme);
    Object.assign(tokens, variables, guiStyleAliases(variables));
  }
  return { ...snapshot, active: { ...active, tokens } };
}

// Legacy mode/accent/density declarations keep the original native appearance.
function workspaceHasStyle(theme) {
  return ['preset', 'colors', 'typography', 'spacing', 'radius', 'borderWidth', 'shadow'].some(key => theme?.[key] !== undefined);
}
function validateNativePresentation(value) {
  if (value === null) return null;
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).some(key => !['theme', 'mode', 'appearance', 'appearanceChain'].includes(key)) ||
      !['light', 'dark'].includes(value.mode)) throw new Error('Invalid native presentation');
  const theme = guiValidateTheme(value.theme);
  if (value.appearanceChain !== undefined && (!Array.isArray(value.appearanceChain) || value.appearanceChain.length > 14 || value.appearance !== undefined)) throw new Error('Invalid native appearance chain');
  const chain = (value.appearanceChain || [value.appearance || {}]).map(entry => guiValidateAppearance(entry));
  const appearance = chain.at(-1) || {};
  const variables = chain.reduce((vars, entry) => guiModuleAppearanceVariables(vars, entry), guiStyleVariables(theme, value.mode));
  const appearanceStyle = guiAppearanceStyle(appearance);
  delete appearanceStyle.padding; // The authored wrapper already reserves this space.
  return Object.freeze({ ...variables, ...guiStyleAliases(variables),
    color: variables['--gui-style-text'], background: variables['--gui-style-background'],
    fontFamily: variables['--gui-style-font'], fontSize: variables['--gui-style-size'],
    lineHeight: variables['--gui-style-line-height'], colorScheme: value.mode, ...appearanceStyle });
}

class WorkspaceLayoutController extends LayoutController {
  workspaceVersion = 1;
  moduleSurfaceVersion = 1;
  #configuration = null;
  #visible = false;
  #nativeRect = null;
  #nativePresentation = null;
  #workspaceActions;
  #listeners = new Set();
  attachPanels(actions) {
    super.attachPanels(actions);
    this.#workspaceActions = actions;
  }
  configureWorkspace(value) {
    const configuration = validateWorkspaceConfiguration(value);
    // UI notifications and profile refreshes may repeat the same declaration.
    // Keep a user's transient drag geometry until the declaration changes.
    if (JSON.stringify(configuration) === JSON.stringify(this.#configuration)) return;
    this.#requireWorkspace().configureWorkspace(configuration);
    this.#configuration = configuration;
    this.#emit();
  }
  resetWorkspace() {
    if (this.#configuration === null && this.#visible === false && this.#nativeRect === null && this.#nativePresentation === null) return;
    this.#requireWorkspace().resetWorkspace();
    this.#configuration = null;
    this.#visible = false;
    this.#nativeRect = null;
    this.#nativePresentation = null;
    this.#emit();
  }
  setWorkspaceVisible(value) {
    if (typeof value !== 'boolean' && value !== 'split') throw new Error('Workspace visibility must be boolean or split');
    if (this.#visible === value) return;
    this.#requireWorkspace().setWorkspaceVisible(value);
    this.#visible = value;
  }
  setNativePresentation(value) {
    const presentation = validateNativePresentation(value);
    if (JSON.stringify(presentation) === JSON.stringify(this.#nativePresentation)) return;
    this.#requireWorkspace().setNativePresentation(presentation);
    this.#nativePresentation = presentation;
  }
  setNativeSurfaceRect(value) {
    let rect = null;
    if (value !== null) {
      if (!value || typeof value !== 'object') throw new Error('Invalid native surface rectangle');
      rect = {};
      for (const key of ['x', 'y', 'width', 'height']) {
        if (!Number.isFinite(value[key]) || Math.abs(value[key]) > 1000000 || (['width', 'height'].includes(key) && value[key] < 0)) throw new Error('Invalid native surface rectangle');
        rect[key] = Math.round(value[key] * 100) / 100;
      }
      rect.visible = value.visible !== false;
      if (value.clip !== undefined) {
        rect.clip = {};
        for (const key of ['top', 'right', 'bottom', 'left']) {
          if (!Number.isFinite(value.clip?.[key]) || value.clip[key] < 0 || value.clip[key] > 1000000) throw new Error('Invalid native surface clipping');
          rect.clip[key] = Math.round(value.clip[key] * 100) / 100;
        }
        Object.freeze(rect.clip);
      }
      Object.freeze(rect);
    }
    if (JSON.stringify(this.#nativeRect) === JSON.stringify(rect)) return;
    this.#requireWorkspace().setNativeSurfaceRect(rect);
    this.#nativeRect = rect;
  }
  getWorkspaceConfiguration() { return this.#configuration; }
  subscribeWorkspace(listener) {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }
  #emit() { for (const listener of this.#listeners) listener(); }
  #requireWorkspace() {
    if (!this.#workspaceActions) throw new Error('layout: workspace root not mounted');
    return this.#workspaceActions;
  }
}
