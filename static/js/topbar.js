/* Topbar: clock, secondary meta, 5-tap super-user shortcut.
 *
 * The status pills are powered by the public /api/network/status and
 * /api/sensor endpoints, so they render even before the user signs in.
 *
 * Honesty rules (product-quality pass):
 *   * `meta-net`  — single "online/offline" truth, not a WiFi%/cell% race.
 *   * `meta-wifi` — separate WiFi-client pill: SSID + bars when joined,
 *                   "wifi —" when not. Never collapsed into "cell".
 *   * `meta-cell` — separate cellular pill: bars + carrier/access-tech
 *                   when modem is up, "cell —" when no SIM/no signal.
 *   * `meta-temp` — ALWAYS prefixed with its source: "Inside" when the
 *                   DHT22 is real, "Cached" when the sensor has been
 *                   missing for a read, and "Mock" on dev boxes so we
 *                   never pretend synthetic data is real.
 *
 * The two link pills always coexist so the operator can see at a glance
 * which uplink is doing the work and which one is dark — no more
 * ambiguous single "link" pill.
 */
(function () {
  // Convert 0..100% signal to a 4-bar unicode glyph string.
  // We deliberately use a monochrome run of block glyphs instead of an
  // emoji bar so the topbar renders identically on every browser/font.
  function signalBars(pct) {
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    let bars = 0;
    if (p >= 88) bars = 4;
    else if (p >= 63) bars = 3;
    else if (p >= 38) bars = 2;
    else if (p >= 13) bars = 1;
    const on = '▮';
    const off = '▯';
    return on.repeat(bars) + off.repeat(4 - bars);
  }

  function tempLabel(reading) {
    if (!reading) return 'Sensor —';
    const f = reading.temperature_f;
    if (f == null) {
      // Show why we don't have a number, not a silent dash.
      const src = (reading.source || '').toLowerCase();
      if (src === 'mock' || reading.mock === true) return 'Mock —';
      return 'Sensor —';
    }
    const source = (reading.source || '').toLowerCase();
    let prefix = 'Inside';
    if (source === 'mock' || reading.mock === true) prefix = 'Mock';
    else if (source === 'dht22_cached') prefix = 'Cached';
    else if (source === 'dht22') prefix = 'Inside';
    return `${prefix} ${f}°F`;
  }

  // Render the cellular pill from the status payload.
  function renderCell(el, status) {
    if (!el) return;
    const c = status.cellular || {};
    const detected = !!c.detected;
    if (!detected) {
      el.textContent = 'cell —';
      el.title = 'no modem detected';
      el.classList.remove('meta-ok', 'meta-bad', 'meta-active');
      el.classList.add('meta-off');
      return;
    }
    const pct = Number(c.signal_pct) || 0;
    const tech = (c.access_tech || '').toUpperCase() || 'cell';
    el.textContent = `${tech} ${signalBars(pct)}`;
    const carrier = c.carrier ? ` · ${c.carrier}` : '';
    const apn = c.apn ? ` · APN ${c.apn}` : '';
    el.title = `cellular${carrier}${apn} · ${pct}%`;
    el.classList.remove('meta-off');
    const active = (status.primary === 'cellular');
    el.classList.toggle('meta-active', active);
    el.classList.toggle('meta-ok', pct >= 25);
    el.classList.toggle('meta-bad', pct < 25);
  }

  // Render the WiFi-client pill from the status payload.
  function renderWifi(el, status) {
    if (!el) return;
    const w = status.wifi || {};
    const connected = !!w.connected;
    if (!connected) {
      el.textContent = 'wifi —';
      el.title = w.ssid ? `not connected (last: ${w.ssid})` : 'not connected';
      el.classList.remove('meta-ok', 'meta-active');
      el.classList.add('meta-off');
      return;
    }
    const pct = Number(w.signal_pct) || 0;
    const ssid = w.ssid || 'wifi';
    el.textContent = `${ssid} ${signalBars(pct)}`;
    el.title = `Wi-Fi · ${ssid} · ${pct}%`;
    el.classList.remove('meta-off');
    const active = (status.primary === 'wifi');
    el.classList.toggle('meta-active', active);
    el.classList.toggle('meta-ok', pct >= 25);
    el.classList.toggle('meta-bad', pct < 25);
  }

  document.addEventListener('DOMContentLoaded', () => {

    // Clock
    const clock = document.getElementById('meta-clock');
    if (clock) {
      const tick = () => {
        const d = new Date();
        var h24 = d.getHours();
        var h12 = h24 % 12 || 12;
        var mm = String(d.getMinutes()).padStart(2, '0');
        var ss = String(d.getSeconds()).padStart(2, '0');
        var ampm = h24 < 12 ? 'AM' : 'PM';
        clock.textContent = h12 + ':' + mm + ':' + ss + ' ' + ampm;
      };
      tick();
      setInterval(tick, 1000);
    }

    // Periodic network/sensor poll for the secondary topbar
    if (window.api && document.getElementById('meta-net')) {
      const netEl  = document.getElementById('meta-net');
      const wifiEl = document.getElementById('meta-wifi');
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
          renderWifi(wifiEl, status);
          renderCell(cellEl, status);
        } catch (e) { /* network probe failed; leave dashes */ }
        try {
          const sensor = await window.api.get('/api/sensor');
          if (tempEl) {
            const reading = sensor && sensor.reading;
            tempEl.textContent = tempLabel(reading);
            const isMock = !reading || reading.mock === true || (reading.source || '').toLowerCase() === 'mock';
            tempEl.classList.toggle('meta-bad', isMock);
            tempEl.title = reading && reading.last_error ? `sensor: ${reading.last_error}` : 'inside-box DHT22';
          }
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

    // Device temp alarm banner poll
    const critBanner = document.getElementById('critical-temp-banner');
    const critText = document.getElementById('critical-temp-text');
    if (critBanner) {
      const checkAlarm = async () => {
        try {
          const r = await fetch('/api/device-temp-alarm', {
            credentials: 'same-origin',
            headers: { Accept: 'application/json' },
            cache: 'no-store',
          });
          if (!r.ok) return;
          const d = await r.json();
          if (d.active) {
            critBanner.hidden = false;
            if (critText && d.cpu_temp_c != null) {
              critText.textContent = 'Device overheating: CPU ' + d.cpu_temp_c.toFixed(0) + '°C (limit: ' + (d.threshold_c || 80) + '°C)';
            }
          } else {
            critBanner.hidden = true;
          }
        } catch (_e) { /* transient */ }
      };
      checkAlarm();
      setInterval(checkAlarm, 15000);
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
