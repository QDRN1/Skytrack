/* First-boot wizard form handler.
 *
 * Admin is PIN-only now — no password. The operator picks a 4-8 digit
 * numeric PIN and confirms it; we POST that to /setup, which finishes
 * activation and returns the auto-generated hotspot WPA2 password so
 * the operator can write it down before the hotspot flips to secured.
 */
(function () {
  document.addEventListener('DOMContentLoaded', () => {

    // If the background video fails to load (file missing, unsupported
    // codec) hide it and let the particle gradient show through.
    const v = document.getElementById('setup-video');
    if (v) {
      v.addEventListener('error', () => v.style.display = 'none');
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
      const pin = (data.get('pin') || '').trim();
      const pinConfirm = (data.get('pin_confirm') || '').trim();

      if (!pin) {
        err.textContent = 'Please enter an Admin PIN (4 to 8 digits, numbers only).';
        err.hidden = false;
        return;
      }
      if (!/^\d+$/.test(pin)) {
        err.textContent = 'Admin PIN must contain digits only (0–9). No letters or symbols.';
        err.hidden = false;
        return;
      }
      if (pin.length < 4 || pin.length > 8) {
        err.textContent = `Admin PIN must be 4 to 8 digits long (you entered ${pin.length}).`;
        err.hidden = false;
        return;
      }
      if (pin !== pinConfirm) {
        err.textContent = 'The two PINs do not match. Please re-enter the same digits in both boxes.';
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
          body: JSON.stringify({ pin }),
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
