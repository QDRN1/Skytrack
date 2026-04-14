/* First-boot wizard form handler. */
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
    if (!form) return;

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      err.hidden = true;

      const data = new FormData(form);
      const pin = (data.get('pin') || '').trim();
      const pinConfirm = (data.get('pin_confirm') || '').trim();
      const adminPw = data.get('admin_password') || '';
      const adminConfirm = data.get('admin_password_confirm') || '';

      if (pin !== pinConfirm) {
        err.textContent = 'PINs do not match.';
        err.hidden = false;
        return;
      }
      if (adminPw !== adminConfirm) {
        err.textContent = 'Admin passwords do not match.';
        err.hidden = false;
        return;
      }

      try {
        const result = await fetch('/setup', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pin, admin_password: adminPw }),
        }).then(r => r.json());

        if (!result.ok) {
          err.textContent = result.error || 'Setup failed.';
          err.hidden = false;
          return;
        }
        if (result.hotspot_password) {
          alert('Setup complete!\n\nYour new hotspot WPA2 password is:\n\n' +
                result.hotspot_password +
                '\n\nWrite it down — it will also appear in Settings → Network.');
        }
        window.location.href = result.next || '/dashboard';
      } catch (e2) {
        err.textContent = e2.message || 'Network error';
        err.hidden = false;
      }
    });
  });
})();
