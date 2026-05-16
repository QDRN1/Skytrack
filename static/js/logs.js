/* Logs page: tab switcher + tail loaders. */
(function () {
  document.addEventListener('DOMContentLoaded', () => {
    bindTabs();
    loadAll();
    setInterval(loadAll, 15000);
  });

  function bindTabs() {
    const tabs = document.querySelectorAll('#logs-tablist .tab');
    const panels = document.querySelectorAll('.tabpanel');
    tabs.forEach((t) => {
      t.addEventListener('click', () => {
        tabs.forEach(x => x.classList.remove('active'));
        panels.forEach(x => x.classList.remove('active'));
        t.classList.add('active');
        var panel = document.querySelector(`[data-tabpanel="${t.dataset.tab}"]`);
        if (panel) panel.classList.add('active');
      });
    });
  }

  async function loadAll() {
    try {
      const portal = await window.api.get('/api/logs/portal');
      var portalEl = document.getElementById('log-portal');
      if (portalEl && Array.isArray(portal)) {
        portalEl.textContent =
          portal.map(r => `${r.ts}  [${r.actor}]  ${r.action}  ${r.detail || ''}`).join('\n') || '(empty)';
      }
    } catch (e) { /* ignore */ }
    try {
      const net = await window.api.get('/api/logs/network');
      var netEl = document.getElementById('log-network');
      if (netEl && Array.isArray(net)) {
        netEl.textContent =
          net.map(r => `${r.ts}  ${r.event}  ${r.detail || ''}`).join('\n') || '(empty)';
      }
    } catch (e) { /* ignore */ }
    try {
      const app = await window.api.get('/api/logs/app');
      var appEl = document.getElementById('log-app');
      if (appEl) appEl.textContent =
        (app.lines || []).join('\n') || '(empty — admin role required)';
    } catch (e) {
      var appElFallback = document.getElementById('log-app');
      if (appElFallback) appElFallback.textContent = 'admin role required';
    }
  }
})();
