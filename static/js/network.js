/* Network page: status panels + hotspot actions.
 *
 * All user-visible dialogs go through window.uiModal (static/js/modal.js)
 * so the kiosk never sees a native Chromium alert box.
 */
(function () {
  function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

  document.addEventListener('DOMContentLoaded', () => {
    refresh();
    setInterval(refresh, 15000);

    bind('#btn-hotspot-show', async () => {
      try {
        const r = await window.api.get('/api/network/hotspot/credentials');
        await window.uiModal.alert(
          `SSID: ${r.ssid}\nPassword: ${r.password || '(not set)'}`,
          'Hotspot credentials'
        );
      } catch (e) {
        await window.uiModal.alert('Admin role required.', 'Not allowed');
      }
    });
    bind('#btn-hotspot-regenerate', async function () {
      var btn = document.querySelector('#btn-hotspot-regenerate');
      try {
        if (btn) btn.disabled = true;
        if (!(await window.uiModal.confirm(
          'This will rotate the hotspot password. Anyone currently connected '
          + 'will need to re-enter the new password.',
          'Generate new hotspot password?'
        ))) return;
        const r = await window.api.post('/api/network/hotspot/regenerate');
        await window.uiModal.alert(`New password:\n${r.password}`, 'Hotspot password');
      } catch (e) {
        await window.uiModal.alert('Operation failed: ' + (e.message || e), 'Error');
      } finally {
        if (btn) btn.disabled = false;
      }
    });
    bind('#btn-hotspot-restart', async function () {
      var btn = document.querySelector('#btn-hotspot-restart');
      try {
        if (btn) btn.disabled = true;
        if (!(await window.uiModal.confirm(
          'Restart hostapd, dnsmasq, and skytrack-hotspot. Clients will '
          + 'briefly disconnect.',
          'Restart hotspot services?'
        ))) return;
        const r = await window.api.post('/api/network/hotspot/restart');
        await window.uiModal.alert(
          r.message || (r.ok ? 'Hotspot restarted.' : 'Restart failed.'),
          r.ok ? 'Done' : 'Failed'
        );
      } catch (e) {
        await window.uiModal.alert('Operation failed: ' + (e.message || e), 'Error');
      } finally {
        if (btn) btn.disabled = false;
      }
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
        `SSID: <strong>${esc(s.hotspot.ssid)}</strong>`,
        `Gateway: ${esc(s.hotspot.gateway || '')}`,
        `Active: ${s.hotspot.enabled ? 'yes' : 'no'}`,
        `Clients: ${s.hotspot.clients ? s.hotspot.clients.length : 0}`,
      ].join('<br>'));
      render('#wifi-status', s.wifi && (s.wifi.connected ? `Connected to ${esc(s.wifi.ssid)} (${s.wifi.signal_pct}%)` : 'Not connected'));
      render('#cell-status', s.cellular && s.cellular.detected
        ? `${esc(s.cellular.carrier || 'unknown')} • ${esc(s.cellular.access_tech || '')} • ${s.cellular.signal_pct || 0}% (${esc(s.cellular.state || '')})`
        : 'No modem detected');
      render('#internet-status', s.internet ? '✓ Internet reachable' : '✗ No internet');
    } catch (e) { /* not authed */ }
  }

  function render(sel, html) {
    const el = document.querySelector(sel);
    if (el) el.innerHTML = html || '<span class="muted">unavailable</span>';
  }
})();
