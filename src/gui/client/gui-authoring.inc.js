    // Local authoring scratchpad only. The Host remains authoritative for saved revisions.
    function createGuiAuthoringCache(storage) {
      const key = 'h2b.gui.authoring.v1', itemLimit = 256 * 1024, totalLimit = 2 * 1024 * 1024, countLimit = 12;
      const fail = function (message) { throw new Error('GUI 工作稿缓存：' + message); };
      const bytes = function (text) { return new TextEncoder().encode(text).length; };
      const validId = function (id) { return typeof id === 'string' && /^[a-zA-Z0-9_-]{1,128}$/.test(id) && !['__proto__', 'constructor', 'prototype'].includes(id); };
      function normalize(id, value) {
        if (!validId(id) || !value || typeof value !== 'object' || !value.draft || value.draft.id !== id || !Number.isSafeInteger(value.draft.revision) || value.draft.revision < 1) fail('工作稿标识或修订无效');
        if (typeof value.text !== 'string' || typeof value.dirty !== 'boolean' || typeof value.instruction !== 'string' || typeof value.selected !== 'string') fail('工作稿字段无效');
        const draft = {};
        ['id', 'revision', 'sessionId', 'createdAt', 'updatedAt', 'document'].forEach(function (field) { if (value.draft[field] !== undefined) draft[field] = value.draft[field]; });
        if (!draft.document || typeof draft.document !== 'object' || Array.isArray(draft.document)) fail('界面定义无效');
        if (value.preview != null && (typeof value.preview !== 'object' || Array.isArray(value.preview))) fail('视觉工作稿无效');
        let encoded;
        try { encoded = JSON.stringify({ draft, text: value.text, dirty: value.dirty, instruction: value.instruction, selected: value.selected, preview: value.preview ?? null }); } catch (_) { fail('无法序列化工作稿，已有缓存已保留'); }
        if (bytes(encoded) > itemLimit) fail('单份工作稿超过 256 KiB，已有缓存已保留');
        return JSON.parse(encoded);
      }
      function load() {
        let raw;
        try { raw = storage.getItem(key); } catch (_) { fail('无法读取浏览器存储，请检查存储权限；已有缓存未改动'); }
        if (raw === null) return { version: 1, entries: [] };
        if (typeof raw !== 'string' || bytes(raw) > totalLimit) fail('缓存内容过大，已有缓存未改动');
        let parsed;
        try { parsed = JSON.parse(raw); } catch (_) { fail('缓存内容损坏，已有缓存未改动'); }
        if (!parsed || parsed.version !== 1 || !Array.isArray(parsed.entries) || parsed.entries.length > countLimit) fail('缓存格式无效，已有缓存未改动');
        const seen = new Set(), entries = [];
        parsed.entries.forEach(function (entry) {
          try {
            if (!entry || seen.has(entry.id)) return;
            const value = normalize(entry.id, entry.value);
            entries.push({ id: entry.id, value }); seen.add(entry.id);
          } catch (_) { /* Isolate one damaged item; healthy drafts remain accessible. */ }
        });
        return { version: 1, entries };
      }
      function save(state) {
        const encoded = JSON.stringify(state);
        if (bytes(encoded) > totalLimit) fail('工作稿总量超过 2 MiB，请先清理已保存的工作稿；已有缓存已保留');
        try { storage.setItem(key, encoded); } catch (_) { fail('无法保存浏览器工作稿（空间不足或存储权限受限），已有缓存已保留；请勿关闭当前编辑页'); }
      }
      return {
        read: function (id) { return load().entries.find(function (entry) { return entry.id === id; })?.value || null; },
        write: function (id, value) {
          const normalized = normalize(id, value), state = load();
          state.entries = state.entries.filter(function (entry) { return entry.id !== id; });
          state.entries.push({ id, value: normalized });
          while (state.entries.length > countLimit || bytes(JSON.stringify(state)) > totalLimit) {
            const disposable = state.entries.findIndex(function (entry) { return entry.id !== id && !entry.value.dirty && !entry.value.instruction.trim(); });
            if (disposable < 0) {
              if (state.entries.length > countLimit) fail('最多保留 12 份工作稿，请先清理已保存的工作稿；已有缓存已保留');
              fail('工作稿总量超过 2 MiB，请先清理已保存的工作稿；已有缓存已保留');
            }
            state.entries.splice(disposable, 1);
          }
          save(state);
        },
        remove: function (id) { const state = load(); if (!state.entries.some(function (entry) { return entry.id === id; })) return; state.entries = state.entries.filter(function (entry) { return entry.id !== id; }); save(state); },
        lastId: function () { const entries = load().entries; return entries.length ? entries[entries.length - 1].id : null; }
      };
    }
