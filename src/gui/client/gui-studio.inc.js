    // GUI documents change presentation; business controllers remain owned by DSH/H2B.
    const guiState = { profile: null, applied: null, trial: null, open: false, all: false, pageId: null, error: '', ready: false };
    const guiListeners = new Set();
    const guiModuleViews = Object.create(null);
    const guiAuthoring = new Map();
    const guiAuthoringCache = createGuiAuthoringCache({
      getItem: function (key) { return window.localStorage.getItem(key); },
      setItem: function (key, value) { window.localStorage.setItem(key, value); },
      removeItem: function (key) { window.localStorage.removeItem(key); }
    });
    const GUI_WORKSPACE_FEATURES = [
      ['dsh.conversation', ['default', 'launcher', 'list', 'summary']],
      ['h2b.directChat', ['default', 'launcher', 'list']],
      ['h2b.contacts', ['default', 'launcher', 'list', 'detail']],
      ['h2b.workflow', ['default', 'launcher', 'list', 'runs', 'detail']],
      ['h2b.kanban', ['default', 'launcher', 'readOnly', 'tasks', 'detail']],
      ['h2b.operations', ['default', 'launcher', 'summary']],
      ['h2b.routine', ['default', 'launcher', 'list']]
    ].map(function (entry) { return { id: entry[0], views: entry[1], singleton: entry[0] === 'dsh.conversation', mountPolicy: 'instance' }; });
    const guiRegistry = createGuiFeatureRegistry(GUI_WORKSPACE_FEATURES);
    const GuiVisualEditor = createGuiVisualEditor(React);
    const GuiProfileManager = createGuiProfileManager(React);
    const GuiConfirmationDialog = createGuiConfirmationDialog(React);
    const guiLayout = ctx.get('layout');
    const guiModuleRuntime = createGuiModuleRuntime({ registry: guiRegistry, maxInstances: 512 });
    const guiModuleReferences = new Map(), guiModuleSlotProps = new Map(), guiCanonicalBindings = new Map();
    const GuiPersistentModuleHost = createGuiModuleHost(React);
    let guiModuleHostElement = null, guiLastGeometry = '';
    function guiModuleKey(value) {
      // Stable text encoding has no hash collisions and uses legal instance-id
      // characters. Native DSH UUIDs and feature names fit the runtime bound.
      return Array.from(String(value)).map(function (character) { return character.codePointAt(0).toString(16); }).join('-');
    }
    function guiDefaultModuleId(feature, context) {
      if (feature === 'h2b.directChat' && context.sessionId) return 'direct:' + guiModuleKey(context.sessionId);
      return 'native:' + feature;
    }
    function guiAuthoredModuleDescriptor(node, doc) {
      const context = Object.fromEntries(Object.keys(node.context || {}).sort().map(function (key) { return [key, node.context[key]]; })), view = node.view || 'default';
      if (!node.instanceId) throw new Error('GUI 模块缺少稳定 instanceId');
      let instanceId = 'auth:' + doc.id + ':' + node.instanceId;
      if (view === 'default' && node.feature === 'dsh.conversation') return { instanceId: 'native:dsh.conversation', feature: node.feature, view: view, context: {} };
      if (view === 'default' && node.feature === 'h2b.directChat' && context.sessionId) {
        // Session ledger owns the peer identity. Extra authoring context cannot
        // create another composer or rebind this session's canonical owner.
        return { instanceId: guiDefaultModuleId(node.feature, context), feature: node.feature, view: view, context: { sessionId: context.sessionId } };
      }
      else if (view === 'default' && node.feature !== 'dsh.conversation' && !Object.keys(context).length) {
        const key = doc.id + ':' + node.feature;
        if (!guiCanonicalBindings.has(key)) guiCanonicalBindings.set(key, node.instanceId);
        if (guiCanonicalBindings.get(key) === node.instanceId) instanceId = guiDefaultModuleId(node.feature, context);
      }
      return { instanceId: instanceId, feature: node.feature, view: view, context: context };
    }
    function guiReconcileModules() {
      const descriptors = new Map();
      guiModuleReferences.forEach(function (reference) {
        const descriptor = reference.descriptor;
        const previous = descriptors.get(descriptor.instanceId);
        if (previous && JSON.stringify(previous) !== JSON.stringify(descriptor)) throw new Error('GUI 模块实例上下文冲突');
        descriptors.set(descriptor.instanceId, descriptor);
      });
      guiModuleRuntime.reconcile(Array.from(descriptors.values()));
    }
    function GuiModulePlaceholder(props) {
      const reference = React.useRef(null), owner = React.useId();
      const descriptorKey = JSON.stringify(props.descriptor);
      React.useLayoutEffect(function () {
        const descriptor = props.descriptor;
        guiModuleReferences.set(owner, { descriptor: descriptor });
        let stop;
        try {
          guiReconcileModules();
          stop = observeGuiModulePlacement(guiModuleRuntime, descriptor.instanceId, reference.current,
            guiModuleHostElement || document.body, window, { owner: owner, priority: props.priority || 0 });
        } catch (error) { guiShowError(error); }
        return function () {
          if (stop) stop();
          guiModuleReferences.delete(owner);
          try { guiReconcileModules(); } catch (error) { guiShowError(error); }
        };
      }, [descriptorKey, props.priority, owner]);
      React.useLayoutEffect(function () {
        if (!props.nativeProps) return;
        const previous = guiModuleSlotProps.get(props.descriptor.instanceId), next = props.nativeProps;
        if (previous && Object.keys(previous).length === Object.keys(next).length && Object.keys(next).every(function (key) { return Object.prototype.hasOwnProperty.call(previous, key) && Object.is(previous[key], next[key]); })) return;
        guiModuleSlotProps.set(props.descriptor.instanceId, next);
        guiModuleRuntime.refresh();
      }, [props.nativeProps, props.descriptor.instanceId]);
      return React.createElement('div', { ref: reference, 'data-gui-module-placeholder': props.descriptor.instanceId,
        'data-gui-placeholder-feature': props.descriptor.feature,
        style: props.priority ? { minWidth: 0, width: '100%', height: props.height || 620, minHeight: 240,
          border: '1px solid var(--dsw-alias-border-l1)', borderRadius: 8 } : {
            // Native composer seats have auto height. Percentage height alone
            // collapses an empty placeholder; retain the original business
            // panels' intrinsic viewport height without sizing nested seats.
            flex: props.composer ? '0 0 auto' : 1, width: '100%',
            height: props.composer ? 'calc(100dvh - 96px)' : '100%', minHeight: 0, minWidth: 0 } });
    }
    function guiNativeModuleSeat(feature, props, context, view, composer = false) {
      const reference = context || {}, selectedView = view || 'default';
      return React.createElement(GuiModulePlaceholder, { descriptor: {
        instanceId: guiDefaultModuleId(feature, reference), feature: feature, view: selectedView, context: reference
      }, nativeProps: props, priority: 0, composer: composer });
    }
    let guiDesignNativeRect = null, guiWorkspaceNativeRect = null, guiWorkspaceNativePresentation = null;
    function guiPlaceNativeModule(rect, instance) {
      const visibilityChanged = Boolean(guiWorkspaceNativeRect?.visible) !== Boolean(rect?.visible);
      guiWorkspaceNativeRect = rect;
      if (visibilityChanged) { guiSyncLayout(); guiListeners.forEach(function (fn) { fn(); }); }
      const presentation = readGuiModulePresentation(instance ? guiModuleRuntime.getPlacementTarget(instance.instanceId) : null);
      guiWorkspaceNativePresentation = presentation.theme ? { theme: presentation.theme, mode: presentation.mode, ...(presentation.appearanceChain?.length ? { appearanceChain: presentation.appearanceChain } : {}) } : null;
      guiLayout?.setNativePresentation?.(guiDesignNativeRect ? null : guiWorkspaceNativePresentation);
      if (guiLayout && guiLayout.moduleSurfaceVersion === 1) guiLayout.setNativeSurfaceRect(guiDesignNativeRect || rect);
    }
    function guiPlaceDesignConversation(rect) {
      const visibilityChanged = Boolean(guiDesignNativeRect) !== Boolean(rect);
      guiDesignNativeRect = rect;
      if (visibilityChanged) { guiSyncLayout(); guiListeners.forEach(function (fn) { fn(); }); }
      guiLayout?.setNativePresentation?.(rect ? null : guiWorkspaceNativePresentation);
      if (guiLayout && guiLayout.moduleSurfaceVersion === 1) guiLayout.setNativeSurfaceRect(rect || guiWorkspaceNativeRect);
    }
    function guiRenderModule(instance) {
      const View = guiModuleViews[instance.feature];
      if (!View) return React.createElement('p', { role: 'status' }, guiLabel(instance.feature) + ' 模块当前不可用');
      return React.createElement(View, Object.assign({}, guiModuleSlotProps.get(instance.instanceId) || {}, {
        instanceId: instance.instanceId, context: instance.context, view: instance.view,
        visible: Boolean(instance.active && instance.rect && instance.rect.visible)
      }));
    }
    function GuiModuleLayer() {
      return React.createElement(GuiPersistentModuleHost, { runtime: guiModuleRuntime, renderModule: guiRenderModule,
        onNativePlacement: guiPlaceNativeModule, hostRef: function (element) { guiModuleHostElement = element; } });
    }
    let guiNavigationEpoch = 0;
    let guiSidebarOverride = null;
    let guiObjectsOverride = null;
    function guiObjectPanelConfiguration() {
      const shell = guiShellDocument();
      if (!shell || guiNativeSidebarVisible(shell) || guiState.open || guiDesignNativeRect) return null;
      // Authored modules already own their lists. Only the external native
      // conversation needs the shared object browser alongside an authored page.
      if (guiState.pageId && !guiWorkspaceNativeRect?.visible) return null;
      const preference = shell.layout?.objects || {};
      const narrow = typeof window !== 'undefined' && window.innerWidth < 700;
      const mode = narrow || preference.mode === 'drawer' ? 'drawer' : 'inline';
      const key = JSON.stringify([shell.id, preference, mode]);
      const open = guiObjectsOverride?.key === key ? guiObjectsOverride.open : mode === 'inline' && preference.mode !== 'collapsible';
      return { mode, open, width: preference.width || 280 };
    }
    function guiToggleObjects(open) {
      const panel = guiObjectPanelConfiguration(), shell = guiShellDocument();
      if (!panel || !shell) return;
      guiObjectsOverride = { key: JSON.stringify([shell.id, shell.layout?.objects || {}, panel.mode]), open: open ?? !panel.open };
      guiNotify();
    }
    function guiOpenObjectSession(id) {
      if (guiState.pageId && guiWorkspaceNativeRect?.visible) guiState.nativeSessionId = id;
      else guiClearPage();
      if (sessions) sessions.open(id);
      guiNotify(); guiObjectsSelected();
    }
    function guiObjectsSelected() {
      if (guiObjectPanelConfiguration()?.mode === 'drawer') guiToggleObjects(false);
    }
    function guiSidebarKey(doc) { return JSON.stringify([doc.id, doc.layout || {}]); }
    function guiNativeSidebarVisible(doc) {
      if (doc.layout?.navigation === 'native') return true;
      if (guiSidebarOverride?.key === guiSidebarKey(doc)) return guiSidebarOverride.visible;
      return doc.layout?.nativeSidebar ?? doc.layout?.navigation !== 'top';
    }
    function guiClearPage() { guiNavigationEpoch++; guiState.pageId = null; guiState.pageReleaseId = null; guiState.nativeSessionId = null; }
    const guiDefaultOnly = typeof location !== 'undefined' && new URLSearchParams(location.search).get('gui') === 'default';
    function guiNotify() { guiSyncLayout(); guiListeners.forEach(function (fn) { fn(); }); notifyAppShell(); }
    function guiSyncLayout() {
      if (!guiLayout || guiLayout.workspaceVersion !== 1) return;
      const doc = guiDocument();
      const shell = guiShellDocument();
      const geometry = shell ? Object.assign({ navigation: 'left' }, shell.layout, { navigation: shell.layout?.navigation === 'native' ? 'left' : shell.layout?.navigation || 'left', theme: shell.theme, nativeSidebar: guiNativeSidebarVisible(shell), objectPanel: guiObjectPanelConfiguration() || undefined }) : null;
      const signature = JSON.stringify(geometry);
      if (signature !== guiLastGeometry) { if (geometry) guiLayout.configureWorkspace(geometry); else guiLayout.resetWorkspace(); guiLastGeometry = signature; }
      if (doc && doc.schemaVersion === 2 && guiState.pageId) { guiLayout.setWorkspaceVisible(true); return; }
      let native = false;
      function scan(node) { if (node.type === 'Feature' && (!node.view || node.view === 'default')) native = true; (node.children || []).forEach(scan); }
      if (doc && guiState.pageId) { const page = doc.pages.find(function (p) { return p.id === guiState.pageId; }); if (page) scan(page.layout); }
      guiLayout.setWorkspaceVisible(doc && guiState.pageId ? (native ? 'split' : true) : false);
    }
    function guiBaseDocument() { return guiDefaultOnly ? null : guiState.trial || guiState.applied; }
    function guiShellDocument() {
      if (guiDefaultOnly) return null;
      if (guiState.trial?.kind === 'shell') return guiState.trial;
      return guiState.applied?.kind === 'shell' ? guiState.applied : null;
    }
    function guiResolveDocument(releaseId) {
      if (guiDefaultOnly) return null;
      if (!releaseId) return guiBaseDocument();
      if (releaseId === guiState.profile?.releaseId) return guiState.profile.release?.document || null;
      return guiState.profile?.pages?.find(function (release) { return release.id === releaseId; })?.document || null;
    }
    function guiDocument() { return guiResolveDocument(guiState.pageReleaseId); }
    function guiNavigateTarget(target) {
      if (!guiProfileTargetAvailable(guiState.profile, [], target)) return Promise.reject(new Error('入口已不可用，请刷新个人页面库'));
      return target.feature ? guiNavigate(target.feature) : guiNavigate(null, target.pageId, target.releaseId);
    }
    function guiSubscribe(fn) { guiListeners.add(fn); return function () { guiListeners.delete(fn); }; }
    async function guiCall(operation, args) { return host.call('h2b-gui-studio', Object.assign({ operation: operation }, args || {})); }
    async function guiRefreshProfile() {
      const requestedNavigationEpoch = guiNavigationEpoch;
      const profile = await guiCall('profile');
      if (guiState.profile && profile.revision < guiState.profile.revision) return guiState.profile;
      if (guiState.profile && guiState.profile.releaseId !== profile.releaseId && !guiState.trial) guiClearPage();
      const firstLoad = !guiState.ready;
      guiState.profile = profile; guiState.applied = profile.release && profile.release.document || null;
      if (guiState.pageReleaseId && !guiResolveDocument(guiState.pageReleaseId)) guiClearPage();
      guiState.ready = true; guiState.error = ''; guiNotify();
      if (firstLoad && profile.home && !guiDefaultOnly && !guiState.trial && !guiState.open && requestedNavigationEpoch === guiNavigationEpoch) await guiNavigateTarget(profile.home);
      return profile;
    }
    function useGuiState() {
      const [, force] = React.useState(0);
      React.useEffect(function () { return guiSubscribe(function () { force(function (n) { return n + 1; }); }); }, []);
      return guiState;
    }
    function guiCaptureLocation() {
      return { pageId: guiState.pageId, pageReleaseId: guiState.pageReleaseId, nativeSessionId: guiState.nativeSessionId,
        sessionId: snapshotOf(sessions).current, section: h2bControlState.section, runScope: h2bControlState.runScope };
    }
    function guiReturnToUse() {
      const location = guiState.returnLocation;
      guiState.returnLocation = null; guiState.open = false; guiState.trial = null; guiState.menu = false;
      guiClearPage();
      if (location) {
        if (location.sessionId && listedSessions().some(function (item) { return item.id === location.sessionId; })) sessions.open(location.sessionId);
        if (location.section) selectH2bControlSection(location.section, location.runScope);
        const doc = guiResolveDocument(location.pageReleaseId);
        if (location.pageId && doc?.pages.some(function (page) { return page.id === location.pageId; })) {
          guiState.pageId = location.pageId; guiState.pageReleaseId = location.pageReleaseId; guiState.nativeSessionId = location.nativeSessionId;
        }
      }
      guiNotify();
    }
    function guiOpen(id) {
      if (!guiState.returnLocation) guiState.returnLocation = guiCaptureLocation();
      guiState.requestedDraftId = typeof id === 'string' ? id : null;
      guiState.menu = false; guiState.all = false; guiState.favoritesOpen = false; guiState.trial = null;
      guiClearPage(); guiState.open = true; guiNotify();
    }
    function guiEditCurrent() {
      guiState.editRelease = guiState.profile?.release || null;
      guiOpen(guiState.editRelease?.draftId);
    }
    async function guiNavigate(feature, pageId, releaseId) {
      const epoch = ++guiNavigationEpoch;
      guiState.all = false;
      if (pageId) {
        const requestedReleaseId = releaseId === undefined ? guiState.pageReleaseId : releaseId;
        const doc = guiResolveDocument(requestedReleaseId), page = doc && doc.pages.find(function (p) { return p.id === pageId; });
        if (!page) throw new Error('个人页面不存在');
        let native;
        function scan(node) { if (node.type === 'Feature' && (!node.view || node.view === 'default') && (doc.schemaVersion !== 2 || node.feature === 'dsh.conversation')) native = node; (node.children || []).forEach(scan); }
        scan(page.layout);
        if (doc.schemaVersion === 2 && (!guiLayout || guiLayout.moduleSurfaceVersion !== 1)) throw new Error('请更新 GUI 布局宿主后打开多模块页面');
        if (native && native.context && native.context.sessionId) {
          const reference = listedSessions().find(function (session) { return session.id === native.context.sessionId; });
          const archived = new Set(snapshotOf(workspaces).archivedSessionIds || []);
          if (!reference || archived.has(reference.id) || currentAppSurface(reference.id) !== 'messages' || demoEntry(reference.id).humanChat ||
            ['H2B · 控制台', 'H2B · 通讯录', 'MFU · Business Console'].includes(reference.displayTitle)) throw new Error('页面引用的 Agent 会话不可用');
          sessions.open(native.context.sessionId);
        } else if (native) await guiActivateNative(native.feature, epoch);
        if (epoch !== guiNavigationEpoch || doc !== guiResolveDocument(requestedReleaseId)) return;
        guiState.nativeSessionId = snapshotOf(sessions).current;
        guiState.pageId = pageId; guiState.pageReleaseId = requestedReleaseId || null; guiState.open = false; guiNotify(); return;
      }
      guiState.pageId = null; guiState.pageReleaseId = null; guiState.open = false; guiNotify();
      return guiActivateNative(feature, epoch);
    }
    async function guiActivateNative(feature, epoch) {
      const canOpen = function () { return epoch === guiNavigationEpoch; };
      const currentId = snapshotOf(sessions).current;
      const rows = listedSessions();
      function eligible(session) { return currentAppSurface(session.id) === 'messages' && !['H2B · 控制台', 'H2B · 通讯录', 'MFU · Business Console'].includes(session.displayTitle); }
      const current = rows.find(function (session) { return session.id === currentId && eligible(session); });
      if (current) { if (demoEntry(current.id).humanChat) guiState.lastDirectSessionId = current.id; else guiState.lastAgentSessionId = current.id; }
      if (feature === 'dsh.conversation' || feature === 'h2b.directChat') {
        const direct = feature === 'h2b.directChat';
        const archived = new Set(snapshotOf(workspaces).archivedSessionIds || []);
        const candidates = rows.filter(function (session) { return !archived.has(session.id) && eligible(session) && Boolean(demoEntry(session.id).humanChat) === direct; });
        const preferred = direct ? guiState.lastDirectSessionId : guiState.lastAgentSessionId;
        const candidate = candidates.find(function (session) { return session.id === preferred; }) || candidates[0];
        if (candidate) { sessions.open(candidate.id); return; }
        if (direct) return openH2bDirectory(canOpen);
        const id = createdSessionId(await sessions.create({}));
        if (!id) throw new Error('无法创建 Agent 会话');
        if (canOpen()) sessions.open(id); return;
      }
      if (feature === 'h2b.contacts') return openH2bDirectory(canOpen);
      const sections = { 'h2b.workflow': 'workflows', 'h2b.kanban': 'kanban', 'h2b.routine': 'schedules', 'h2b.operations': 'overview' };
      if (sections[feature]) { selectH2bControlSection(sections[feature]); return openH2bControl(canOpen); }
    }
    function guiPageButtons() {
      if (guiDefaultOnly) return null;
      const doc = guiBaseDocument(), targets = [];
      if (doc?.kind === 'page') doc.pages.forEach(function (page) { targets.push({ key: 'active:' + page.id, label: page.title, run: function () { return guiNavigate(null, page.id, null); } }); });
      (guiState.profile?.pages || []).filter(function (release) { return release.document.id !== guiShellDocument()?.id; }).forEach(function (release) { release.document.pages.forEach(function (page) {
        targets.push({ key: release.id + ':' + page.id, label: page.title, run: function () { return guiNavigateTarget({ releaseId: release.id, pageId: page.id }); } });
      }); });
      return targets.map(function (item) { return React.createElement('button', { key: item.key, className: 'h2bapps-nav', onClick: function () { item.run().catch(guiShowError); } }, React.createElement('span', { className: 'h2bapps-nav-icon' }, '▦'), React.createElement('span', { className: 'h2bapps-nav-label' }, item.label)); });
    }
    function guiNavigationItems() {
      const doc = guiShellDocument();
      const configured = guiDefaultOnly || doc ? null : guiState.profile?.navigation;
      if (!doc && !configured?.orderedFeatures?.length && !configured?.hiddenFeatures?.length) return null;
      const items = doc ? doc.navigation.slice() : GUI_WORKSPACE_FEATURES.map(function (feature) { return { id: feature.id, feature: feature.id, label: guiLabel(feature.id) }; });
      const hidden = new Set(configured?.hiddenFeatures || []), order = configured?.orderedFeatures || [];
      return items.filter(function (item) { return !item.feature || !hidden.has(item.feature); }).sort(function (a, b) {
        const ai = order.indexOf(a.feature), bi = order.indexOf(b.feature);
        return (ai < 0 ? order.length : ai) - (bi < 0 ? order.length : bi);
      });
    }
    function guiNavigationButtons() {
      const doc = guiShellDocument(), items = guiNavigationItems();
      if (!items) return null;
      // The layout owns sidebar visibility. A top shell must leave the native
      // application rail intact so opening native navigation restores every level.
      if (doc?.layout?.navigation === 'native' || (doc?.layout?.navigation === 'top' && guiLayout?.workspaceVersion === 1)) return null;
      return items.map(function (item) {
        return React.createElement('button', { key: item.id, className: 'h2bapps-nav', title: item.label, onClick: function () { guiNavigate(item.feature, item.pageId, null).catch(guiShowError); } },
          React.createElement('span', { className: 'h2bapps-nav-icon' }, item.pageId ? '▦' : '◇'),
          React.createElement('span', { className: 'h2bapps-nav-label' }, item.label));
      });
    }
    function guiShowError(error) { guiState.error = error.message || String(error); guiNotify(); }
    function guiLabel(feature) {
      return { 'dsh.conversation': 'Agent 会话', 'h2b.directChat': 'H2B 直聊', 'h2b.contacts': '通讯录', 'h2b.workflow': 'Workflow', 'h2b.kanban': '任务看板', 'h2b.routine': 'Routine', 'h2b.operations': '运维' }[feature] || feature;
    }
    function guiReference(feature) {
      return React.createElement('article', { className: 'gui-feature-reference' }, React.createElement('strong', null, guiLabel(feature)),
        React.createElement('p', null, '完整业务模块 · 在原生工作区打开'),
        React.createElement('button', { onClick: function () { guiNavigate(feature).catch(guiShowError); } }, '打开 ' + guiLabel(feature)));
    }
    const GuiWorkspace = createGuiWorkspaceRenderer(React);
    let GuiBoundaryImpl;
    function GuiBoundary(props) {
      if (!GuiBoundaryImpl) GuiBoundaryImpl = class extends React.Component {
        constructor(value) { super(value); this.state = { error: null }; }
        static getDerivedStateFromError(error) { return { error: error }; }
        render() { return this.state.error ? React.createElement('div', { role: 'alert' }, '个人界面无法显示。请使用系统栏恢复默认。') : this.props.children; }
      };
      return React.createElement(GuiBoundaryImpl, props);
    }
    function GuiPage() {
      useGuiState();
      const doc = guiDocument();
      if (!doc || !guiState.pageId) return null;
      return React.createElement('section', { className: 'gui-personal-page', 'data-gui-scope': doc.kind, 'aria-label': doc.kind === 'shell' ? '整体工作空间' : '个人常用页' },
        doc.kind === 'shell' ? null : React.createElement('header', null, React.createElement('strong', null, '个人页面 · ' + doc.name), React.createElement('button', { onClick: function () { guiClearPage(); guiNotify(); } }, '返回工作区')),
        React.createElement(GuiBoundary, { key: doc.id + ':' + guiState.pageId }, React.createElement(GuiWorkspace, {
          document: doc, pageId: guiState.pageId, registry: guiRegistry, shellNavigationExternal: Boolean(guiLayout?.workspaceVersion === 1), renderFeature: function (feature, view, nodeId, node) {
            if (doc.schemaVersion === 2 && view !== 'launcher') return React.createElement(GuiModulePlaceholder, {
              descriptor: guiAuthoredModuleDescriptor(node, doc), priority: 10, height: node.height
            });
            if (view === 'readOnly' && feature === 'h2b.kanban' && guiModuleViews[feature]) return React.createElement(guiModuleViews[feature], { embedded: true });
            if (view === 'default') return React.createElement('article', { className: 'gui-feature-reference' }, React.createElement('strong', null, guiLabel(feature)), React.createElement('p', null, '完整交互模块显示在原生工作区，保持原有会话、编辑状态和授权。'), React.createElement('button', { onClick: function () { guiClearPage(); guiNotify(); } }, '展开工作区'));
            return guiReference(feature);
          }, onNavigate: function (item) { guiNavigate(item.feature, item.pageId).catch(guiShowError); }
        })));
    }
    function GuiSystemLayer() {
      useGuiState();
      const doc = guiDocument();
      const objectPanel = guiObjectPanelConfiguration();
      React.useEffect(function () {
        function resize() { guiSyncLayout(); guiListeners.forEach(function (fn) { fn(); }); }
        window.addEventListener('resize', resize);
        return function () { window.removeEventListener('resize', resize); };
      }, []);
      React.useEffect(function () {
        function dismiss(event) {
          if (event.type === 'keydown' && event.key !== 'Escape') return;
          if (event.type === 'keydown' && guiObjectPanelConfiguration()?.mode === 'drawer') guiToggleObjects(false);
          if (event.type === 'pointerdown' && event.target.closest?.('.gui-workspace-menu, .gui-system-bar, .gui-all-functions')) return;
          if (guiState.menu || guiState.all || guiState.favoritesOpen) { guiState.menu = false; guiState.all = false; guiState.favoritesOpen = false; guiNotify(); }
        }
        window.addEventListener('pointerdown', dismiss); window.addEventListener('keydown', dismiss);
        return function () { window.removeEventListener('pointerdown', dismiss); window.removeEventListener('keydown', dismiss); };
      }, []);
      React.useEffect(function () {
        let active = true;
        guiRefreshProfile().catch(function (error) { if (active) { guiState.error = 'GUI 定制暂不可用：' + error.message; guiNotify(); } });
        function refresh() { if (!document.hidden) guiRefreshProfile().catch(guiShowError); }
        window.addEventListener('focus', refresh);
        return function () { active = false; window.removeEventListener('focus', refresh); };
      }, []);
      return React.createElement(React.Fragment, null,
        objectPanel?.open && objectPanel.mode === 'drawer' ? React.createElement('button', { className: 'gui-object-backdrop', style: { left: Math.min(objectPanel.width, Math.max(0, window.innerWidth - 48)) }, 'aria-label': '关闭对象列表', onClick: function () { guiToggleObjects(false); } }) : null,
        !guiState.open ? React.createElement('div', { className: 'gui-system-bar', 'aria-label': '工作空间入口' },
          React.createElement('button', { 'data-gui-action': 'workspace-menu', 'aria-expanded': !!guiState.menu, onClick: function () { guiState.menu = !guiState.menu; guiState.all = false; guiState.favoritesOpen = false; guiNotify(); } }, '工作空间')) : null,
        guiState.menu && !guiState.open ? React.createElement('section', { className: 'gui-workspace-menu', 'aria-label': '工作空间菜单' },
          React.createElement('strong', null, guiDefaultOnly ? '安全模式 · 原生界面' : guiState.profile?.release ? guiState.profile.release.document.name + ' · v' + guiState.profile.release.draftRevision : '原生界面'),
          guiDefaultOnly ? React.createElement('a', { href: '/' }, '返回正常启动') : null,
          guiState.profile?.release && !guiDefaultOnly ? React.createElement('button', { 'data-gui-action': 'edit-current', onClick: guiEditCurrent }, '编辑当前界面') : null,
          React.createElement('button', { 'data-gui-action': 'studio', onClick: function () { guiState.editRelease = null; guiOpen(); } }, '界面管理'),
          React.createElement('button', { onClick: function () { guiState.editRelease = null; guiOpen(); guiState.focusStartup = true; } }, '启动页设置'),
          !guiDefaultOnly && guiState.profile?.home ? React.createElement('button', { onClick: function () { guiState.menu = false; guiNavigateTarget(guiState.profile.home).catch(guiShowError); } }, '打开启动页') : null,
          guiShellDocument() && guiShellDocument().layout?.navigation !== 'native' ? React.createElement('button', { 'data-gui-action': 'native-navigation', 'aria-pressed': guiNativeSidebarVisible(guiShellDocument()), onClick: function () { const shell = guiShellDocument(); guiSidebarOverride = { key: guiSidebarKey(shell), visible: !guiNativeSidebarVisible(shell) }; guiState.menu = false; guiNotify(); } }, guiNativeSidebarVisible(guiShellDocument()) ? '收起原生侧栏' : '显示原生侧栏') : null,
          React.createElement('button', { onClick: function () { guiState.menu = false; guiState.all = true; guiNotify(); } }, '全部功能'),
          !guiDefaultOnly && guiState.profile?.favorites?.length ? React.createElement('button', { onClick: function () { guiState.menu = false; guiState.favoritesOpen = true; guiNotify(); } }, '常用收藏') : null,
          React.createElement('details', null, React.createElement('summary', null, '故障恢复'),
            React.createElement('a', { href: '?gui=default', title: '本次绕过定制加载，不改变已启用界面' }, '安全打开原生界面'),
            React.createElement('p', null, '安全打开仅影响当前地址；永久切换请在界面管理中操作。'))) : null,
        guiState.trial && !guiState.open ? React.createElement('div', { className: 'gui-trial-banner', role: 'status' },
          React.createElement('strong', null, '交互预览 · 尚未启用'),
          React.createElement('span', null, '聊天、发送和运行仍会真实生效。'),
          React.createElement('button', { 'data-gui-action': 'back-to-editor', onClick: function () { guiOpen(guiState.trialDraftId); } }, '返回编辑'),
          React.createElement('button', { 'data-gui-action': 'end-trial', onClick: guiReturnToUse }, '结束预览')) : null,
        guiState.notice && !guiState.open ? React.createElement('div', { className: 'gui-use-notice', role: 'status' }, guiState.notice,
          React.createElement('button', { onClick: function () { guiState.notice = ''; guiNotify(); } }, '关闭提示')) : null,
        guiState.favoritesOpen && !guiDefaultOnly ? React.createElement('nav', { className: 'gui-all-functions', 'aria-label': '常用收藏' },
          (guiState.profile?.favorites || []).filter(function (target) { return guiProfileTargetAvailable(guiState.profile, [], target); }).map(function (target) {
            const item = guiProfileTargets(guiState.profile, []).find(function (value) { return value.key === guiProfileTargetKey(target); });
            return React.createElement('button', { key: item.key, onClick: function () { guiState.favoritesOpen = false; guiNavigateTarget(target).catch(guiShowError); } }, item.label);
          })) : null,
        guiState.all ? React.createElement('nav', { className: 'gui-all-functions', 'aria-label': '全部功能' },
          GUI_WORKSPACE_FEATURES.map(function (feature) { return React.createElement('button', { key: feature.id, onClick: function () { guiNavigate(feature.id).catch(guiShowError); } }, guiLabel(feature.id)); }),
          (guiDefaultOnly ? [] : guiProfileTargets(guiState.profile, [])).filter(function (item) { return item.target.pageId; }).map(function (item) {
            return React.createElement('button', { key: item.key, onClick: function () { guiNavigateTarget(item.target).catch(guiShowError); } }, item.label);
          }),
          guiState.trial ? guiState.trial.pages.map(function (page) { return React.createElement('button', { key: 'trial:' + page.id, onClick: function () { guiNavigate(null, page.id, null).catch(guiShowError); } }, page.title); }) : null,
          React.createElement('button', { onClick: function () { guiState.editRelease = null; guiOpen(); } }, '界面与版本管理')) : null,
        guiState.error ? React.createElement('div', { className: 'gui-system-error', role: 'alert' }, guiState.error, React.createElement('button', { onClick: function () { guiState.error = ''; guiNotify(); } }, '关闭')) : null,
        (!guiLayout || guiLayout.workspaceVersion !== 1) && guiState.pageId && !guiState.open ? React.createElement(GuiPage) : null,
        guiState.open ? React.createElement(GuiStudio) : null);
    }
    function guiChangeDocumentScope(document, kind) {
      const value = JSON.parse(JSON.stringify(document));
      if (!['page', 'shell'].includes(kind)) throw new Error('未知界面范围');
      if (kind === 'page' && value.pages.length !== 1) throw new Error('个人页面只能包含一页，请先保留一个页面或复制界面再调整');
      value.kind = kind;
      if (kind === 'shell') {
        value.layout = Object.assign({ navigation: 'left' }, value.layout);
        const navigation = value.navigation.slice(), ids = new Set(navigation.map(function (item) { return item.id; }));
        function append(target, label, stem) {
          if (navigation.some(function (item) { return target.feature ? item.feature === target.feature : item.pageId === target.pageId; })) return;
          let id = stem, suffix = 1;
          while (ids.has(id)) id = stem + '-' + suffix++;
          ids.add(id); navigation.push(Object.assign({ id: id, label: label }, target));
        }
        // Keep business modules intact: navigation opens their existing owners.
        GUI_WORKSPACE_FEATURES.forEach(function (feature, index) { append({ feature: feature.id }, guiLabel(feature.id), 'workspace-feature-' + index); });
        value.pages.forEach(function (page, index) { append({ pageId: page.id }, page.title, 'workspace-page-' + index); });
        if (navigation.length > 32) throw new Error('导航入口超过 32 个，请先合并重复入口后再转换');
        value.navigation = navigation;
      }
      return value;
    }
    function guiFocus(document, selection, previousDocument) {
      const workspace = { selection: '@workspace', label: '整个工作空间', path: ['整个工作空间'] };
      if (!document || !selection || selection === '@workspace') return workspace;
      const [scope, id] = selection.split('/');
      if (scope === '@navigation') { const nav = document.navigation.find(function (n) { return n.id === id; }); return nav ? { selection, label: '导航 · ' + nav.label, path: ['导航菜单', nav.label] } : id ? workspace : { selection: '@navigation/', label: '整个导航菜单', path: ['导航菜单'] }; }
      const page = document.pages.find(function (p) { return p.id === (scope === '@page' ? id : scope); });
      if (!page) return workspace;
      const base = { selection: '@page/' + page.id, label: '页面 · ' + page.title, path: [page.title] };
      if (scope === '@page' || !id) return base;
      let found;
      function walk(node, path) { const label = node.type === 'Text' ? node.text.slice(0, 36) : node.type === 'Feature' ? guiLabel(node.feature) : ({Stack:'纵向排列', Grid:'网格', Split:'分栏', Tabs:'标签页'}[node.type] || node.type); const next = path.concat(label); if (node.id === id) found = {selection, label: next.join(' › '), path: next}; (node.children || []).forEach(function (n) { walk(n, next); }); }
      walk(page.layout, [page.title]);
      if (!found && previousDocument) {
        const oldPage = previousDocument.pages.find(function (p) { return p.id === page.id; }); let ancestors = [];
        function oldWalk(node, trail) { if (node.id === id) ancestors = trail; (node.children || []).forEach(function (n) { oldWalk(n, trail.concat(node.id)); }); }
        if (oldPage) oldWalk(oldPage.layout, []);
        for (const parent of ancestors.reverse()) { const recovered = guiFocus(document, page.id + '/' + parent); if (recovered.selection === page.id + '/' + parent) return recovered; }
      }
      return found || base;
    }
    function GuiStudio() {
      const e = React.createElement;
      const [items, setItems] = React.useState([]), [releases, setReleases] = React.useState([]);
      const [editorCatalog, setEditorCatalog] = React.useState(GUI_WORKSPACE_FEATURES);
      const [draft, setDraft] = React.useState(null), [example, setExample] = React.useState(null);
      const [text, setText] = React.useState(''), [dirty, setDirty] = React.useState(false), [instruction, setInstruction] = React.useState('');
      const [busy, setBusy] = React.useState(''), [error, setError] = React.useState(''), [selected, setSelected] = React.useState('');
      const [preview, setPreview] = React.useState(null), [notice, setNotice] = React.useState('');
      const [view, setView] = React.useState('manage'), [assistant, setAssistant] = React.useState(false);
      const [template, setTemplate] = React.useState('conversation'), [cacheError, setCacheError] = React.useState('');
      const [propertiesOpen, setPropertiesOpen] = React.useState(false);
      const [propertiesHeight, setPropertiesHeight] = React.useState(230);
      React.useEffect(function () { if (view !== 'edit' || !draft) return; sessions.open(draft.sessionId); setAssistant(true); setPropertiesOpen(false); }, [view, draft?.id]);
      const [lockedFocus, setLockedFocus] = React.useState(null), [locateSelection, setLocateSelection] = React.useState(0);
      React.useEffect(function () { setLockedFocus(null); }, [draft?.id]);
      const [lastId, setLastId] = React.useState(null);
      const [confirmation, setConfirmation] = React.useState(null);
      React.useEffect(function () { setConfirmation(null); }, [draft?.id, draft?.revision, text]);
      const lock = React.useRef(false), currentDraft = React.useRef(null), dirtyRef = React.useRef(false);
      const authoringRef = React.useRef(null), mounted = React.useRef(true), cacheTimer = React.useRef(null);
      currentDraft.current = draft; dirtyRef.current = dirty;
      authoringRef.current = { draft, text, dirty, instruction, selected, preview };
      function retain() {
        const value = authoringRef.current;
        if (!value?.draft) return;
        guiAuthoring.set(value.draft.id, value);
        try { guiAuthoringCache.write(value.draft.id, value); if (mounted.current) setCacheError(''); }
        catch (err) {
          const message = err.message + '。当前编辑仍保留在本窗口，请保存草稿后再刷新。';
          if (mounted.current) setCacheError(message);
          guiState.error = message;
        }
      }
      function accept(value) {
        setDraft(value); setText(JSON.stringify(value.document, null, 2)); setDirty(false); setPreview(value.document);
        // Update the synchronous snapshot too: an exit while a save completes
        // must retain the new base revision, never the obsolete dirty snapshot.
        authoringRef.current = Object.assign({}, authoringRef.current, { draft: value, text: JSON.stringify(value.document, null, 2), dirty: false, preview: value.document });
        currentDraft.current = value; dirtyRef.current = false;
      }
      async function refresh() {
        const list = await guiCall('list'); setItems(list.drafts); setExample(list.exampleV2 || list.example); setEditorCatalog(list.catalogV2 || list.catalog || GUI_WORKSPACE_FEATURES);
        const history = await guiCall('releases'); setReleases(history.releases); await guiRefreshProfile();
        try { setLastId(guiAuthoringCache.lastId()); } catch (err) { setCacheError(err.message); }
      }
      async function load(id) {
        retain();
        const value = await guiCall('get', { id });
        let cached = guiAuthoring.get(id);
        if (!cached) { try { cached = guiAuthoringCache.read(id); } catch (err) { setCacheError(err.message); } }
        accept(value); setSelected(''); setInstruction(''); setAssistant(false); setView('edit');
        if (cached) {
          setInstruction(cached.instruction || ''); setSelected(cached.selected || '');
          if (cached.dirty) {
            setDraft(cached.draft); setText(cached.text); setDirty(true); setPreview(cached.preview || cached.draft.document);
            setNotice(value.revision !== cached.draft.revision ? '已恢复本地工作稿。服务器已有新修订，保存时会检查冲突；可导出本地工作稿，或放弃本地修改并读取最新版本。' : '已恢复未保存的工作稿，可以继续编辑或直接退出。');
          }
        }
      }
      function leave() { if (lock.current) return; retain(); guiReturnToUse(); }
      function manage() { retain(); setAssistant(false); setView('manage'); }
      React.useEffect(function () {
        mounted.current = true;
        return function () { mounted.current = false; cacheTimer.current?.(); retain(); };
      }, []);
      React.useEffect(function () {
        cacheTimer.current?.();
        cacheTimer.current = ctx.timeout(retain, 250);
        return function () { cacheTimer.current?.(); };
      }, [draft, text, dirty, instruction, selected, preview]);
      React.useEffect(function () {
        function flush() { retain(); }
        function key(event) {
          if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing) return;
          if (event.target.closest?.('.gui-agent-dock, dialog, [role=dialog], [role=alertdialog]')) return;
          event.preventDefault(); leave();
        }
        window.addEventListener('pagehide', flush); window.addEventListener('keydown', key);
        return function () { window.removeEventListener('pagehide', flush); window.removeEventListener('keydown', key); };
      }, []);
      async function action(label, fn) {
        if (lock.current) return; lock.current = true; setBusy(label); setError(''); setNotice('');
        try { await fn(); } catch (err) { if (mounted.current) setError(err.message || String(err)); else { guiState.error = err.message || String(err); guiNotify(); } }
        finally { lock.current = false; if (mounted.current) setBusy(''); }
      }
      React.useEffect(function () { action('加载', async function () {
        await refresh();
        if (guiState.requestedDraftId) {
          const id = guiState.requestedDraftId, release = guiState.editRelease;
          guiState.requestedDraftId = null; guiState.editRelease = null;
          const list = await guiCall('list');
          if (list.drafts.some(function (item) { return item.id === id; })) await load(id);
          else if (release) {
            const recovered = list.drafts.filter(function (item) { return item.document.id === release.document.id; }).sort(function (a, b) { return (b.updatedAt || 0) - (a.updatedAt || 0); })[0];
            if (recovered) await load(recovered.id);
            else { accept(await guiCall('import', { document: release.document, sessionId: await designSession() })); setView('edit'); setNotice('原草稿已移除，已从当前启用版本创建编辑稿。'); }
          }
          else throw new Error('草稿已不存在，请在界面管理中选择其他界面。');
        }
        if (guiState.focusStartup) { guiState.focusStartup = false; ctx.timeout(function () { document.querySelector('.gui-profile-manager')?.scrollIntoView({ block: 'start' }); }, 0); }
      }); }, []);
      React.useEffect(function () {
        let active = true, reading = false;
        const timer = setInterval(async function () {
          const value = currentDraft.current;
          if (!value || dirtyRef.current || lock.current || reading || document.hidden) return;
          reading = true;
          try {
            const latest = await guiCall('get', { id: value.id });
            if (active && !dirtyRef.current && !lock.current && currentDraft.current?.id === value.id && currentDraft.current.revision === value.revision && latest.revision > value.revision) {
              accept(latest); setNotice('Agent 已更新设计，画布已同步到 v' + latest.revision + '。');
            }
          } catch (err) { if (active) setError(err.message); } finally { reading = false; }
        }, 2500);
        return function () { active = false; clearInterval(timer); };
      }, []);
      function button(label, fn, disabled, key, extra) {
        return e('button', Object.assign({ type: 'button', key: key || label, disabled: !!busy || !!disabled, onClick: function () { action(label, fn); } }, extra || {}), label);
      }
      async function designSession() {
        const native = listedSessions().find(function (s) { return s.id === snapshotOf(sessions).current && currentAppSurface(s.id) === 'messages' && !demoEntry(s.id).humanChat; });
        if (native) return native.id;
        const id = createdSessionId(await sessions.create({}));
        if (!id) throw new Error('无法创建 GUI 设计会话');
        const binding = sessions.binding(id);
        if (binding?.session?.rename) await binding.session.rename('GUI · 个人工作空间');
        return id;
      }
      async function create() {
        retain(); const sid = await designSession();
        const doc = JSON.parse(JSON.stringify(example)); doc.schemaVersion = 2; doc.kind = 'shell'; doc.id = 'personal-' + Date.now().toString(36); doc.name = '我的工作空间';
        doc.navigation = [{ id: 'conversation', label: 'Agent 会话', feature: 'dsh.conversation' }, { id: 'workflow', label: 'Workflow', feature: 'h2b.workflow' }, { id: 'kanban', label: '任务看板', feature: 'h2b.kanban' }, { id: 'home', label: '常用页', pageId: 'home' }];
        if (template === 'tasks') {
          doc.name = '任务工作空间'; doc.layout.navigation = 'top'; doc.pages[0].title = '任务推进';
          doc.pages[0].layout = { type: 'Split', id: 'task-layout', ratio: 60, children: [{ type: 'Feature', id: 'workflow', feature: 'h2b.workflow' }, { type: 'Feature', id: 'kanban', feature: 'h2b.kanban', view: 'readOnly' }] };
        } else if (template === 'page') {
          doc.name = '我的常用页'; doc.kind = 'page'; doc.pages[0].title = '我的常用页';
          doc.pages[0].layout = { type: 'Stack', id: 'page-layout', children: [{ type: 'Text', id: 'heading', text: '我的常用工作空间' }, { type: 'Feature', id: 'workflow', feature: 'h2b.workflow' }] };
        }
        doc.pages.forEach(function (page) { function identify(node) { if (node.type === 'Feature' && !node.instanceId) node.instanceId = 'instance-' + page.id + '-' + node.id; (node.children || []).forEach(identify); } identify(page.layout); });
        accept(await guiCall('create', { document: doc, sessionId: sid })); setSelected(''); setInstruction(''); setAssistant(false); setView('edit'); await refresh();
      }
      async function save() {
        const value = await guiCall('update', { id: draft.id, baseRevision: draft.revision, document: JSON.parse(text) });
        accept(value); retain(); await refresh(); return value;
      }
      async function conversation() {
        if (!listedSessions().some(function (row) { return row.id === draft.sessionId; }) || (snapshotOf(workspaces).archivedSessionIds || []).includes(draft.sessionId)) throw new Error('设计会话不可用，请先恢复该会话');
        retain(); guiClearPage(); guiNotify(); sessions.open(draft.sessionId); setAssistant(true);
      }
      const previousFocusDocument = React.useRef(null);
      const focus = guiFocus(preview || draft?.document, lockedFocus === null ? selected : lockedFocus, previousFocusDocument.current);
      React.useEffect(function () {
        if (!draft || !preview) return;
        if (selected && guiFocus(preview, selected, previousFocusDocument.current).selection !== selected) { setSelected(guiFocus(preview, selected, previousFocusDocument.current).selection); setNotice('原选区已不存在，已返回仍存在的父级。'); }
        if (lockedFocus !== null && focus.selection !== lockedFocus) { setLockedFocus(focus.selection); setNotice('锁定目标已不存在，讨论焦点已返回仍存在的父级。'); }
        previousFocusDocument.current = preview;
      }, [preview, selected, lockedFocus]);
      const designSendContext = React.useRef(null);
      const nativeFocusAvailable = ctx.get('conversation')?.guiDesignContextVersion === 1;
      designSendContext.current = draft && view === 'edit' && assistant ? { draft, dirty, focus, save } : null;
      React.useEffect(function () {
        return ctx.on('gui-design/before-send', async function (request) {
          const captured = designSendContext.current;
          if (!guiState.open || !captured || request.sessionId !== captured.draft.sessionId) return;
          if (lock.current) throw new Error('正在处理界面草稿，请稍后发送；输入内容已保留。');
          lock.current = true; setBusy('同步设计焦点');
          try {
            const value = captured.dirty ? await captured.save() : captured.draft;
            request.contextText = '[GUI 设计焦点]\n' + JSON.stringify({ draftId: value.id, revision: value.revision, selection: captured.focus.selection, path: captured.focus.path, scope: value.document.kind }) +
              '\n这是用户发送此消息时的编辑焦点。先调用 h2b_gui_context 读取最新草稿与样式规范；修订变化时重新核对目标，不覆盖其他区域。仅在用户要求修改时编辑，保留完整业务模块；不自动发布或应用。';
          } catch (err) { if (mounted.current) setError('设计上下文同步失败，消息未发送：' + err.message); throw err; }
          finally { lock.current = false; if (mounted.current) setBusy(''); }
        });
      }, []);
      async function trial() {
        if (!guiLayout || guiLayout.workspaceVersion !== 1) throw new Error('请更新 GUI 布局宿主后再预览');
        const value = dirty ? await save() : draft;
        await guiCall('validate', { id: value.id, revision: value.revision });
        retain(); guiState.trial = value.document; guiState.trialDraftId = value.id;
        guiClearPage(); guiState.open = false; guiNotify();
        const pageId = selected.startsWith('@page/') ? selected.slice(6) : selected.startsWith('@') ? null : selected.split('/')[0];
        await guiNavigate(null, value.document.pages.some(function (p) { return p.id === pageId; }) ? pageId : value.document.pages[0].id, null);
      }
      async function activateRelease(release) {
        const isPage = release.document.kind === 'page';
        let profile;
        try {
          await guiCall(isPage ? 'install-page' : 'apply', { releaseId: release.id, baseRevision: guiState.profile.revision });
        } catch (error) {
          try { profile = await guiRefreshProfile(); } catch (_) {}
          const confirmed = profile && (isPage ? profile.pageReleaseIds.includes(release.id) : profile.releaseId === release.id);
          if (!confirmed) throw new Error('版本已发布，启用尚未确认：' + error.message + '。' + (profile ? '已刷新当前状态，可重试启用。' : '请恢复连接后重新打开界面管理核对启用版本。'));
        }
        guiState.trial = null; guiClearPage();
        try { profile = await guiRefreshProfile(); }
        catch (error) { throw new Error('启用操作已成功，但读取最新界面状态失败：' + error.message + '。请恢复连接后重新打开界面管理核对；无需重新设计。'); }
        guiState.returnLocation = null; guiState.open = false; guiState.menu = false;
        guiState.notice = isPage ? '已安装页面 ' + release.document.name + '，当前整体界面保持不变。' : '已启用 ' + release.document.name + ' · v' + release.draftRevision + '，下次正常打开继续使用。';
        guiNotify();
        const target = isPage ? { releaseId: release.id, pageId: release.document.pages[0].id } : profile.home || { releaseId: release.id, pageId: release.document.pages[0].id };
        try { await guiNavigateTarget(target); } catch (error) { guiShowError(new Error('界面已启用，但启动页打开失败：' + error.message)); }
      }
      async function apply() {
        if (!guiLayout || guiLayout.workspaceVersion !== 1) throw new Error('请更新 GUI 布局宿主后再启用个人界面');
        const value = dirty ? await save() : draft;
        const release = await guiCall('publish', { id: value.id, revision: value.revision });
        await activateRelease(release);
      }
      function download(document) {
        const url = URL.createObjectURL(new Blob([JSON.stringify({ document }, null, 2)], { type: 'application/json' }));
        const a = window.document.createElement('a'); a.href = url; a.download = document.id + '.gui.json'; a.click(); ctx.timeout(function () { URL.revokeObjectURL(url); }, 1000);
      }
      function edit(mutator) {
        try { const value = JSON.parse(text); mutator(value); setText(JSON.stringify(value, null, 2)); setPreview(value); setDirty(true); setError(''); }
        catch (_) { setError('界面定义中有无效 JSON，请在“界面定义与精确编辑”中修正，或放弃本地编辑。'); }
      }
      async function discard() {
        accept(await guiCall('get', { id: draft.id })); guiAuthoring.delete(draft.id); guiAuthoringCache.remove(draft.id); setNotice('已放弃本地界面修改，读取最新草稿。');
      }
      async function changeScope(kind) {
        const value = guiChangeDocumentScope(JSON.parse(text), kind);
        guiEditorHelpers().inspect(value);
        setText(JSON.stringify(value, null, 2)); setPreview(value); setDirty(true);
        setNotice(kind === 'shell' ? '工作稿已转换为整体工作空间，保留原页面、样式和业务模块，并补齐功能入口。发布并启用后覆盖整个 GUI；当前启用版本尚未改变。' : '工作稿已改为个人页面。发布只安装这一页，当前整体 GUI 保持不变。');
      }
      const safeMode = guiDefaultOnly ? e('p', { className: 'gui-safe-mode', role: 'status' }, '当前为安全启动：个人界面暂不加载。可编辑草稿；预览和应用请返回正常启动。 ', e('a', { href: '/' }, '返回正常启动')) : null;
      const appearance = preview ? e('details', { className: 'gui-editor-appearance' }, e('summary', null, '界面与外观'),
        e('label', { className: 'gui-editor-field' }, '界面名称', e('input', { 'aria-label': '界面名称', value: preview.name, maxLength: 160, onChange: function (event) { edit(function (doc) { doc.name = event.target.value; }); } })),
        guiThemeControls(React, preview.theme || {}, function (theme) { try { const valid = guiValidateTheme(theme); guiEditorHelpers().inspect(Object.assign({}, preview, { theme: valid })); edit(function (doc) { doc.theme = valid; }); } catch (error) { setError('配色或样式未应用：' + error.message + '。请调整后重试，或重置全局样式。'); } })) : null;
      const editing = view === 'edit' && draft;
      return e(React.Fragment, null,
        confirmation ? e(GuiConfirmationDialog, { title: confirmation.title, confirmLabel: confirmation.title.startsWith('放弃') ? '确认放弃' : '确认删除', message: confirmation.message, onCancel: function () { setConfirmation(null); }, onConfirm: function () { const pending = confirmation; setConfirmation(null); action(pending.title, pending.commit); } }) : null,
        e('section', { className: 'gui-studio' + (editing && assistant ? ' is-docked' : ''), role: 'dialog', 'aria-label': 'GUI 设计工作台', 'data-gui-view': editing ? 'edit' : 'manage', 'data-collaboration': assistant ? 'agent-first' : 'properties', 'data-properties-open': propertiesOpen, style: {'--gui-properties-height': propertiesHeight + 'px'} },
          e('header', { className: 'gui-studio-header' },
            e('div', { className: 'gui-studio-heading' }, e('strong', null, editing ? (preview || draft.document).name : '界面管理'),
              e('small', null, '当前启用：' + (guiState.profile?.release ? guiState.profile.release.document.name + ' · v' + guiState.profile.release.draftRevision : '原生界面')),
              e('small', { role: 'status' }, editing ? busy ? busy + '…' : dirty ? '有未保存修改 · 工作稿保留在本机' : '草稿已保存 · 修订 ' + draft.revision : '选择工作空间或个人页面，进入编辑后再调整布局')),
            e('div', { className: 'gui-studio-primary-actions' },
              editing ? e('button', { type: 'button', 'data-gui-action': 'manage', onClick: manage }, '界面管理') : null,
              editing ? button('保存草稿', save, !dirty, 'save', { 'data-gui-action': 'save', className: 'gui-primary' }) : null,
              editing ? button('交互预览', trial, guiDefaultOnly, 'preview', { 'data-gui-action': 'preview', title: guiDefaultOnly ? '安全启动模式下不可预览，请返回正常启动' : '保存后预览当前页；业务操作仍会真实生效' }) : null,
              editing ? button((preview || draft.document).kind === 'page' ? '发布并安装页面' : '发布并启用', apply, guiDefaultOnly, 'apply', { 'data-gui-action': 'apply', title: guiDefaultOnly ? '安全启动模式下不可应用' : '保存并应用当前设计' }) : null,
              editing ? e('button', { type: 'button', 'data-gui-action': 'agent', 'aria-pressed': assistant, onClick: function () { if (assistant) setAssistant(false); else conversation().catch(function (err) { setError(err.message); }); } }, assistant ? '收起会话' : '与 Agent 对话') : null,
              e('button', { type: 'button', 'data-gui-action': 'exit', onClick: leave, disabled: !!busy, title: '退出编辑并返回原位置，保留工作稿。Esc 也可退出。' }, '退出编辑'))),
          safeMode,
          editing ? e('div', { className: 'gui-studio-notice', 'data-gui-scope': (preview || draft.document).kind },
            e('strong', null, (preview || draft.document).kind === 'shell' ? '整体工作空间' : '个人页面'),
            e('p', null, (preview || draft.document).kind === 'shell' ? '发布并启用后，主题统一作用于导航与工作区。Agent 会话、Workflow、Kanban 等业务模块保持完整，系统栏始终保留全部功能与恢复入口。' : '发布后安装到个人页面库，样式只作用于这一页，不会替换整个 GUI。要定制整个界面，请转换为整体工作空间。'),
            (preview || draft.document).kind === 'page' ? button('转换为整体工作空间', function () { return changeScope('shell'); }, false, 'scope-shell', { 'data-gui-action': 'scope-shell' }) : button('改为个人页面', function () { return changeScope('page'); }, (preview || draft.document).pages.length !== 1, 'scope-page', { 'data-gui-action': 'scope-page', title: '仅单页工作空间可转换；修改保留在工作稿，发布前可切换回来' })) : null,
          error ? e('p', { className: 'gui-error', role: 'alert' }, error) : null,
          cacheError ? e('p', { className: 'gui-error', role: 'alert' }, cacheError) : null,
          notice ? e('p', { className: 'gui-studio-notice', role: 'status' }, notice) : null,
          editing ? e('main', { className: 'gui-studio-main gui-studio-edit-main' },
            e('fieldset', { className: 'gui-editor-fieldset', disabled: !!busy, inert: busy ? true : undefined, 'aria-busy': !!busy },
              e('legend', { className: 'gui-editor-legend' }, '编辑画布 · 点击模块选择并配置，预览后才可操作业务功能'),
              e(GuiVisualEditor, { key: draft.id, document: preview, selected, onSelect: setSelected, inspectorExtras: appearance,
                onShowInspector: function () { setPropertiesOpen(true); }, locateSelection,
                catalog: draft.document.schemaVersion === 2 ? editorCatalog : editorCatalog.map(function (feature) { return Object.assign({}, feature, { views: feature.views.filter(function (v) { return ['default', 'launcher', 'readOnly'].includes(v); }) }); }),
                onChange: function (value) { if (lock.current) return; setPreview(value); setText(JSON.stringify(value, null, 2)); setDirty(true); setError(''); } })),
            e('details', { className: 'gui-editor-advanced' }, e('summary', null, '界面定义与精确编辑'), e('p', null, '作用范围：整个界面草稿。以下操作不局限于当前选中的组件或页面。'),
              e('textarea', { className: 'gui-json-editor', 'aria-label': 'GUI JSON', value: text, disabled: !!busy, onChange: function (event) { setText(event.target.value); setDirty(true); try { const parsed = JSON.parse(event.target.value); guiEditorHelpers().inspect(parsed); setPreview(parsed); } catch (_) {} } })),
            e('details', { className: 'gui-editor-more' }, e('summary', null, '更多操作'),
              e('div', { className: 'gui-studio-actions' },
                button('打开设计会话', async function () { retain(); guiState.returnLocation = null; guiState.open = false; guiClearPage(); sessions.open(draft.sessionId); guiNotify(); }),
                button('刷新草稿', async function () { retain(); await load(draft.id); }, false),
                button('放弃本地编辑', function () { setConfirmation({ title: '放弃整个草稿的未保存修改', message: '重新读取服务端草稿，会放弃当前全部页面、组件和导航的未保存修改。当前启用界面和业务数据不变。', commit: discard }); }, !dirty),
                button('复制界面', async function () { const value = dirty ? await save() : draft; accept(await guiCall('clone', { id: value.id, baseRevision: value.revision })); setSelected(''); await refresh(); }),
                draft.document.schemaVersion === 1 ? button('升级为多模块布局', async function () { const value = dirty ? await save() : draft; accept(await guiCall('migrate', { id: value.id, baseRevision: value.revision })); await refresh(); }) : null,
                button('导出', async function () { if (dirty) download(JSON.parse(text)); else { const value = await guiCall('export', { id: draft.id }); download(value.document || value); } }),
                button('删除草稿', function () { const captured = { id: draft.id, revision: draft.revision, text }; setConfirmation({ title: '删除整个界面草稿', message: '删除“' + (preview?.name || draft.document.name) + '”及其本地工作稿，包含全部页面、组件和导航设计。已发布版本、当前启用界面与业务数据保留。此操作不可通过画布撤销恢复。', commit: async function () {
                  if (currentDraft.current?.id !== captured.id || currentDraft.current?.revision !== captured.revision || authoringRef.current?.text !== captured.text) throw new Error('草稿已更新，请重新核对删除对象。');
                  await guiCall('delete', { id: captured.id, baseRevision: captured.revision }); guiAuthoring.delete(captured.id); guiAuthoringCache.remove(captured.id); authoringRef.current = null; setDraft(null); setPreview(null); setAssistant(false); setView('manage'); await refresh();
                } }); })))
            ) : e('main', { className: 'gui-studio-manager' },
              e('section', { className: 'gui-studio-library', 'aria-label': '界面草稿' },
                e('h2', null, '我的界面'),
                e('div', { className: 'gui-studio-create' }, e('select', { 'aria-label': '界面模板', value: template, onChange: function (event) { setTemplate(event.target.value); } }, e('option', { value: 'conversation' }, '整体工作空间 · 会话优先'), e('option', { value: 'tasks' }, '整体工作空间 · 任务推进'), e('option', { value: 'page' }, '个人页面 · 仅此页')), button('新建界面', create, !example)),
                lastId && items.some(function (item) { return item.id === lastId; }) ? button('继续上次编辑', function () { return load(lastId); }) : null,
                items.length ? e('div', { className: 'gui-draft-grid' }, items.map(function (item) { return button(item.document.name + ' · v' + item.revision, function () { return load(item.id); }, false, item.id); })) : e('p', null, '从模板创建界面，再调整布局或交给 Agent 设计。'),
                e('details', null, e('summary', null, '导入界面包'), e('input', { 'aria-label': '导入界面包', type: 'file', accept: '.json,application/json', disabled: !!busy, onChange: function (event) { const file = event.target.files[0]; if (!file) return; action('导入', async function () { if (file.size > 70000) throw new Error('界面包过大'); const parsed = JSON.parse(await file.text()); accept(await guiCall('import', { document: parsed.document || parsed, sessionId: await designSession() })); setView('edit'); await refresh(); }); } })),
                e('details', { className: 'gui-release-list' }, e('summary', null, '已发布版本'), releases.map(function (release) { return button(release.document.name + ' · v' + release.draftRevision, async function () { if (!guiLayout || guiLayout.workspaceVersion !== 1 || guiDefaultOnly) throw new Error('请返回正常工作区应用界面'); await activateRelease(release); }, guiDefaultOnly, release.id); })),
                button('切换为原生界面', async function () { if (!window.confirm('切换后，正常启动将使用原生界面，启动页和导航偏好会重置；草稿、发布版本和已安装个人页保留。')) return; await guiCall('restore', { baseRevision: guiState.profile.revision }); guiState.trial = null; guiClearPage(); await refresh(); guiState.returnLocation = null; guiState.open = false; guiState.notice = '已切换为原生界面，下次正常打开继续使用。'; guiNotify(); })),
              e(GuiProfileManager, { ConfirmationDialog: GuiConfirmationDialog, profile: guiState.profile, releases, busy: !!busy || guiDefaultOnly, onAction: async function (operation, args) { await guiCall(operation, args); await refresh(); }, onNavigate: function (target) { guiState.returnLocation = null; return guiNavigateTarget(target); } }))),
        editing && assistant ? e(GuiDesignConversation, { sessionId: draft.sessionId, propertiesOpen, propertiesHeight, onProperties: function () { setPropertiesOpen(!propertiesOpen); }, onResize: setPropertiesHeight, onClose: function () { setAssistant(false); } },
          e('section', {className:'gui-agent-focus', 'aria-label':'设计讨论焦点'},
            e('span', null, '讨论范围：'),
            e('button', {type:'button', 'data-gui-action':'locate-focus', title:focus.label, onClick:function(){setSelected(focus.selection);setLocateSelection(function(n){return n+1;});if(window.innerWidth<=900)setAssistant(false);}}, focus.label),
            e('button', {type:'button', 'data-gui-action':'lock-focus', 'aria-pressed':lockedFocus !== null, onClick:function(){setLockedFocus(lockedFocus === null ? focus.selection : null);}}, lockedFocus === null ? '跟随选择' : '已锁定'),
            e('button', {type:'button', onClick:function(){setLockedFocus('@workspace');}}, '整个界面'),
            e('small', {role:'status'}, !nativeFocusAvailable ? '运行时缺少设计焦点能力，请更新后发送设计要求。' : busy === '同步设计焦点' ? '保存设计中…' : dirty ? '发送前自动保存；失败会保留输入' : '修订 ' + draft.revision + ' · 发送时附带焦点'))) : null);
    }

    function GuiDesignConversation(props) {
      const ref = React.useRef(null);
      React.useLayoutEffect(function () {
        let frame;
        function measure() {
          if (!ref.current) return;
          const rect = ref.current.getBoundingClientRect();
          guiPlaceDesignConversation({ x: rect.x, y: rect.y, width: rect.width, height: rect.height, visible: rect.width > 0 && rect.height > 0 });
        }
        function schedule() { window.cancelAnimationFrame(frame); frame = window.requestAnimationFrame(measure); }
        const observer = new window.ResizeObserver(schedule); observer.observe(ref.current);
        window.addEventListener('resize', schedule); window.addEventListener('scroll', schedule, true); schedule();
        return function () { window.cancelAnimationFrame(frame); observer.disconnect(); window.removeEventListener('resize', schedule); window.removeEventListener('scroll', schedule, true); guiPlaceDesignConversation(null); };
      }, [props.sessionId]);
      return React.createElement('aside', { className: 'gui-agent-dock', 'aria-label': '设计 Agent 会话' },
        React.createElement('header', null, React.createElement('strong', null, 'Agent · 设计协作'), React.createElement('button', {type:'button', 'aria-expanded':props.propertiesOpen, onClick:props.onProperties}, props.propertiesOpen ? '收起属性' : '选区属性'), React.createElement('button', { type: 'button', onClick: props.onClose }, '收起')),
        props.propertiesOpen ? React.createElement('div', {className:'gui-agent-properties-space', style:{height:props.propertiesHeight + 'px'}}, React.createElement('input', {type:'range', min:150, max:320, value:props.propertiesHeight, 'aria-label':'属性面板高度', onChange:function(event){props.onResize(Number(event.target.value));}})) : null,
        React.createElement('div', { ref, className: 'gui-agent-seat', 'aria-hidden': true }),
        props.children);
    }
    slots.inject('shell.overlay', function () { return slots.register({ name: 'shell.overlay', id: 'h2b-gui-modules', order: 80 }, GuiModuleLayer); });
    slots.inject('shell.overlay', function () { return slots.register({ name: 'shell.overlay', id: 'h2b-gui-system', order: 90 }, GuiSystemLayer); });
    slots.inject('workspace', function () { return slots.register({ name: 'workspace', id: 'h2b-gui-workspace' }, GuiPage); });
    function GuiTopNavigation() {
      useGuiState();
      const doc = guiShellDocument(), objectPanel = guiObjectPanelConfiguration();
      return guiState.open || !doc || !doc.layout || doc.layout.navigation !== 'top' ? null : React.createElement('nav', { className: 'gui-top-navigation', 'aria-label': '个人工作空间导航' }, objectPanel ? React.createElement('button', { className: 'gui-object-toggle', 'data-gui-action': 'objects', 'aria-expanded': objectPanel.open, onClick: function () { guiToggleObjects(); } }, objectPanel.open ? '收起对象列表' : '切换对象') : null, (guiNavigationItems() || []).map(function (item) {
        return React.createElement('button', { key: item.id, onClick: function () { guiNavigate(item.feature, item.pageId, null).catch(guiShowError); } }, item.label);
      }));
    }
    slots.inject('workspace.navigation', function () { return slots.register({ name: 'workspace.navigation', id: 'h2b-gui-navigation' }, GuiTopNavigation); });
    function GuiConversationPreview(props) {
      const block = props.block;
      if (!block || block.kind !== 'tool-result') return React.createElement('p', null, '正在准备 GUI 预览…');
      let value;
      try { value = JSON.parse((block.content || []).filter(function (part) { return typeof part.text === 'string'; }).map(function (part) { return part.text; }).join('\n')); } catch (_) {}
      const draft = value && value.draft;
      if (!draft || !draft.document) return React.createElement('p', null, 'GUI 预览不可用，请查看工具返回并修正设计。');
      return React.createElement('article', { className: 'gui-conversation-preview' },
        React.createElement('strong', null, draft.document.name + ' · v' + draft.revision),
        React.createElement('p', null, '本次设计快照 · 尚未应用'),
        React.createElement('details', null, React.createElement('summary', null, '查看界面设计'),
          React.createElement(GuiBoundary, null, React.createElement(GuiWorkspace, { document: draft.document, registry: guiRegistry, renderFeature: function (feature) { return React.createElement('div', { className: 'gui-feature-reference' }, guiLabel(feature)); } }))),
        React.createElement('button', { onClick: function () { guiOpen(draft.id); } }, '打开当前草稿'));
    }
    slots.inject('tool.call.toolview', function () {
      const offPreview = slots.register({ name: 'tool.call.toolview', key: 'h2b_gui_preview' }, GuiConversationPreview);
      const offPublish = slots.register({ name: 'tool.call.toolview', key: 'h2b_gui_prepare_publish' }, GuiConversationPreview);
      return function () { offPreview(); offPublish(); };
    });
