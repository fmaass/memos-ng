// happy-dom (in this vitest config) does not wire window.localStorage, and Node 22's
// experimental global one is unavailable. Provide a minimal in-memory Storage so tests
// exercise real persistence. Only activates when absent — real browsers are untouched.
if (typeof window !== 'undefined' && !window.localStorage) {
  const data = new Map<string, string>()
  const storage: Storage = {
    get length() {
      return data.size
    },
    clear: () => data.clear(),
    getItem: (key) => (data.has(key) ? data.get(key)! : null),
    key: (index) => [...data.keys()][index] ?? null,
    removeItem: (key) => {
      data.delete(key)
    },
    setItem: (key, value) => {
      data.set(key, String(value))
    },
  }
  Object.defineProperty(window, 'localStorage', { value: storage, configurable: true })
}
