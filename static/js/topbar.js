/* Topbar: clock, secondary meta, 5-tap super-user shortcut.
 *
 * The status pills (net / link / temp) are powered by the public
 * /api/network/status, /api/sensor, and /api/auth/status endpoints,
 * so they render even before the user signs in.
 *
 * Honesty rules (Issues #6 and #9):
 *   * `meta-net`  — single "online/offline" truth, not a WiFi%/cell% race.
 *   * `meta-cell` — shows the ACTIVE uplink (cell/wifi/ethernet/hotspot)
 *                   as signal bars, not a percentage. Hidden if there is
 *                   no link and the device is offline.
 *   * `meta-temp` — ALWAYS prefixed with its source: "Indoor" when the
 *                   DHT22 is real, "Cached" when the sensor has been
 *                   missing for a read, and "Mock" on dev boxes so we
 *                   never pretend synthetic data is real.
 */
(function () {
  // Convert 0..100% signal to a 4-bar unicode glyph string.
  // We deliberately use a monochrome run of block glyphs instead of an
  // emoji bar so the topbar renders identically on every browser/font.
  function signalBars(pct) {
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    // 0..12  → 0 bars
    // 13..37 → 1 bar
    // 38..62 → 2 bars
    // 63..87 → 3 bars
    // 88..100→ 4 bars
    let bars = 0;
    if (p >= 88) bars = 4;
    else if (p >= 63) bars = 3;
    else if (p >= 38) bars = 2;
    else if (p >= 13) bars = 1;
    const on = '▮';
    const off = '▯';
    return on.repeat(bars) + off.repeat(4 - bars);
  }

  function linkLabel(uplink, wifi, cellular, hotspot) {
    const kind = (uplink && uplink.kind) || 'none';
    if (kind === 'cellular') {
      return { label: 'cell', pct: (cellular && cellular.signal_pct) || 0, show: true };
    }
    if (kind === 'wifi') {
      return { label: 'wifi', pct: (wifi && wifi.signal_pct) || 0, show: true };
    }
    if (kind === 'ethernet') {
      return { label: 'eth', pct: 100, show: true };
    }
    if (hotspot && hotspot.enabled) {
      return { label: 'hs', pct: 100, show: true };
    }
    return { label: '—', pct: 0, show: false };
  }

  function tempLabel(reading) {
    if (!reading) return '--°F';
    const f = reading.temperature_f;
    if (f == null) return '--°F';
    const source = (reading.source || '').toLowerCase();
    let prefix = 'Indoor';
    if (source === 'mock' || reading.mock === true) prefix = 'Mock';
    else if (source === 'dht22_cached') prefix = 'Cached';
    else if (source === 'dht22') prefix = 'Indoor';
    return `${prefix} ${f}°F`;
  }

  document.addEventListener('DOMContentLoaded', () => {

    // Clock
    const clock = document.getElementById('meta-clock');
    if (clock) {
      const tick = () => {
        const d = new Date();
        clock.textContent = d.toLocaleTimeString();
      };
      tick();
      setInterval(tick, 1000);
    }

    // Periodic network/sensor poll for the secondary topbar
    if (window.api && document.getElementById('meta-net')) {
      const netEl  = document.getElementById('meta-net');
      const cellEl = document.getElementById('meta-cell');
      const tempEl = document.getElementById('meta-temp');

      const refresh = async () => {
        try {
          const status = await window.api.get('/api/network/status');
          if (netEl) {
            netEl.textContent = status.internet ? 'online' : 'offline';
            netEl.classList.toggle('meta-ok', !!status.internet);
            netEl.classList.toggle('meta-bad', !status.internet);
          }
          if (cellEl) {
            const info = linkLabel(status.uplink, status.wifi, status.cellular, status.hotspot);
            if (!info.show) {
              cellEl.textContent = '—';
            } else {
              cellEl.textContent = `${info.label} ${signalBars(info.pct)}`;
            }
            cellEl.title = status.primary_interface
              ? `active link: ${info.label} via ${status.primary_interface}`
              : 'no active link';
          }
        } catch (e) { /* network probe failed; leave dashes */ }
        try {
          const sensor = await window.api.get('/api/sensor');
          if (tempEl) tempEl.textContent = tempLabel(sensor && sensor.reading);
        } catch (e) { /* sensor unavailable */ }
      };
      refresh();
      setInterval(refresh, 30000);
    }

    // Sign-out link — fire a POST so we don't leak credentials in logs,
    // then reload to repaint the topbar.
    const signout = document.getElementById('topbar-signout');
    if (signout) {
      signout.addEventListener('click', async (ev) => {
        ev.preventDefault();
        try {
          await fetch('/logout', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { Accept: 'application/json' },
          });
        } catch (_e) { /* offline */ }
        if (window.auth && window.auth.toast) window.auth.toast('Signed out', 'info');
        window.location.href = '/dashboard';
      });
    }

    // Sign-in button is wired by auth.js via [data-auth-trigger="admin"].

    // 5-tap or long-press super-user shortcut on the topbar logo
    const logo = document.getElementById('topbar-logo');
    if (logo) {
      let taps = 0;
      let timer = null;
      let pressTimer = null;
      logo.addEventListener('click', () => {
        taps += 1;
        clearTimeout(timer);
        timer = setTimeout(() => { taps = 0; }, 1500);
        if (taps >= 5) {
          window.location.href = '/superuser';
        }
      });
      logo.addEventListener('mousedown', () => {
        pressTimer = setTimeout(() => { window.location.href = '/superuser'; }, 1500);
      });
      ['mouseup', 'mouseleave', 'touchend', 'touchcancel'].forEach((ev) => {
        logo.addEventListener(ev, () => clearTimeout(pressTimer));
      });
    }
  });
})();
