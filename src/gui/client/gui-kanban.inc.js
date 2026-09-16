    // Read-only views over the existing Kanban bridge's board/export responses.
    // TaskWarrior UUID is the only task identity. No title or numeric-ID matching.
    function guiKanbanTaskId(value) {
      if (typeof value !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) throw new Error('任务标识必须是 Kanban 原始 UUID');
      return value.toLowerCase();
    }
    function guiKanbanEnvelope(value, operation) {
      if (!['board', 'export'].includes(operation)) throw new Error('不支持的 Kanban 只读操作');
      if (!value || value.ok !== true || value.operation !== operation || !Array.isArray(value.tasks) || !Number.isSafeInteger(value.taskCount) || value.taskCount !== value.tasks.length) throw new Error('Kanban 任务响应不兼容，请检查看板版本');
      if (value.filtered !== (operation === 'board') || JSON.stringify(value).length > 4 * 1024 * 1024) throw new Error('Kanban 任务响应范围或大小不兼容');
      const seen = new Set();
      value.tasks.forEach(function (task) {
        if (!task || typeof task !== 'object' || Array.isArray(task)) throw new Error('Kanban 返回了无效任务');
        const id = guiKanbanTaskId(task.uuid);
        if (seen.has(id)) throw new Error('Kanban 返回重复的任务 UUID');
        seen.add(id);
      });
      return { tasks: value.tasks, filtered: value.filtered, filter: typeof value.filter === 'string' ? value.filter : '', taskCount: value.taskCount };
    }
    function guiKanbanSelection(tasks, context, allowWholeBoard) {
      if (context?.taskId) {
        const id = guiKanbanTaskId(context.taskId);
        const task = tasks.find(function (item) { return guiKanbanTaskId(item.uuid) === id; });
        return task ? { state: 'detail', task, tasks: [task] } : { state: 'missing', tasks: [], taskId: id };
      }
      if (!allowWholeBoard && (context?.workflowId || context?.runId)) return { state: 'unlinked', tasks: [] };
      return { state: 'tasks', tasks };
    }
    function guiKanbanReadRequest(view, context, sessionId, allowWholeBoard) {
      if (!['tasks', 'detail'].includes(view)) throw new Error('不支持的 Kanban 数据视图');
      if (!context?.taskId && !allowWholeBoard && (context?.workflowId || context?.runId)) return null;
      if (view === 'detail' && !context?.taskId && !allowWholeBoard) return null;
      if (context?.taskId) guiKanbanTaskId(context.taskId);
      const session = sessionId || context?.sessionId;
      if (typeof session !== 'string' || !session.trim() || session.length > 256 || /[\0\r\n]/.test(session)) throw new Error('请先选择一个工作会话，再读取 Kanban');
      return { operation: context?.taskId ? 'export' : 'board', sessionId: session };
    }
    function createGuiKanbanView(React) {
      const h = React.createElement;
      function text(value) { return value === undefined || value === null || value === '' ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value); }
      return function GuiKanbanView({ view = 'tasks', context = {}, sessionId, call, onTaskSelect }) {
        const [response, setResponse] = React.useState(null), [error, setError] = React.useState(''), [loading, setLoading] = React.useState(false);
        const [selected, setSelected] = React.useState(null), [query, setQuery] = React.useState(''), [page, setPage] = React.useState(0), [reload, setReload] = React.useState(0), [wholeBoard, setWholeBoard] = React.useState(false);
        const contextKey = JSON.stringify([context.sessionId || '', context.taskId || '', context.workflowId || '', context.runId || '', sessionId || '', view]);
        const [selectionOwner, setSelectionOwner] = React.useState(contextKey);
        const effectiveSelected = selectionOwner === contextKey ? selected : null;
        const effectiveWholeBoard = selectionOwner === contextKey && wholeBoard;
        const effectiveContext = effectiveSelected ? { ...context, taskId: effectiveSelected } : context;
        const requestKey = contextKey + ':' + (effectiveSelected || '') + ':' + effectiveWholeBoard;
        React.useEffect(function () { setSelected(null); setWholeBoard(false); setSelectionOwner(contextKey); setPage(0); setQuery(''); }, [contextKey]);
        React.useEffect(function () {
          let active = true;
          setResponse(null); setError('');
          let request;
          try { request = guiKanbanReadRequest(view, effectiveContext, sessionId, effectiveWholeBoard); }
          catch (err) { setError(err.message); setLoading(false); return function () { active = false; }; }
          if (!request) { setLoading(false); return function () { active = false; }; }
          setLoading(true);
          Promise.resolve().then(function () { return call('h2b-kanban-rpc', request); }).then(function (value) {
            const parsed = guiKanbanEnvelope(value, request.operation);
            if (active) setResponse({ key: requestKey, ...parsed });
          }).catch(function (err) { if (active) setError(err.message || String(err)); }).finally(function () { if (active) setLoading(false); });
          return function () { active = false; };
        }, [requestKey, reload, call]);
        const current = response?.key === requestKey ? response : null;
        let selection;
        try { selection = guiKanbanSelection(current?.tasks || [], effectiveContext, effectiveWholeBoard); } catch (err) { selection = { state: 'invalid', tasks: [] }; }
        const filtered = selection.tasks.filter(function (task) { const needle = query.trim().toLowerCase(); return !needle || [task.uuid, task.description, task.project, task.owner].some(function (value) { return String(value || '').toLowerCase().includes(needle); }); });
        const currentPage = Math.min(page, Math.max(0, Math.ceil(filtered.length / 50) - 1));
        function open(task) { const taskId = guiKanbanTaskId(task.uuid); setSelectionOwner(contextKey); setSelected(taskId); setPage(0); if (onTaskSelect) Promise.resolve().then(function () { return onTaskSelect({ ...(context.sessionId ? { sessionId: context.sessionId } : {}), taskId }); }).catch(function (err) { setError(err.message || String(err)); }); }
        return h('section', { className: 'gui-kanban-data', 'aria-label': view === 'detail' || effectiveContext.taskId ? 'Kanban 任务详情' : 'Kanban 任务列表' },
          h('header', null, h('strong', null, 'Kanban · 只读任务数据'), h('button', { type: 'button', disabled: loading, onClick: function () { setReload(function (value) { return value + 1; }); } }, '刷新任务')),
          context.workflowId || context.runId ? h('p', { className: 'gui-kanban-context' }, '导航上下文（不是任务关联记录）：', context.workflowId ? 'Workflow ' + context.workflowId + ' ' : '', context.runId ? 'Run ' + context.runId : '') : null,
          loading ? h('p', { role: 'status' }, '正在读取 Kanban…') : null,
          error ? h('p', { role: 'alert' }, error) : null,
          selection.state === 'unlinked' ? h('div', null, h('p', null, '此 Workflow / Run 尚无已记录的 Kanban 任务 UUID 关联。'), h('button', { type: 'button', onClick: function () { setSelectionOwner(contextKey); setWholeBoard(true); } }, '查看本机看板（不按 Workflow 筛选）')) : null,
          view === 'detail' && !effectiveContext.taskId && !effectiveWholeBoard && selection.state !== 'unlinked' ? h('p', null, '请选择具有稳定 UUID 的任务以查看详情。') : null,
          current && selection.state === 'missing' ? h('p', { role: 'status' }, '当前任务库中找不到 UUID：' + selection.taskId + '。未按标题替换成其他任务。') : null,
          current && selection.state === 'detail' ? h('article', { 'data-task-id': selection.task.uuid },
            effectiveSelected ? h('button', { type: 'button', onClick: function () { setSelected(null); } }, '返回任务列表') : null,
            h('h3', null, text(selection.task.description)), h('code', null, selection.task.uuid),
            h('dl', null, ['status', 'stage', 'project', 'owner', 'handoff', 'body', 'items', 'artifact', 'depends', 'entry', 'modified', 'due', 'end'].map(function (key) { return h(React.Fragment, { key }, h('dt', null, key), h('dd', null, text(selection.task[key]))); })),
            h('details', null, h('summary', null, '查看原始任务字段'), h('pre', null, JSON.stringify(selection.task, null, 2)))) : null,
          current && selection.state === 'tasks' ? h(React.Fragment, null,
            h('p', null, '沿用 Kanban 看板过滤：' + (current.filter || '由 Kanban 提供') + ' · ' + current.taskCount + ' 项'),
            effectiveWholeBoard && (context.workflowId || context.runId) ? h('p', null, '当前是本机看板，不表示这些任务与上述 Workflow / Run 已关联。') : null,
            h('label', null, '搜索任务', h('input', { type: 'search', value: query, onChange: function (event) { setQuery(event.target.value); setPage(0); } })),
            filtered.length ? h('ul', null, filtered.slice(currentPage * 50, currentPage * 50 + 50).map(function (task) { return h('li', { key: task.uuid }, h('button', { type: 'button', onClick: function () { open(task); } }, text(task.description)), h('small', null, ' ' + text(task.stage) + ' · ' + text(task.status)), h('code', null, task.uuid)); })) : h('p', null, query ? '没有匹配的任务。' : '当前 Kanban 看板没有任务。'),
            filtered.length > 50 ? h('nav', { 'aria-label': '任务分页' }, h('button', { type: 'button', disabled: currentPage === 0, onClick: function () { setPage(currentPage - 1); } }, '上一页'), h('span', null, (currentPage + 1) + ' / ' + Math.ceil(filtered.length / 50)), h('button', { type: 'button', disabled: (currentPage + 1) * 50 >= filtered.length, onClick: function () { setPage(currentPage + 1); } }, '下一页')) : null) : null);
      };
    }
