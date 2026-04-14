/* First-boot wizard form handler.
 *
 * PIN is optional now — only the admin password is required. After a
 * successful POST we reveal the auto-generated hotspot password so the
 * operator can write it down before connecting back over WPA2.
 */
(function () {
  document.addEventListener('DOMContentLoaded', () => {

    // If the video failed to load (404 because the user hasn't dropped
    // the file in yet), hide it and let the particle gradient show.
    const v = document.getElementById('setup-video');
    if (v) {
      v.addEventListener('error', () => v.style.display = 'none');
      // Some browsers fire 'stalled' instead of 'error' for missing src.
      setTimeout(() => {
        if (v.readyState === 0) v.style.display = 'none';
      }, 1500);
    }

    const form = document.getElementById('setup-form');
    const err = document.getElementById('setup-error');
    const done = document.getElementById('setup-done');
    const hotspotPwEl = document.getElementById('setup-hotspot-pw');
    const goDashboard = document.getElementById('setup-go-dashboard');
    if (!form) return;

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      err.hidden = true;

      const data = new FormData(form);
      const adminPw = data.get('admin_password') || '';
      const adminConfirm = data.get('admin_password_confirm') || '';
      const pin = (data.get('pin') || '').trim();

      if (adminPw.length < 6) {
        err.textContent = 'Admin password must be at least 6 characters.';
        err.hidden = false;
        return;
      }
      if (adminPw !== adminConfirm) {
        err.textContent = 'Admin passwords do not match.';
        err.hidden = false;
        return;
      }
      if (pin && (!/^[0-9]+$/.test(pin) || pin.length < 4 || pin.length > 8)) {
        err.textContent = 'PIN must be 4–8 digits, or left blank.';
        err.hidden = false;
        return;
      }

      try {
        const resp = await fetch('/setup', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
          },
          body: JSON.stringify({ admin_password: adminPw, pin }),
        });
        const result = await resp.json().catch(() => ({}));

        if (!resp.ok || !result.ok) {
          err.textContent = result.error || 'Setup failed.';
          err.hidden = false;
          return;
        }

        // Reveal the hotspot password card and hide the form
        if (done && hotspotPwEl && result.hotspot_password) {
          hotspotPwEl.textContent = result.hotspot_password;
          form.hidden = true;
          done.hidden = false;
          if (goDashboard) {
            goDashboard.href = result.next || '/dashboard';
          }
        } else {
          // Fallback if the server didn't return a hotspot password
          window.location.href = result.next || '/dashboard';
        }
      } catch (e2) {
        err.textContent = e2.message || 'Network error';
        err.hidden = false;
      }
    });
  });
})();
