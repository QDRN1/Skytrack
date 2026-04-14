/* Topbar: clock, secondary meta, 5-tap super-user shortcut.
 *
 * The status pills (net / cell / temp) are powered by the public
 * /api/network/status, /api/sensor, and /api/auth/status endpoints,
 * so they render even before the user signs in.
 */
(function () {
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
      const refresh = async () => {
        try {
          const status = await window.api.get('/api/network/status');
          const net = document.getElementById('meta-net');
          const cell = document.getElementById('meta-cell');
          if (net) net.textContent = status.internet ? 'net ✓' : 'net ✗';
          if (cell) cell.textContent = status.cellular && status.cellular.detected
            ? `cell ${status.cellular.signal_pct || 0}%` : 'cell —';
        } catch (e) { /* network probe failed; leave dashes */ }
        try {
          const sensor = await window.api.get('/api/sensor');
          const t = document.getElementById('meta-temp');
          if (t && sensor.reading) t.textContent = `${sensor.reading.temperature_f}°F`;
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
