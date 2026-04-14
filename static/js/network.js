/* Network page: status panels + hotspot actions. */
(function () {
  document.addEventListener('DOMContentLoaded', () => {
    refresh();
    setInterval(refresh, 15000);

    bind('#btn-hotspot-show', async () => {
      try {
        const r = await window.api.get('/api/network/hotspot/credentials');
        alert(`SSID: ${r.ssid}\nPassword: ${r.password || '(not set)'}`);
      } catch (e) { alert('Admin role required'); }
    });
    bind('#btn-hotspot-regenerate', async () => {
      if (!confirm('Generate a new hotspot password?')) return;
      const r = await window.api.post('/api/network/hotspot/regenerate');
      alert(`New password:\n${r.password}`);
    });
    bind('#btn-hotspot-restart', async () => {
      if (!confirm('Restart hotspot services?')) return;
      const r = await window.api.post('/api/network/hotspot/restart');
      alert(r.message || (r.ok ? 'OK' : 'Failed'));
    });

    const metered = document.getElementById('metered-toggle');
    if (metered) metered.addEventListener('change', async () => {
      await window.api.post('/api/network/metered', { metered_connection: metered.checked });
    });
  });

  function bind(sel, fn) {
    const el = document.querySelector(sel);
    if (el) el.addEventListener('click', fn);
  }

  async function refresh() {
    try {
      const s = await window.api.get('/api/network/status');
      render('#hotspot-status', s.hotspot && [
        `SSID: <strong>${s.hotspot.ssid}</strong>`,
        `Gateway: ${s.hotspot.gateway}`,
        `Active: ${s.hotspot.enabled ? 'yes' : 'no'}`,
        `Clients: ${s.hotspot.clients ? s.hotspot.clients.length : 0}`,
      ].join('<br>'));
      render('#wifi-status', s.wifi && (s.wifi.connected ? `Connected to ${s.wifi.ssid} (${s.wifi.signal_pct}%)` : 'Not connected'));
      render('#cell-status', s.cellular && s.cellular.detected
        ? `${s.cellular.carrier || 'unknown'} • ${s.cellular.access_tech || ''} • ${s.cellular.signal_pct || 0}% (${s.cellular.state})`
        : 'No modem detected');
      render('#internet-status', s.internet ? '✓ Internet reachable' : '✗ No internet');
    } catch (e) { /* not authed */ }
  }

  function render(sel, html) {
    const el = document.querySelector(sel);
    if (el) el.innerHTML = html || '<span class="muted">unavailable</span>';
  }
})();
