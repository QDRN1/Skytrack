/* Super-user console. */
(function () {
  document.addEventListener('DOMContentLoaded', () => {

    // Login form (on the superuser_login page)
    const loginForm = document.getElementById('super-form');
    const loginErr = document.getElementById('super-error');
    if (loginForm) {
      loginForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        loginErr.hidden = true;
        const fd = new FormData(loginForm);
        try {
          const r = await fetch('/superuser', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              username: fd.get('username'),
              password: fd.get('password'),
            }),
          }).then(x => x.json());
          if (r.ok) { window.location.href = r.next; return; }
          loginErr.textContent = r.error || 'auth failed';
          loginErr.hidden = false;
        } catch (err) {
          loginErr.textContent = 'connection error';
          loginErr.hidden = false;
        }
      });
    }

    // Console actions
    const out = document.getElementById('super-output');
    const log = (msg) => {
      if (out) out.textContent += `\n${new Date().toISOString()}  ${msg}`;
    };

    bind('#btn-regen-device', async () => {
      if (!confirm('Force-regenerate device ID? Anyone displaying the old ID will need to re-pair.')) return;
      const r = await window.api.post('/api/super/device/regenerate', { reason: 'super-console' });
      log(`device → ${r.device.device_id}`);
    });

    bind('#btn-prune', async () => {
      await window.api.post('/api/super/db/prune');
      log('db prune complete');
    });

    bind('#btn-vacuum', async () => {
      await window.api.post('/api/super/db/vacuum');
      log('db vacuum complete');
    });

    bind('#btn-factory-reset', async () => {
      if (!confirm('Wipe auth.json and re-run the first-boot wizard?')) return;
      await window.api.post('/api/super/factory_reset');
      log('factory reset — redirecting to /setup');
      setTimeout(() => window.location.href = '/setup', 1500);
    });

    const credsForm = document.getElementById('form-super-creds');
    if (credsForm) {
      credsForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const fd = new FormData(credsForm);
        try {
          await window.api.post('/api/super/credentials', {
            username: fd.get('username'),
            password: fd.get('password'),
          });
          log('super credentials updated');
          credsForm.reset();
        } catch (err) {
          log(`error: ${(err.data && err.data.error) || err.message}`);
        }
      });
    }
  });

  function bind(sel, fn) {
    const el = document.querySelector(sel);
    if (el) el.addEventListener('click', fn);
  }
})();
