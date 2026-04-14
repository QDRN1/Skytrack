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
        document.querySelector(`[data-tabpanel="${t.dataset.tab}"]`).classList.add('active');
      });
    });
  }

  async function loadAll() {
    try {
      const portal = await window.api.get('/api/logs/portal');
      document.getElementById('log-portal').textContent =
        portal.map(r => `${r.ts}  [${r.actor}]  ${r.action}  ${r.detail || ''}`).join('\n') || '(empty)';
    } catch (e) { /* ignore */ }
    try {
      const net = await window.api.get('/api/logs/network');
      document.getElementById('log-network').textContent =
        net.map(r => `${r.ts}  ${r.event}  ${r.detail || ''}`).join('\n') || '(empty)';
    } catch (e) { /* ignore */ }
    try {
      const app = await window.api.get('/api/logs/app');
      document.getElementById('log-app').textContent =
        (app.lines || []).join('\n') || '(empty — admin role required)';
    } catch (e) {
      document.getElementById('log-app').textContent = 'admin role required';
    }
  }
})();
