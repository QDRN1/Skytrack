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
      if (!(await window.uiModal.confirm(
        'Anyone displaying the old ID on the radar page will need to re-pair. '
        + 'This action cannot be undone.',
        'Force-regenerate device ID?'
      ))) return;
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
      if (!(await window.uiModal.confirm(
        'This wipes auth.json (admin PIN, hotspot password, API keys) and '
        + 'forces the device back to the first-boot wizard. Your aircraft '
        + 'database is kept. This cannot be undone.',
        'Factory reset?'
      ))) return;
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

    // Shell terminal
    const shellForm = document.getElementById('form-shell');
    const shellOutput = document.getElementById('shell-output');
    const shellCmd = document.getElementById('shell-cmd');
    const shellHistory = [];
    let historyIdx = -1;

    if (shellForm && shellOutput && shellCmd) {
      shellForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const cmd = shellCmd.value.trim();
        if (!cmd) return;
        shellHistory.push(cmd);
        historyIdx = shellHistory.length;
        shellOutput.textContent += '$ ' + cmd + '\n';
        shellCmd.value = '';
        shellCmd.disabled = true;
        try {
          const r = await fetch('/api/super/shell', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
            body: JSON.stringify({ command: cmd }),
          });
          const data = await r.json();
          if (data.output) shellOutput.textContent += data.output;
          if (data.error) shellOutput.textContent += data.error + '\n';
          if (!data.output && !data.error) shellOutput.textContent += '\n';
        } catch (err) {
          shellOutput.textContent += 'error: ' + (err.message || 'request failed') + '\n';
        }
        shellCmd.disabled = false;
        shellCmd.focus();
        shellOutput.scrollTop = shellOutput.scrollHeight;
      });

      shellCmd.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          if (historyIdx > 0) { historyIdx--; shellCmd.value = shellHistory[historyIdx]; }
        } else if (e.key === 'ArrowDown') {
          e.preventDefault();
          if (historyIdx < shellHistory.length - 1) { historyIdx++; shellCmd.value = shellHistory[historyIdx]; }
          else { historyIdx = shellHistory.length; shellCmd.value = ''; }
        }
      });
    }
  });

  function bind(sel, fn) {
    const el = document.querySelector(sel);
    if (el) el.addEventListener('click', fn);
  }
})();
