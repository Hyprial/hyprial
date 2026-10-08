export function createMemoryPersistence(seed = null) {
  let value = seed === null ? null : JSON.stringify(seed);
  let pending = Promise.resolve();
  return {
    async read() { return value === null ? null : JSON.parse(value); },
    async write(bytes) { value = bytes; },
    async lock(run) {
      const previous = pending;
      let release;
      pending = new Promise(resolve => { release = resolve; });
      await previous;
      try { return await run(); } finally { release(); }
    },
  };
}
