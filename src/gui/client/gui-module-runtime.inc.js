    // Host-owned module lifetimes are independent from the authored layout tree.
    // Layout leaves reserve geometry; this flat host mounts each business view
    // once under its stable instance key. No DOM relocation, portals, or copied
    // business controllers are involved. Persistence here is browser lifetime;
    // business modules retain their existing durable state ownership.
    function createGuiModuleRuntime(options) {
      const registry = options.registry;
      const maxInstances = options.maxInstances || 64;
      const nativeViews = new Set(options.nativeViews || ['dsh.conversation/default']);
      const contextKeys = new Set(options.contextKeys || ['sessionId', 'workflowId', 'runId', 'taskId', 'targetUri']);
      const entries = new Map(), listeners = new Set(), placements = new Map();
      let revision = 0;
      let snapshot = Object.freeze({ revision, instances: Object.freeze([]) });
      function emit() {
        snapshot = Object.freeze({ revision: ++revision, instances: Object.freeze([...entries.values()]) });
        for (const listener of listeners) listener();
      }
      function normalize(input) {
        if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('Invalid GUI module instance');
        if (typeof input.instanceId !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,2047}$/.test(input.instanceId)) throw new Error('Invalid GUI module instanceId');
        const view = input.view || 'default';
        if (!registry.supports(input.feature, view)) throw new Error('Unsupported GUI module view');
        const context = {};
        if (input.context !== undefined) {
          if (!input.context || typeof input.context !== 'object' || Array.isArray(input.context)) throw new Error('Invalid GUI module context');
          for (const key of Object.keys(input.context).sort()) {
            const value = input.context[key];
            if (!contextKeys.has(key) || typeof value !== 'string' || !value.trim() || value.length > (key === 'targetUri' ? 1024 : 256)) throw new Error('Invalid GUI module context reference');
            context[key] = value;
          }
        }
        const descriptor = { instanceId: input.instanceId, feature: input.feature, view, context: Object.freeze(context) };
        return Object.freeze({ ...descriptor, external: nativeViews.has(input.feature + '/' + view), identity: JSON.stringify(descriptor) });
      }
      function rectangle(value) {
        if (!value || typeof value !== 'object') throw new Error('Invalid GUI module rectangle');
        const result = {};
        for (const key of ['x', 'y', 'width', 'height']) {
          const number = value[key];
          if (!Number.isFinite(number) || Math.abs(number) > 1000000 || (['width', 'height'].includes(key) && number < 0)) throw new Error('Invalid GUI module rectangle');
          result[key] = Math.round(number * 100) / 100;
        }
        result.visible = value.visible !== false && result.width > 0 && result.height > 0;
        if (value.clip !== undefined) {
          const clip = {};
          for (const key of ['top', 'right', 'bottom', 'left']) {
            if (!Number.isFinite(value.clip?.[key]) || value.clip[key] < 0 || value.clip[key] > 1000000) throw new Error('Invalid GUI module clipping');
            clip[key] = Math.round(value.clip[key] * 100) / 100;
          }
          result.clip = Object.freeze(clip);
        }
        return Object.freeze(result);
      }
      function reconcile(inputs) {
        if (!Array.isArray(inputs)) throw new Error('GUI module instances must be an array');
        const incoming = inputs.map(normalize);
        const activeIds = new Set();
        let nativeCount = 0;
        for (const item of incoming) {
          if (activeIds.has(item.instanceId)) throw new Error('Duplicate GUI module instanceId');
          activeIds.add(item.instanceId);
          if (item.external && ++nativeCount > 1) throw new Error('The native DSH conversation owner has one active instance');
          const previous = entries.get(item.instanceId);
          if (previous && previous.identity !== item.identity) throw new Error('GUI module context changed: use a new instanceId');
        }
        const newCount = incoming.filter(item => !entries.has(item.instanceId)).length;
        if (entries.size + newCount > maxInstances) throw new Error('GUI module instance limit reached: 模块状态过多，请先保存编辑并刷新界面');
        let changed = false;
        for (const [id, entry] of entries) {
          const active = activeIds.has(id);
          if (entry.active !== active) { entries.set(id, Object.freeze({ ...entry, active })); changed = true; }
        }
        for (const item of incoming) {
          if (!entries.has(item.instanceId)) {
            entries.set(item.instanceId, Object.freeze({ ...item, active: true, rect: null }));
            changed = true;
          }
        }
        if (changed) emit();
        return snapshot;
      }
      function place(instanceId, value) {
        const previous = entries.get(instanceId);
        if (!previous) throw new Error('Unknown GUI module instance');
        const rect = value === null ? null : rectangle(value);
        if (JSON.stringify(previous.rect) === JSON.stringify(rect)) return;
        entries.set(instanceId, Object.freeze({ ...previous, rect }));
        emit();
      }
      function close(instanceId) {
        const entry = entries.get(instanceId);
        if (!entry) return false;
        // The adapter may reject closing an editor with unsaved/pending work.
        // No asynchronous implicit discard is permitted here.
        if (options.canClose && options.canClose(entry) !== true) throw new Error('GUI module still has unsaved or pending work');
        entries.delete(instanceId); placements.delete(instanceId); emit(); return true;
      }
      function claimPlacement(instanceId, owner, priority = 0, target = null) {
        if (!entries.has(instanceId) || typeof owner !== 'string' || !owner || !Number.isSafeInteger(priority)) throw new Error('Invalid GUI placement claim');
        if (!placements.has(instanceId)) placements.set(instanceId, new Map());
        const claims = placements.get(instanceId);
        if (claims.has(owner)) throw new Error('Duplicate GUI placement owner');
        const claim = { rect: null, priority, target }; claims.set(owner, claim);
        let disposed = false;
        function resolve() {
          if (!entries.has(instanceId)) return;
          const visible = [...claims.values()].filter(item => item.rect?.visible).sort((a, b) => b.priority - a.priority);
          if (visible.length > 1 && visible[0].priority === visible[1].priority) {
            place(instanceId, null);
            throw new Error('Multiple visible placements for GUI module ' + instanceId);
          }
          place(instanceId, visible[0]?.rect || null);
        }
        return Object.freeze({
          place(value) { if (disposed) return; claim.rect = value === null ? null : rectangle(value); resolve(); },
          dispose() { if (disposed) return; disposed = true; claims.delete(owner); resolve(); }
        });
      }
      return Object.freeze({
        reconcile, place, close, claimPlacement, refresh: emit,
        getPlacementTarget(instanceId) {
          const visible = [...(placements.get(instanceId)?.values() || [])].filter(item => item.rect?.visible).sort((a, b) => b.priority - a.priority);
          return visible.length && !(visible.length > 1 && visible[0].priority === visible[1].priority) ? visible[0].target : null;
        },
        get: id => entries.get(id),
        getSnapshot: () => snapshot,
        subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); }
      });
    }

    // Only inherit declared design tokens, never arbitrary DOM styles or selectors.
    function readGuiModulePresentation(target) {
      if (!target || typeof guiStyleVariables !== 'function') return { style: {}, theme: null };
      const scope = target.closest?.('[data-gui-theme]');
      const computed = target.ownerDocument?.defaultView?.getComputedStyle(target);
      let variables = {};
      if (computed) for (const key of Object.keys(guiStyleVariables({}))) {
        const value = computed.getPropertyValue(key).trim();
        if (value) variables[key] = value;
      }
      let theme = null;
      try { if (scope) theme = JSON.parse(scope.getAttribute('data-gui-theme')); } catch (_) {}
      const appearanceChain = [];
      for (let cursor = target; cursor; cursor = cursor.parentElement) {
        try { const raw = cursor.getAttribute?.('data-gui-appearance'); if (raw) appearanceChain.unshift(JSON.parse(raw)); } catch (_) {}
        if (cursor === scope) break;
      }
      let appearance = null;
      try { const owner = target.closest?.('[data-gui-appearance]'); if (owner) appearance = JSON.parse(owner.getAttribute('data-gui-appearance')); } catch (_) {}
      let appearanceStyle = {};
      if (appearance && Object.keys(variables).length) {
        appearanceStyle = guiAppearanceStyle(appearance); delete appearanceStyle.padding;
      }
      return { style: Object.keys(variables).length ? { ...variables, ...guiStyleAliases(variables), ...appearanceStyle } : {},
        theme, appearance, appearanceChain, mode: scope?.getAttribute('data-gui-style-mode') || 'light' };
    }

    function createGuiModuleHost(React) {
      const h = React.createElement;
      class ModuleBoundary extends React.Component {
        constructor(props) { super(props); this.state = { error: null }; }
        static getDerivedStateFromError(error) { return { error }; }
        render() {
          return this.state.error ? h('p', { role: 'alert' }, '模块暂不可用。请先保存其他编辑，再使用系统栏“默认启动”重新加载。') : this.props.children;
        }
      }
      function ModuleContent({ instance, renderModule }) { return renderModule(instance); }
      function ModuleSurface({ instance, renderModule, runtime }) {
        const surface = React.useRef(null);
        React.useLayoutEffect(() => bindGuiModuleScrollBridge(surface.current, () => runtime.getPlacementTarget(instance.instanceId)), [runtime, instance.instanceId]);
        const presentation = readGuiModulePresentation(runtime.getPlacementTarget(instance.instanceId));
        const visible = instance.active && instance.rect && instance.rect.visible;
        const rect = instance.rect || { x: 0, y: 0, width: 0, height: 0 };
        return h('section', {
          ref: surface,
          'data-gui-module-instance': instance.instanceId,
          'data-gui-module-feature': instance.feature,
          'data-gui-styled': Object.keys(presentation.style).length ? 'true' : undefined,
          hidden: !visible,
          style: { ...presentation.style, position: 'absolute', left: rect.x, top: rect.y, width: rect.width, height: rect.height,
            minWidth: 0, minHeight: 0, overflow: 'auto', display: visible ? 'flex' : 'none', flexDirection: 'column', pointerEvents: 'auto',
            clipPath: rect.clip ? `inset(${rect.clip.top}px ${rect.clip.right}px ${rect.clip.bottom}px ${rect.clip.left}px)` : undefined }
        }, h(ModuleBoundary, null, h(ModuleContent, { instance, renderModule })));
      }
      return function GuiModuleHost({ runtime, renderModule, onNativePlacement, className, hostRef }) {
        const snapshot = React.useSyncExternalStore(runtime.subscribe, runtime.getSnapshot, runtime.getSnapshot);
        const native = snapshot.instances.find(instance => instance.active && instance.external);
        const nativeRect = native && native.rect && native.rect.visible ? native.rect : null;
        const nativePresentation = JSON.stringify(readGuiModulePresentation(native ? runtime.getPlacementTarget(native.instanceId) : null));
        React.useLayoutEffect(() => {
          onNativePlacement?.(nativeRect, native || null);
        }, [onNativePlacement, nativeRect, native?.instanceId, nativePresentation]);
        React.useLayoutEffect(() => () => { onNativePlacement?.(null, null); }, [onNativePlacement]);
        // Only the host is allowed to unmount these siblings, via close(). A
        // different layout document/page changes active/rect, not their keys.
        return h('div', { ref: hostRef, className, 'data-gui-module-host': true,
          style: { position: 'fixed', inset: 0, pointerEvents: 'none' } },
          snapshot.instances.filter(instance => !instance.external).map(instance =>
            h(ModuleSurface, { key: instance.instanceId, instance, renderModule, runtime })));
      };
    }

    // A fixed surface has no authored scroll ancestors. Forward only residual
    // scrolling to the winning placement's ancestors; leave native internal
    // scrolling, zoom, controls and explicitly contained scrolling alone.
    function bindGuiModuleScrollBridge(surface, getPlaceholder, environment) {
      if (!surface) return () => {};
      const env = environment || surface.ownerDocument.defaultView;
      function plan(start, stop, axis, delta) {
        const changes = [];
        const position = axis === 'x' ? 'scrollLeft' : 'scrollTop';
        const size = axis === 'x' ? 'clientWidth' : 'clientHeight';
        const extent = axis === 'x' ? 'scrollWidth' : 'scrollHeight';
        const overflow = axis === 'x' ? 'overflowX' : 'overflowY';
        const overscroll = axis === 'x' ? 'overscrollBehaviorX' : 'overscrollBehaviorY';
        for (let node = start; node && delta; node = node === stop ? null : node.parentElement) {
          const style = env.getComputedStyle(node);
          if (['auto', 'scroll', 'overlay'].includes(style[overflow]) || node === node.ownerDocument?.scrollingElement) {
            // Negative RTL scrollLeft is intentionally left to the browser.
            if (axis === 'x' && style.direction === 'rtl') return { changes, remaining: 0 };
            const before = node[position], maximum = Math.max(0, node[extent] - node[size]);
            const after = Math.max(0, Math.min(maximum, before + delta));
            if (after !== before) { changes.push({ node, position, value: after }); delta -= after - before; }
            if (['contain', 'none'].includes(style[overscroll])) return { changes, remaining: 0 };
          }
        }
        return { changes, remaining: delta };
      }
      function forward(event, dx, dy) {
        if (event.defaultPrevented || !event.cancelable || event.ctrlKey || event.metaKey) return;
        const placeholder = getPlaceholder();
        if (!placeholder?.isConnected) return;
        const target = event.target?.nodeType === 1 ? event.target : event.target?.parentElement;
        if (!target || !surface.contains(target)) return;
        if (target.closest?.('select, input[type="range"], input[type="number"]')) return;
        const changes = [];
        let forwarded = false;
        for (const [axis, delta] of [['x', dx], ['y', dy]]) {
          if (!Number.isFinite(delta) || !delta) continue;
          const internal = plan(target, surface, axis, delta);
          const external = plan(placeholder.parentElement, null, axis, internal.remaining);
          changes.push(...internal.changes, ...external.changes);
          forwarded ||= external.changes.length > 0;
        }
        if (!forwarded) return;
        // Cancelling a wheel event cancels both axes. Apply the planned internal
        // portion too, so diagonal/partially consumed movement is not lost.
        event.preventDefault();
        for (const change of changes) change.node[change.position] = change.value;
      }
      function wheel(event) {
        const factor = event.deltaMode === 1 ? (parseFloat(env.getComputedStyle(surface).lineHeight) || 16) : event.deltaMode === 2 ? surface.clientHeight : 1;
        forward(event, event.deltaX * factor, event.deltaY * factor);
      }
      let touch = null;
      function touchStart(event) {
        touch = event.touches.length === 1 ? { id: event.touches[0].identifier, x: event.touches[0].clientX, y: event.touches[0].clientY } : null;
      }
      function touchMove(event) {
        if (!touch || event.touches.length !== 1 || event.touches[0].identifier !== touch.id) { touch = null; return; }
        const point = event.touches[0], dx = touch.x - point.clientX, dy = touch.y - point.clientY;
        touch = { id: point.identifier, x: point.clientX, y: point.clientY };
        forward(event, dx, dy);
      }
      function touchEnd() { touch = null; }
      // Single-finger boundary swipes are forwarded while cancelable; native
      // internal scrolling and pinch zoom stay native. Once a browser commits
      // a native gesture it may make touchmove noncancelable: a fresh swipe at
      // the boundary then scrolls the page. No synthetic momentum is invented.
      surface.addEventListener('wheel', wheel, { passive: false });
      surface.addEventListener('touchstart', touchStart, { passive: true });
      surface.addEventListener('touchmove', touchMove, { passive: false });
      surface.addEventListener('touchend', touchEnd, { passive: true });
      surface.addEventListener('touchcancel', touchEnd, { passive: true });
      return () => {
        surface.removeEventListener('wheel', wheel);
        surface.removeEventListener('touchstart', touchStart);
        surface.removeEventListener('touchmove', touchMove);
        surface.removeEventListener('touchend', touchEnd);
        surface.removeEventListener('touchcancel', touchEnd);
      };
    }

    // Observe an EMPTY declarative leaf, never the live business subtree. All
    // geometry is viewport-relative for the fixed module host. Captured scroll
    // events cover nested layout scrollers; ResizeObserver covers rearrangement.
    let guiPlacementSequence = 0;
    function observeGuiModulePlacement(runtime, instanceId, placeholder, host, environment, options) {
      const env = environment || window;
      const settings = options || {};
      const claim = runtime.claimPlacement(instanceId, settings.owner || 'placeholder-' + (++guiPlacementSequence), settings.priority ?? 10, placeholder);
      let frame = null, disposed = false, presentationKey = '';
      function measure() {
        frame = null;
        if (disposed || !runtime.get(instanceId)) return;
        if (!placeholder.isConnected || !host.isConnected || placeholder.getClientRects().length === 0) {
          claim.place(null); return;
        }
        const rect = placeholder.getBoundingClientRect();
        let left = Math.max(0, rect.left), top = Math.max(0, rect.top);
        let right = Math.min(env.innerWidth, rect.right), bottom = Math.min(env.innerHeight, rect.bottom);
        if (env.getComputedStyle) {
          for (let ancestor = placeholder.parentElement; ancestor; ancestor = ancestor.parentElement) {
            const style = env.getComputedStyle(ancestor);
            const bounds = ancestor.getBoundingClientRect();
            if (['auto', 'scroll', 'hidden', 'clip'].includes(style.overflowX)) {
              left = Math.max(left, bounds.left + ancestor.clientLeft);
              right = Math.min(right, bounds.left + ancestor.clientLeft + ancestor.clientWidth);
            }
            if (['auto', 'scroll', 'hidden', 'clip'].includes(style.overflowY)) {
              top = Math.max(top, bounds.top + ancestor.clientTop);
              bottom = Math.min(bottom, bounds.top + ancestor.clientTop + ancestor.clientHeight);
            }
          }
        }
        const nextPresentation = JSON.stringify(readGuiModulePresentation(placeholder));
        const presentationChanged = nextPresentation !== presentationKey; presentationKey = nextPresentation;
        const visible = right > left && bottom > top;
        claim.place({ x: rect.left, y: rect.top, width: rect.width, height: rect.height, visible,
          clip: { top: Math.max(0, top - rect.top), right: Math.max(0, rect.right - right),
            bottom: Math.max(0, rect.bottom - bottom), left: Math.max(0, left - rect.left) } });
        if (presentationChanged) runtime.refresh();
      }
      function schedule() { if (!disposed && frame === null) frame = env.requestAnimationFrame(measure); }
      const observer = new env.ResizeObserver(schedule);
      observer.observe(placeholder); observer.observe(host);
      // ResizeObserver alone misses equal-sized nodes reordered in a grid.
      const mutation = env.MutationObserver ? new env.MutationObserver(schedule) : null;
      const layoutRoot = settings.layoutRoot || placeholder.closest?.('.h2bgui-workspace') || placeholder.parentElement;
      if (layoutRoot) mutation?.observe(layoutRoot, { subtree: true, childList: true, attributes: true, attributeFilter: ['style', 'class', 'hidden', 'data-gui-theme', 'data-gui-style-mode', 'data-gui-appearance'] });
      env.addEventListener('resize', schedule);
      env.addEventListener('scroll', schedule, true);
      schedule();
      return () => {
        disposed = true;
        observer.disconnect();
        mutation?.disconnect();
        env.removeEventListener('resize', schedule);
        env.removeEventListener('scroll', schedule, true);
        if (frame !== null) env.cancelAnimationFrame(frame);
        claim.dispose();
      };
    }
