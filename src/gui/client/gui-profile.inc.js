    // User-owned presentation preferences. No Host calls or writes during render.
    const GUI_PROFILE_FEATURES = Object.freeze([
      { id: 'dsh.conversation', label: 'Agent 会话' }, { id: 'h2b.directChat', label: 'H2B 直聊' },
      { id: 'h2b.contacts', label: '通讯录' }, { id: 'h2b.workflow', label: 'Workflow' },
      { id: 'h2b.kanban', label: '任务看板' }, { id: 'h2b.routine', label: 'Routine' },
      { id: 'h2b.operations', label: '运维' }
    ]);
    function guiProfileReleaseMap(profile, releases) {
      const result = new Map();
      [...(releases || []), ...(profile?.pages || []), ...(profile?.release ? [profile.release] : [])].forEach(function (release) {
        if (release && typeof release.id === 'string' && release.document && Array.isArray(release.document.pages)) result.set(release.id, release);
      });
      return result;
    }
    function guiProfileTargetKey(target) {
      if (!target || typeof target !== 'object' || Array.isArray(target)) return null;
      if (Object.prototype.hasOwnProperty.call(target, 'feature') && typeof target.feature === 'string' && Object.keys(target).length === 1 && GUI_PROFILE_FEATURES.some(function (item) { return item.id === target.feature; })) return 'feature:' + target.feature;
      if (Object.prototype.hasOwnProperty.call(target, 'releaseId') && Object.prototype.hasOwnProperty.call(target, 'pageId') && Object.keys(target).length === 2 && typeof target.releaseId === 'string' && typeof target.pageId === 'string' && target.releaseId && target.pageId) return 'page:' + target.releaseId + '/' + target.pageId;
      return null;
    }
    function guiProfileTargetAvailable(profile, releases, target) {
      const key = guiProfileTargetKey(target);
      if (!key) return false;
      if (target.feature) return true;
      if (target.releaseId !== profile?.releaseId && !(profile?.pageReleaseIds || []).includes(target.releaseId)) return false;
      const release = guiProfileReleaseMap(profile, releases).get(target.releaseId);
      return Boolean(release && release.document.pages.some(function (page) { return page.id === target.pageId; }));
    }
    function guiProfileTargets(profile, releases) {
      const result = GUI_PROFILE_FEATURES.map(function (item) { return { key: 'feature:' + item.id, label: item.label, target: { feature: item.id } }; });
      const map = guiProfileReleaseMap(profile, releases);
      const ids = new Set([profile?.releaseId, ...(profile?.pageReleaseIds || [])].filter(Boolean));
      ids.forEach(function (id) {
        const release = map.get(id);
        if (release) release.document.pages.forEach(function (page) {
          const target = { releaseId: id, pageId: page.id };
          result.push({ key: guiProfileTargetKey(target), label: release.document.name + ' / ' + page.title, target });
        });
      });
      return result;
    }
    function guiProfileNavigation(profile) {
      const known = GUI_PROFILE_FEATURES.map(function (item) { return item.id; });
      const configured = profile?.navigation || {};
      return {
        orderedFeatures: [...new Set([...(configured.orderedFeatures || []), ...known])].filter(function (id) { return known.includes(id); }),
        hiddenFeatures: [...new Set(configured.hiddenFeatures || [])].filter(function (id) { return known.includes(id); })
      };
    }
    function guiProfileOperation(profile, releases, intent, value) {
      if (!profile || !Number.isSafeInteger(profile.revision) || profile.revision < 0) throw new Error('请等待个人配置加载后重试');
      const base = { baseRevision: profile.revision };
      if (intent === 'install' || intent === 'remove') {
        const release = guiProfileReleaseMap(profile, releases).get(value);
        if (!release || release.document.kind !== 'page') throw new Error('所选版本不是可安装的个人页面');
        const installed = (profile.pageReleaseIds || []).includes(value);
        if (intent === 'install' && installed) throw new Error('该页面版本已经安装');
        if (intent === 'remove' && !installed) throw new Error('该页面版本已经移除，请刷新列表');
        return { operation: intent === 'install' ? 'install-page' : 'remove-page', args: { ...base, releaseId: value } };
      }
      if (intent === 'home') {
        if (value !== null && !guiProfileTargetAvailable(profile, releases, value)) throw new Error('启动页目标已不可用，请重新选择');
        return { operation: 'configure-profile', args: { ...base, home: value } };
      }
      if (intent === 'favorite') {
        if (!guiProfileTargetAvailable(profile, releases, value)) throw new Error('收藏目标已不可用，请重新选择');
        const key = guiProfileTargetKey(value), seen = new Set();
        const favorites = (profile.favorites || []).filter(function (target) {
          const id = guiProfileTargetKey(target);
          if (!guiProfileTargetAvailable(profile, releases, target) || seen.has(id)) return false;
          seen.add(id); return true;
        });
        const exists = favorites.some(function (target) { return guiProfileTargetKey(target) === key; });
        if (!exists && favorites.length >= 24) throw new Error('最多收藏 24 个入口，请先取消一个收藏');
        return { operation: 'configure-profile', args: { ...base, favorites: exists ? favorites.filter(function (target) { return guiProfileTargetKey(target) !== key; }) : [...favorites, value] } };
      }
      const navigation = guiProfileNavigation(profile);
      if (!value || !GUI_PROFILE_FEATURES.some(function (item) { return item.id === value.feature; })) throw new Error('系统必要入口不能通过个人导航配置隐藏或排序');
      if (intent === 'visibility') navigation.hiddenFeatures = value.hidden ? [...new Set([...navigation.hiddenFeatures, value.feature])] : navigation.hiddenFeatures.filter(function (id) { return id !== value.feature; });
      else if (intent === 'move') {
        if (![1, -1].includes(value.direction)) throw new Error('无效的排序方向');
        const index = navigation.orderedFeatures.indexOf(value.feature), next = index + value.direction;
        if (next < 0 || next >= navigation.orderedFeatures.length) throw new Error('已经到达导航边界');
        [navigation.orderedFeatures[index], navigation.orderedFeatures[next]] = [navigation.orderedFeatures[next], navigation.orderedFeatures[index]];
      } else throw new Error('未知的个人配置操作');
      return { operation: 'configure-profile', args: { ...base, navigation } };
    }
    function createGuiProfileManager(React) {
      const h = React.createElement;
      return function GuiProfileManager({ profile, releases = [], onAction, onNavigate, busy = false, ConfirmationDialog }) {
        const [pending, setPending] = React.useState(false), [error, setError] = React.useState('');
        const [homeChoice, setHomeChoice] = React.useState('');
        const [removal, setRemoval] = React.useState(null);
        const profileRef = React.useRef(profile); profileRef.current = profile;
        React.useEffect(() => { setRemoval(null); }, [profile?.revision]);
        const lock = React.useRef(false);
        const disabled = busy || pending || !profile;
        const targets = guiProfileTargets(profile, releases), navigation = guiProfileNavigation(profile);
        const map = guiProfileReleaseMap(profile, releases);
        const activeShell = map.get(profile?.releaseId)?.document?.kind === 'shell' ? map.get(profile.releaseId) : null;
        const installedIds = profile?.pageReleaseIds || [];
        const installed = installedIds.map(function (id) { return map.get(id) || { id, document: null }; });
        const available = [...map.values()].filter(function (release) { return release.document.kind === 'page' && !installedIds.includes(release.id); });
        const homeKey = guiProfileTargetKey(profile?.home);
        const currentHome = targets.find(function (item) { return item.key === homeKey; });
        const choice = targets.find(function (item) { return item.key === homeChoice; }) || currentHome || targets[0];
        async function run(intent, value) {
          if (disabled || lock.current) return;
          lock.current = true; setPending(true); setError('');
          try { const action = guiProfileOperation(profile, releases, intent, value); await onAction(action.operation, action.args); }
          catch (err) { setError(err.message || String(err)); }
          finally { lock.current = false; setPending(false); }
        }
        function button(label, fn, extraDisabled) { return h('button', { type: 'button', disabled: disabled || extraDisabled, onClick: fn }, label); }
        function go(target) {
          if (disabled || !onNavigate || !guiProfileTargetAvailable(profile, releases, target)) return;
          Promise.resolve().then(function () { return onNavigate(target); }).catch(function (err) { setError(err.message || String(err)); });
        }
        return h('section', { className: 'gui-profile-manager', 'aria-label': '个人页面与导航管理' },
          removal && ConfirmationDialog ? h(ConfirmationDialog, { title: '移除已安装页面', confirmLabel: '确认移除', message: removal.message, onCancel: () => setRemoval(null), onConfirm: () => {
            const selected = removal; setRemoval(null);
            if (profileRef.current?.revision !== selected.revision) { setError('配置已更新，请重新核对要移除的页面。'); return; }
            run('remove', selected.releaseId);
          } }) : null,
          h('h3', null, '个人页面库'),
          error ? h('p', { role: 'alert' }, error) : null,
          pending ? h('p', { role: 'status' }, '正在保存个人配置…') : null,
          h('p', null, '个人页面只增加一个内容页，不替换整个界面。移除页面不会删除发布版本，也不会停用当前整体界面。'),
          installed.length ? h('ul', null, installed.map(function (release) { return h('li', { key: release.id },
            h('strong', null, release.document ? release.document.name + ' · v' + release.draftRevision : '页面版本暂不可用'),
            release.document?.id && release.document.id === activeShell?.document.id ? h('small', null, '同一设计的旧个人页面版本 · 与当前启用的整体界面独立') : null,
            release.document ? release.document.pages.map(function (page) { const target = { releaseId: release.id, pageId: page.id }; return h('span', { key: page.id }, button('打开 ' + page.title, function () { go(target); }, !onNavigate)); }) : null,
            button('移除页面', function () { setRemoval({ releaseId: release.id, revision: profile.revision, message: '从个人页面库移除“' + release.document.name + '” · v' + release.draftRevision + '。指向该版本的启动页设置和收藏将被清理；已发布版本、草稿和业务数据保留。此操作立即影响个人配置，不属于草稿编辑。' }); }, !release.document)); })) : h('p', null, '尚未安装个人页面。'),
          h('details', null, h('summary', null, '安装已发布页面'), available.length ? available.map(function (release) { return h('div', { key: release.id }, h('span', null, release.document.name + ' · v' + release.draftRevision), button('安装页面', function () { run('install', release.id); })); }) : h('p', null, '没有尚未安装的页面版本。请先发布一个个人页面。')),
          h('h3', null, '启动页'), h('p', null, '启动页决定正常打开 GUI 时首先显示的内容，不改变当前启用的界面。'), h('p', null, '当前启动页：' + (currentHome ? currentHome.label : '随当前界面启动')),
          h('label', null, '启动页目标', h('select', { value: choice?.key || '', disabled, onChange: function (event) { setHomeChoice(event.target.value); } }, targets.map(function (item) { return h('option', { key: item.key, value: item.key }, item.label); }))),
          button('设置启动页', function () { if (choice) run('home', choice.target); }, !choice),
          button('跟随当前界面启动', function () { run('home', null); }, !profile?.home),
          h('h3', null, '常用收藏'), h('ul', null, targets.map(function (item) {
            const favorite = (profile?.favorites || []).some(function (target) { return guiProfileTargetKey(target) === item.key; });
            return h('li', { key: item.key }, button(item.label, function () { go(item.target); }, !onNavigate), h('button', { type: 'button', disabled, 'aria-pressed': favorite, 'aria-label': (favorite ? '取消收藏 ' : '收藏 ') + item.label, onClick: function () { run('favorite', item.target); } }, favorite ? '取消收藏' : '收藏'));
          })),
          h('h3', null, activeShell ? '当前界面导航' : '导航顺序与显示'), h('p', null, activeShell ? '导航由当前界面设计统一管理。通过“编辑当前界面”进入“主导航菜单”，修改名称、顺序与入口；发布并启用后生效。个人导航偏好不会覆盖整体界面的设计。' : '此处调整原生界面的业务导航。工作空间管理、安全打开原生界面和恢复入口始终保留。'),
          activeShell ? null : h('ol', null, navigation.orderedFeatures.map(function (id, index) {
            const feature = GUI_PROFILE_FEATURES.find(function (item) { return item.id === id; });
            return h('li', { key: id }, h('label', null, h('input', { type: 'checkbox', checked: !navigation.hiddenFeatures.includes(id), disabled, onChange: function (event) { run('visibility', { feature: id, hidden: !event.target.checked }); } }), feature.label),
              button('上移 ' + feature.label, function () { run('move', { feature: id, direction: -1 }); }, index === 0), button('下移 ' + feature.label, function () { run('move', { feature: id, direction: 1 }); }, index === navigation.orderedFeatures.length - 1));
          })));
      };
    }
