    // Presentation adapters use the existing session and directory authorities.
    function createGuiSessionLibrary(React, dependencies) {
      return function GuiSessionLibrary(props) {
        const [, update] = React.useState(0);
        const [query, setQuery] = React.useState('');
        const [error, setError] = React.useState('');
        React.useEffect(() => dependencies.subscribe(() => update(n => n + 1)), []);
        const snapshot = dependencies.read();
        const archived = new Set(snapshot.archived || []);
        const rows = snapshot.sessions.filter(row => !archived.has(row.id) && dependencies.eligible(row) &&
          dependencies.direct(row.id) === Boolean(props.direct));
        const matched = rows.filter(row => !query || String(row.displayTitle || row.id).toLowerCase().includes(query.toLowerCase()));
        const h = React.createElement;
        return h('section', { className: 'gui-session-library', 'aria-label': props.direct ? '直聊会话列表' : 'Agent 会话列表' },
          h('header', null, h('strong', null, props.direct ? 'H2B 直聊' : 'Agent 会话'), h('span', null, rows.length + ' 个会话')),
          props.view === 'summary' ? h('p', null, '当前会话：' + (rows.find(row => row.id === snapshot.current)?.displayTitle || '未选择')) : null,
          h('label', null, '查找会话', h('input', { value: query, onChange: event => setQuery(event.target.value) })),
          error ? h('p', { role: 'alert' }, error) : null,
          matched.map(row => h('button', { key: row.id, 'aria-current': row.id === snapshot.current ? 'page' : undefined,
            onClick: () => { setError(''); Promise.resolve().then(() => props.onSelect ? props.onSelect(row.id) : dependencies.open(row.id)).catch(err => setError(err.message || String(err))); } }, row.displayTitle || row.id)),
          !matched.length ? h('p', null, '没有符合条件的会话') : null);
      };
    }

    function createGuiContactBrowser(React, dependencies) {
      return function GuiContactBrowser(props) {
        const h = React.createElement;
        const [rows, setRows] = React.useState([]), [error, setError] = React.useState('');
        const [query, setQuery] = React.useState(''), [selected, setSelected] = React.useState(props.context?.targetUri || '');
        const [revision, refresh] = React.useState(0);
        const [, update] = React.useState(0);
        React.useEffect(() => dependencies.subscribe(() => update(n => n + 1)), []);
        React.useEffect(() => {
          if (props.visible === false) return;
          let active = true, reading = false;
          async function read() {
            if (reading) return; reading = true;
            try {
              const result = await dependencies.read();
              if (!Array.isArray(result)) throw new Error('通讯录返回格式不兼容');
              if (active) { setRows(result.filter(dependencies.eligible)); setError(''); }
            } catch (err) { if (active) setError(err.message || String(err)); }
            finally { reading = false; }
          }
          read(); const timer = setInterval(read, 10000);
          return () => { active = false; clearInterval(timer); };
        }, [revision, props.visible]);
        const native = props.instanceId?.startsWith('native:');
        const target = native ? dependencies.selected()?.targetUri : selected;
        const contact = rows.find(row => row.targetUri === target) || null;
        const filtered = rows.filter(row => !query || [row.targetUri, dependencies.label(row)].some(value => String(value).toLowerCase().includes(query.toLowerCase())));
        return h('section', { className: 'gui-contact-browser', 'data-view': props.view || 'default', 'data-native': native ? 'true' : undefined, 'aria-label': '个人通讯录模块' },
          native && props.view === 'detail' ? null : h('header', null, h('strong', null, '通讯录'), h('button', { onClick: () => refresh(n => n + 1) }, '刷新通讯录')),
          error ? h('p', { role: 'alert' }, error) : null,
          h('div', { className: 'gui-contact-content' }, props.view !== 'detail' ? h('div', { className: 'gui-contact-library' },
            h('label', null, '查找联系人', h('input', { value: query, onChange: event => setQuery(event.target.value) })),
            filtered.map(row => h('button', { key: row.targetUri, 'aria-pressed': target === row.targetUri,
              onClick: () => { setSelected(row.targetUri); if (native) dependencies.select(row); } }, dependencies.label(row), h('small', null, row.targetUri))),
            !filtered.length ? h('p', null, '暂无可用联系人') : null) : null,
          props.view !== 'list' ? h('div', { className: 'gui-contact-detail-pane' }, dependencies.renderDetail(contact)) : null));
      };
    }
