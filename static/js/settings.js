/* Settings page: tab switcher + per-form submitters. */
(function () {
  document.addEventListener('DOMContentLoaded', () => {
    bindTabs('#settings-tablist', '.tabpanel');
    bindForms();
    bindHardwareExtras();
  });

  function bindTabs(navSel, panelSel) {
    const tabs = document.querySelectorAll(`${navSel} .tab`);
    const panels = document.querySelectorAll(panelSel);
    tabs.forEach((t) => {
      t.addEventListener('click', () => {
        tabs.forEach(x => x.classList.remove('active'));
        panels.forEach(x => x.classList.remove('active'));
        t.classList.add('active');
        const target = document.querySelector(`[data-tabpanel="${t.dataset.tab}"]`);
        if (target) target.classList.add('active');
      });
    });
  }

  function bindForms() {
    document.querySelectorAll('form[data-endpoint]').forEach((form) => {
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const url = form.dataset.endpoint;
        const fd = new FormData(form);
        const body = {};
        const secrets = {};
        // The PIN form is a special case: an empty value means "clear
        // the PIN", so we must transmit it instead of stripping it.
        const allowEmptyKeys = (form.id === 'form-pin') ? new Set(['pin']) : new Set();
        fd.forEach((v, k) => {
          if (v === '' && !allowEmptyKeys.has(k)) return;
          if (form.querySelector(`[name="${k}"]`).type === 'checkbox') {
            body[k] = form.querySelector(`[name="${k}"]`).checked;
          } else if (k.endsWith('_key') || k.endsWith('_pass') || k === 'opensky_user' || k === 'piaware_feeder_id') {
            secrets[k] = v;
          } else if (v !== '' && !isNaN(v)) {
            body[k] = Number(v);
          } else {
            body[k] = v;
          }
        });
        // Forms with a checkbox always need to send false too
        form.querySelectorAll('input[type=checkbox]').forEach((cb) => {
          body[cb.name] = cb.checked;
        });
        const payload = Object.keys(secrets).length ? { ...body, secrets } : body;
        try {
          await window.api.post(url, payload);
          flash(form, 'Saved');
        } catch (err) {
          flash(form, (err.data && err.data.error) || 'Save failed', true);
        }
      });
    });
  }

  function bindHardwareExtras() {
    const out = document.getElementById('buzzer-volume-out');
    const slider = document.querySelector('input[name="buzzer_volume"]');
    if (slider && out) {
      slider.addEventListener('input', () => out.textContent = slider.value);
    }

    const btn = document.getElementById('btn-buzzer-test');
    if (btn) {
      btn.addEventListener('click', async () => {
        try {
          const r = await window.api.post('/api/settings/hardware/buzzer/test');
          alert(r.message || (r.ok ? 'OK' : 'Failed'));
        } catch (e) { alert('Buzzer test failed'); }
      });
    }

    const btnAero = document.getElementById('btn-test-aeroapi');
    if (btnAero) btnAero.addEventListener('click', async () => {
      const r = await window.api.post('/api/settings/integrations/test/aeroapi');
      alert(r.message);
    });
    const btnOs = document.getElementById('btn-test-opensky');
    if (btnOs) btnOs.addEventListener('click', async () => {
      const r = await window.api.post('/api/settings/integrations/test/opensky');
      alert(r.message);
    });

    const btnCheck = document.getElementById('btn-update-check');
    const btnApply = document.getElementById('btn-update-apply');
    const out2 = document.getElementById('update-output');
    if (btnCheck) btnCheck.addEventListener('click', async () => {
      out2.textContent = 'Checking...';
      try {
        const r = await window.api.post('/api/settings/updates/check');
        out2.textContent = r.message || (r.ok ? 'Up to date' : 'Failed');
      } catch (e) { out2.textContent = 'Check failed'; }
    });
    if (btnApply) btnApply.addEventListener('click', async () => {
      if (!confirm('Apply updates and restart services?')) return;
      out2.textContent = 'Applying...';
      try {
        const r = await window.api.post('/api/settings/updates/apply');
        out2.textContent = r.message;
      } catch (e) { out2.textContent = 'Apply failed'; }
    });
  }

  function flash(form, msg, isErr) {
    let el = form.querySelector('.flash');
    if (!el) {
      el = document.createElement('div');
      el.className = 'flash muted';
      form.appendChild(el);
    }
    el.textContent = msg;
    el.style.color = isErr ? 'var(--accent-warn)' : 'var(--accent)';
    setTimeout(() => { el.textContent = ''; }, 2500);
  }
})();
