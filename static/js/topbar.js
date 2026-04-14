/* Topbar: clock, secondary meta, 5-tap super-user shortcut. */
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
        } catch (e) { /* not signed in yet */ }
        try {
          const sensor = await window.api.get('/api/sensor');
          const t = document.getElementById('meta-temp');
          if (t && sensor.reading) t.textContent = `${sensor.reading.temperature_f}°F`;
        } catch (e) { /* not signed in yet */ }
      };
      refresh();
      setInterval(refresh, 30000);
    }

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
