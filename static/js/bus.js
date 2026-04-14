/* Tiny pub/sub event bus shared across modules. */
(function () {
  const listeners = new Map();
  window.bus = {
    on(evt, fn) {
      if (!listeners.has(evt)) listeners.set(evt, new Set());
      listeners.get(evt).add(fn);
      return () => listeners.get(evt).delete(fn);
    },
    emit(evt, payload) {
      (listeners.get(evt) || []).forEach((fn) => {
        try { fn(payload); } catch (e) { console.error('bus handler', e); }
      });
    },
  };
})();
