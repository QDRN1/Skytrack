/* SkyTrack Settings — touch-friendly shell.
 *
 * Responsibilities:
 *   - Section navigation (rail buttons ↔ section panes).
 *   - Generic form submission for any <form data-endpoint data-section>.
 *   - Slider live-output binding (.touch-slider with matching .slider-out).
 *   - Confirm modal for destructive actions.
 *   - Network / Diagnostics / Feeders status refreshers.
 *   - Backup, watchlist, saved WiFi, update, log-tail glue.
 */
(function () {

  // ------------------------------------------------------------------
  // Small DOM helpers
  // ------------------------------------------------------------------
  const $  = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

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

  function toast(msg, isErr) {
    let t = document.getElementById('settings-toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'settings-toast';
      t.className = 'settings-toast';
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.toggle('err', !!isErr);
    t.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.remove('show'), 2400);
  }

  // Poll /healthz until the backend answers (used by OTA apply, which
  // drops the current socket when skytrack-app restarts).
  async function waitForHealthz(attempts = 60, delayMs = 1000) {
    for (let i = 0; i < attempts; i++) {
      try {
        const r = await fetch('/healthz', { cache: 'no-store' });
        if (r.ok) return true;
      } catch (_) { /* still rebooting */ }
      await new Promise(r => setTimeout(r, delayMs));
    }
    return false;
  }

  function fmtNumber(n) {
    if (n == null || isNaN(n)) return '—';
    try { return Number(n).toLocaleString(); } catch (_) { return String(n); }
  }

  function fmtBytes(n) {
    if (n == null || isNaN(n)) return '—';
    const u = ['B','KB','MB','GB','TB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + ' ' + u[i];
  }

  function fmtUptime(sec) {
    if (sec == null) return '—';
    sec = Number(sec);
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${m}m`;
    return `${m}m`;
  }

  // ------------------------------------------------------------------
  // Section navigation
  // ------------------------------------------------------------------
  function bindRail() {
    const rail = $('#settings-rail');
    if (!rail) return;
    const buttons = $$('.rail-btn', rail);
    const panes = $$('.settings-section');

    function show(name) {
      buttons.forEach(b => b.classList.toggle('active', b.dataset.section === name));
      panes.forEach(p => p.classList.toggle('active', p.dataset.sectionPane === name));
      window.scrollTo(0, 0);
      try {
        const u = new URL(window.location.href);
        u.hash = name;
        history.replaceState(null, '', u.toString());
      } catch (_) {}
      // Lazy refresh hooks: kick off live data when a section becomes visible.
      if (name === 'general')       refreshSystemStatus();
      if (name === 'network')      refreshNetworkStatus();
      if (name === 'feeders')      refreshFeederStatus();
      if (name === 'diagnostics')  refreshDiagnostics();
      if (name === 'data')       { refreshDataUsage(); refreshAircraftLog(); }
      if (name === 'updates')      refreshBackups();
      if (name === 'alerts')     { refreshWatchlist(); refreshHardwareStatus(); }
    }

    buttons.forEach(b => b.addEventListener('click', () => show(b.dataset.section)));

    // Cross-section "jump" links (e.g. Access → Network).
    $$('[data-jump-section]').forEach(b => {
      b.addEventListener('click', () => show(b.dataset.jumpSection));
    });

    const initial = (window.location.hash || '').replace('#', '') || 'general';
    show(initial);
  }

  // ------------------------------------------------------------------
  // Generic form submitter
  // ------------------------------------------------------------------
  const SECRET_KEYS = new Set([
    'aeroapi_key', 'opensky_password', 'openweather_api_key',
    'fr24_key', 'piaware_feeder_id', 'flightaware_id',
  ]);

  function isCheckbox(el) { return el && el.type === 'checkbox'; }
  function isNumberish(el) { return el && (el.type === 'number' || el.type === 'range'); }

  function buildPayload(form) {
    const body = {};
    const secrets = {};
    const allowEmpty = new Set(
      (form.dataset.allowEmpty || '').split(',').map(s => s.trim()).filter(Boolean)
    );

    // Checkboxes: always send a bool (incl. unchecked → false).
    $$('input[type=checkbox][name]', form).forEach(cb => {
      body[cb.name] = !!cb.checked;
    });

    // Radios + selects + text + number + range.
    $$('input[name], select[name], textarea[name]', form).forEach(el => {
      if (isCheckbox(el)) return;
      if (el.type === 'radio' && !el.checked) return;
      const k = el.name;
      let v = el.value;
      if (v === '' && !allowEmpty.has(k)) {
        // Skip empty fields unless explicitly allowed (e.g. PIN clear).
        // But for radios with no selection we already returned above.
        return;
      }
      if (SECRET_KEYS.has(k)) {
        secrets[k] = v;
        return;
      }
      if (isNumberish(el) && v !== '' && !isNaN(v)) {
        body[k] = Number(v);
      } else {
        body[k] = v;
      }
    });

    return Object.keys(secrets).length ? { ...body, secrets } : body;
  }

  // Track the server-side value of every reboot-sensitive field per-form so
  // we can diff it against whatever the user ends up submitting.
  const _rebootBaseline = new WeakMap();

  function currentRebootValue(form, key) {
    const input = form.querySelector(`[name="${key}"]`);
    if (!input) return '';
    if (input.type === 'checkbox') return input.checked ? '1' : '0';
    if (input.type === 'radio') {
      const checked = form.querySelector(`[name="${key}"]:checked`);
      return checked ? checked.value : '';
    }
    return String(input.value);
  }

  function snapshotRebootFields(form) {
    const snap = {};
    $$('[data-reboot-field]', form).forEach(el => {
      snap[el.dataset.rebootField] = currentRebootValue(form, el.dataset.rebootField);
    });
    return snap;
  }

  async function maybeOfferReboot(form) {
    const before = _rebootBaseline.get(form) || {};
    const changed = [];
    Object.keys(before).forEach(k => {
      if (currentRebootValue(form, k) !== before[k]) changed.push(k);
    });
    // Refresh the baseline so a second save only prompts on the next change.
    _rebootBaseline.set(form, snapshotRebootFields(form));
    if (!changed.length) return;
    const pretty = {
      display_rotation:           'Screen rotation',
      display_brightness:         'Brightness',
      display_fullscreen_on_boot: 'Fullscreen on boot',
    };
    const names = changed.map(k => pretty[k] || k).join(', ');
    const ok = await confirmModal(
      'Reboot to apply?',
      `${names} — these settings take effect on the next boot. Reboot now?`
    );
    if (!ok) {
      toast('Saved — will apply on next reboot');
      return;
    }
    try {
      await window.api.post('/api/settings/updates/reboot');
      toast('Rebooting…');
    } catch (_) {
      toast('Reboot failed — reboot manually later', true);
    }
  }

  function bindForms() {
    $$('form[data-endpoint]').forEach(form => {
      // Capture the server-side initial value of any apply-on-reboot fields.
      _rebootBaseline.set(form, snapshotRebootFields(form));

      // Cancel button → reset the form to its initial markup state.
      const cancel = form.querySelector('[data-action=reset]');
      if (cancel) cancel.addEventListener('click', () => form.reset());

      // Confirm-of: keep a confirm field in sync with its source field.
      const confirms = $$('[data-confirm-of]', form);
      confirms.forEach(c => {
        const target = form.querySelector(`[name="${c.dataset.confirmOf}"]`);
        const check = () => {
          if (c.value && target.value && c.value !== target.value) {
            c.setCustomValidity("Values don't match");
          } else {
            c.setCustomValidity('');
          }
        };
        c.addEventListener('input', check);
        target && target.addEventListener('input', check);
      });

      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (!form.reportValidity()) return;
        const payload = buildPayload(form);
        const url = form.dataset.endpoint;
        const submitBtn = form.querySelector('button[type=submit]');
        if (submitBtn) submitBtn.disabled = true;
        try {
          const r = await window.api.post(url, payload);
          flash(form, (r && r.message) || 'Saved');
          toast('Saved');
          // Clear write-only secret inputs after save so the (stored) hint reappears next render.
          $$('input[type=password]', form).forEach(p => { p.value = ''; });
          // Trigger any post-save refresher tied to this section.
          const sec = form.dataset.section;
          if (sec === 'network')     refreshNetworkStatus();
          if (sec === 'diagnostics') refreshDiagnostics();
          // Apply theme + animation changes immediately so the operator
          // sees the result without reloading.
          if (payload.default_theme && window.portalTheme) {
            window.portalTheme.setMode(payload.default_theme);
          }
          if (payload.display_animation_level != null || payload.display_particle_density != null) {
            applyDisplaySettingsLive(payload);
          }
          // Offer a reboot if the user touched anything flagged as
          // apply-on-reboot in this form.
          await maybeOfferReboot(form);
        } catch (err) {
          const msg = (err.data && err.data.error) || err.message || 'Save failed';
          flash(form, msg, true);
          toast(msg, true);
        } finally {
          if (submitBtn) submitBtn.disabled = false;
        }
      });
    });
  }

  // ------------------------------------------------------------------
  // Sliders with live <output>
  // ------------------------------------------------------------------
  function bindSliders() {
    $$('input.touch-slider').forEach(s => {
      const out = document.getElementById(s.id + '-out');
      if (!out) return;
      const sync = () => { out.textContent = s.value; };
      s.addEventListener('input', sync);
      sync();
    });
  }

  // ------------------------------------------------------------------
  // Apply display/animation settings live (no reload required)
  // ------------------------------------------------------------------
  function applyDisplaySettingsLive(payload) {
    var root = document.documentElement;
    if (payload.display_animation_level != null) {
      root.setAttribute('data-anim', payload.display_animation_level);
    }
    if (payload.display_particle_density != null) {
      root.setAttribute('data-particle-density', String(payload.display_particle_density));
    }
    // Particles module reads data-* once at boot. Reload to re-init with new values.
    if (payload.display_animation_level != null || payload.display_particle_density != null) {
      setTimeout(function () { location.reload(); }, 600);
    }
  }

  // ------------------------------------------------------------------
  // Confirm modal
  //
  // Priority:
  //   1. The settings page has its own #confirm-modal (richer layout).
  //   2. Otherwise we fall back to the global uiModal helper.
  //   3. Never fall back to window.confirm — kiosks don't render it well.
  // ------------------------------------------------------------------
  let _confirmResolve = null;
  function confirmModal(title, body) {
    const modal = $('#confirm-modal');
    if (modal) {
      $('#confirm-title', modal).textContent = title || 'Are you sure?';
      $('#confirm-body',  modal).textContent = body  || '';
      modal.hidden = false;
      return new Promise(resolve => { _confirmResolve = resolve; });
    }
    if (window.uiModal && window.uiModal.confirm) {
      return window.uiModal.confirm(body || '', title || 'Are you sure?');
    }
    // Last-ditch — never hit in prod because modal.js loads in base.html.
    return Promise.resolve(window.confirm(`${title}\n\n${body || ''}`));
  }
  function bindConfirmModal() {
    const modal = $('#confirm-modal');
    if (!modal) return;
    $$('[data-confirm-cancel]', modal).forEach(el => {
      el.addEventListener('click', () => { modal.hidden = true; _confirmResolve && _confirmResolve(false); _confirmResolve = null; });
    });
    $('#confirm-ok', modal).addEventListener('click', () => {
      modal.hidden = true; _confirmResolve && _confirmResolve(true); _confirmResolve = null;
    });
  }

  // Buttons that opt into the confirm modal via data-confirm-* attrs.
  function withConfirm(btn, fn) {
    const title = btn.dataset.confirmTitle;
    const body  = btn.dataset.confirmBody;
    if (!title) return fn();
    return confirmModal(title, body).then(ok => { if (ok) return fn(); });
  }

  // ------------------------------------------------------------------
  // Network section
  // ------------------------------------------------------------------
  async function refreshNetworkStatus() {
    if (!$('#net-cell-tile')) return;
    try {
      const r = await window.api.get('/api/settings/network/status');
      const setTile = (id, tile) => {
        const t = $('#' + id + '-tile');
        const v = $('#' + id + '-value');
        const s = $('#' + id + '-sub');
        if (!t) return;
        const state = (tile && tile.state) || 'off';
        if (v) v.textContent = state;
        if (s) s.textContent = (tile && tile.detail) || '';
        t.classList.toggle('is-on',     state === 'on' || state === 'online');
        t.classList.toggle('is-off',    state === 'off' || state === 'offline');
        t.classList.toggle('is-active', !!(tile && tile.active));
      };
      setTile('net-cell', r.cellular);
      setTile('net-wifi', r.wifi);
      setTile('net-hot',  r.hotspot);
      setTile('net-inet', r.internet);
      if (r.hotspot && r.hotspot.ssid) {
        const el = $('#net-hot-ssid'); if (el) el.textContent = r.hotspot.ssid;
      }
      // Reflect the LIVE APN (read straight off the gsm connection) in
      // the input so the operator sees what NetworkManager is actually
      // using, not whatever stale value happens to be in config.yaml.
      const apnIn = $('#net-cell-apn');
      if (apnIn && r.cellular && r.cellular.apn && document.activeElement !== apnIn) {
        apnIn.value = r.cellular.apn;
      }
      // Primary-link banner
      const banner = $('#net-primary-banner');
      if (banner) {
        const pretty = {
          cellular: 'Cellular',
          wifi:     'WiFi client',
          ethernet: 'Ethernet',
          other:    'other link',
          none:     'no uplink',
        }[r.primary || 'none'] || r.primary;
        const iface = r.primary_interface ? ` (${r.primary_interface})` : '';
        if ((r.primary || 'none') === 'none') {
          banner.textContent = 'Not reaching the internet right now.';
          banner.dataset.tone = 'warn';
        } else {
          banner.textContent = `Active uplink: ${pretty}${iface}`;
          banner.dataset.tone = 'ok';
        }
      }
    } catch (e) { /* leave placeholders */ }
    refreshSavedWifi();
  }

  async function refreshSavedWifi() {
    const list = $('#saved-wifi-list');
    if (!list) return;
    try {
      const r = await window.api.get('/api/settings/network/wifi/saved');
      const items = (r && r.networks) || [];
      if (!items.length) {
        list.innerHTML = '<li class="muted">No saved networks yet.</li>';
        return;
      }
      list.innerHTML = '';
      items.forEach(net => {
        const li = document.createElement('li');
        li.className = 'saved-wifi-item';
        li.innerHTML = `
          <span class="sw-ssid">${escapeHtml(net.ssid)}</span>
          <span class="muted small">${net.connected ? 'connected' : ''}</span>
          <button type="button" class="btn" data-wifi-connect="${escapeAttr(net.ssid)}">Connect</button>
          <button type="button" class="btn btn-danger" data-wifi-forget="${escapeAttr(net.ssid)}">Forget</button>`;
        list.appendChild(li);
      });
    } catch (_) {
      list.innerHTML = '<li class="muted">Could not load saved networks.</li>';
    }
  }

  function bindNetworkExtras() {
    const showBtn = $('#btn-hot-show');
    if (showBtn) showBtn.addEventListener('click', async () => {
      const code = $('#net-hot-pw');
      if (!code) return;
      if (showBtn.dataset.shown === '1') {
        code.textContent = '••••••••••';
        showBtn.textContent = 'Show';
        showBtn.dataset.shown = '';
        return;
      }
      try {
        const r = await window.api.get('/api/settings/network/hotspot/password');
        code.textContent = r.password || '—';
        showBtn.textContent = 'Hide';
        showBtn.dataset.shown = '1';
      } catch (_) { toast('Could not reveal password', true); }
    });

    // Hotspot password regenerate — single canonical implementation.
    // Both Settings → Network and the legacy /network page eventually
    // call this same handler if they live on the same page; the network
    // page also has its own JS that hits the same backend endpoint.
    const regen = $('#btn-hot-regen');
    if (regen) regen.addEventListener('click', async () => {
      const ok = await confirmModal(
        'Regenerate hotspot password?',
        'Anyone currently connected will be kicked off and will need the new password.'
      );
      if (!ok) return;
      regen.disabled = true;
      try {
        const r = await window.api.post('/api/settings/network/hotspot/regenerate');
        if (r && r.password) {
          const code = $('#net-hot-pw');
          if (code) {
            code.textContent = r.password;
            const showBtn = $('#btn-hot-show');
            if (showBtn) {
              showBtn.textContent = 'Hide';
              showBtn.dataset.shown = '1';
            }
          }
          toast('Hotspot password regenerated');
        } else {
          toast('Regenerate returned no password', true);
        }
      } catch (e) {
        const msg = (e && e.data && (e.data.message || e.data.error)) || 'Failed to regenerate';
        toast(msg, true);
        if (window.uiModal) window.uiModal.alert(msg, 'Hotspot password');
      } finally {
        regen.disabled = false;
      }
    });

    const restartHs = $('#btn-hot-restart');
    if (restartHs) restartHs.addEventListener('click', async () => {
      const ok = await confirmModal(
        'Restart hotspot?',
        'You may lose this session for ~10 seconds if you are connected over the hotspot.'
      );
      if (!ok) return;
      restartHs.disabled = true;
      try {
        await window.api.post('/api/settings/network/hotspot/restart');
        toast('Hotspot restarting');
      } catch (e) {
        const msg = (e && e.data && (e.data.message || e.data.error)) || 'Restart failed';
        toast(msg, true);
      } finally {
        restartHs.disabled = false;
      }
    });

    // Hotspot auto-start switch (single-flag form-less toggle).
    const autostart = $('#net-hot-autostart');
    if (autostart) autostart.addEventListener('change', async () => {
      try {
        await window.api.post('/api/settings/network', { hotspot_auto_start: !!autostart.checked });
        toast('Saved');
      } catch (e) { toast('Save failed', true); autostart.checked = !autostart.checked; }
    });

    // Add new WiFi.
    const addForm = $('#form-wifi-add');
    if (addForm) addForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(addForm);
      const payload = { ssid: fd.get('ssid'), password: fd.get('password') || '' };
      try {
        await window.api.post('/api/settings/network/wifi/add', payload);
        addForm.reset();
        toast('WiFi network saved');
        refreshSavedWifi();
      } catch (e) { toast('Could not save network', true); }
    });

    // APN apply button — fires the dedicated endpoint so the value
    // reaches NetworkManager even when the operator hasn't submitted the
    // main Connectivity form yet. Surfaces the live nmcli error verbatim
    // when polkit / modem state rejects the change instead of pretending
    // it succeeded.
    const apnBtn = $('#btn-cell-apn-apply');
    const apnIn  = $('#net-cell-apn');
    if (apnBtn && apnIn) apnBtn.addEventListener('click', async () => {
      const apn = (apnIn.value || '').trim();
      if (!apn) {
        toast('APN cannot be empty', true);
        return;
      }
      apnBtn.disabled = true;
      try {
        const r = await window.api.post('/api/settings/network/cellular/apn', { apn });
        if (r && r.ok) {
          const tail = r.applied ? ' (live)' : ' (saved — modem will pick it up)';
          toast(`APN saved: ${apn}${tail}`);
          // Re-pull live status so the field reflects what NM is actually using.
          refreshNetworkStatus();
        } else {
          const msg = (r && r.message) || 'APN save failed';
          toast(msg, true);
          if (window.uiModal) window.uiModal.alert(msg, 'APN');
        }
      } catch (e) {
        const msg = (e && e.data && (e.data.message || e.data.error)) || 'APN save failed';
        toast(msg, true);
        if (window.uiModal) window.uiModal.alert(msg, 'APN');
      } finally {
        apnBtn.disabled = false;
      }
    });

    // WiFi scan button — delegates to the new backend-agnostic endpoint.
    const scanBtn = $('#btn-wifi-scan');
    const scanList = $('#wifi-scan-list');
    const scanStatus = $('#wifi-scan-status');
    if (scanBtn && scanList) scanBtn.addEventListener('click', async () => {
      scanBtn.disabled = true;
      if (scanStatus) scanStatus.textContent = 'Scanning…';
      scanList.innerHTML = '<li class="muted">Scanning nearby networks…</li>';
      try {
        const r = await window.api.get('/api/settings/network/wifi/scan');
        if (!r.ok) {
          scanList.innerHTML = `<li class="muted">${escapeHtml(r.error || 'Scan unavailable on this device.')}</li>`;
          if (scanStatus) scanStatus.textContent = 'Scan unavailable.';
          return;
        }
        const nets = r.networks || [];
        if (!nets.length) {
          scanList.innerHTML = '<li class="muted">No networks visible right now.</li>';
          if (scanStatus) scanStatus.textContent = 'No networks found.';
          return;
        }
        scanList.innerHTML = '';
        nets.forEach(n => {
          const li = document.createElement('li');
          li.className = 'saved-wifi-item';
          const lock = (n.security && n.security !== '' && n.security !== '--') ? '🔒' : '🔓';
          const inUse = n.in_use ? '<span class="muted small"> · connected</span>' : '';
          li.innerHTML = `
            <span class="sw-ssid">${lock} ${escapeHtml(n.ssid)}${inUse}</span>
            <span class="muted small">${n.signal || 0}%</span>
            <button type="button" class="btn" data-wifi-join="${escapeAttr(n.ssid)}" data-wifi-sec="${escapeAttr(n.security || '')}">Join</button>`;
          scanList.appendChild(li);
        });
        if (scanStatus) scanStatus.textContent = `${nets.length} network${nets.length === 1 ? '' : 's'} found.`;
      } catch (e) {
        scanList.innerHTML = '<li class="muted">Scan failed — check admin role and network service.</li>';
        if (scanStatus) scanStatus.textContent = 'Scan failed.';
      } finally {
        scanBtn.disabled = false;
      }
    });

    // Delegated join handler for scan results — prompts for password if
    // the AP is secured, then hits the live wifi/connect endpoint.
    document.addEventListener('click', async (e) => {
      const j = e.target.closest('[data-wifi-join]');
      if (!j) return;
      const ssid = j.dataset.wifiJoin;
      const sec  = j.dataset.wifiSec || '';
      let password = null;
      if (sec && sec !== '' && sec !== '--') {
        password = await window.uiModal.prompt(
          `Enter the password for ${ssid}.`, '', 'Join Wi-Fi'
        );
        if (password === null) return;
      }
      try {
        const r = await window.api.post('/api/settings/network/wifi/connect', { ssid, password });
        if (r.ok) {
          toast(`Joining ${ssid}…`);
          refreshNetworkStatus();
          refreshSavedWifi();
        } else {
          window.uiModal.alert(r.message || 'Connect failed.', 'Join Wi-Fi');
        }
      } catch (err) {
        window.uiModal.alert('Connect failed. Check the password and try again.', 'Join Wi-Fi');
      }
    });

    // Connect / forget delegated clicks.
    document.addEventListener('click', async (e) => {
      const c = e.target.closest('[data-wifi-connect]');
      if (c) {
        const ssid = c.dataset.wifiConnect;
        try { await window.api.post('/api/settings/network/wifi/connect', { ssid }); toast(`Connecting to ${ssid}`); refreshNetworkStatus(); }
        catch (_) { toast('Connect failed', true); }
        return;
      }
      const f = e.target.closest('[data-wifi-forget]');
      if (f) {
        const ssid = f.dataset.wifiForget;
        const ok = await confirmModal('Forget network?', `Remove saved credentials for ${ssid}.`);
        if (!ok) return;
        try { await window.api.post('/api/settings/network/wifi/forget', { ssid }); toast('Forgotten'); refreshSavedWifi(); }
        catch (_) { toast('Forget failed', true); }
      }
    });
  }

  // ------------------------------------------------------------------
  // Feeders section
  // ------------------------------------------------------------------
  async function refreshFeederStatus() {
    if (!$('#feed-dump-tile')) return;
    try {
      var r = await window.api.get('/api/settings/feeders/status');
      var setTile = function (id, st) {
        var t = $('#feed-' + id + '-tile');
        var v = $('#feed-' + id + '-value');
        var s = $('#feed-' + id + '-sub');
        var state = (st && st.state) || 'off';
        var label = {
          on:      'running',
          off:     'stopped',
          missing: 'not installed',
        }[state] || state;
        if (v) v.textContent = label;
        if (s) s.textContent = (st && st.detail) || '';
        if (t) {
          t.classList.toggle('is-on',      state === 'on');
          t.classList.toggle('is-off',     state === 'off');
          t.classList.toggle('is-missing', state === 'missing');
        }
      };
      setTile('dump', r.dump1090);
      setTile('fr24', r.fr24);
      setTile('pia',  r.piaware);

      var adsbInstalled = r.dump1090 && r.dump1090.state !== 'missing';
      var fr24Installed = r.fr24 && r.fr24.state !== 'missing';
      var adsbBtn = $('#btn-install-adsb');
      var fr24Btn = $('#btn-install-fr24');
      if (adsbBtn) {
        if (adsbInstalled) {
          adsbBtn.textContent = 'Reinstall ADS-B stack';
          adsbBtn.classList.remove('btn-primary');
        } else {
          adsbBtn.textContent = 'Install ADS-B stack';
          adsbBtn.classList.add('btn-primary');
        }
      }
      if (fr24Btn) {
        if (fr24Installed) {
          fr24Btn.textContent = 'Reinstall FR24 feeder';
          fr24Btn.classList.remove('btn-primary');
        } else {
          fr24Btn.textContent = 'Install FR24 feeder';
          fr24Btn.classList.add('btn-primary');
        }
      }
    } catch (_) {}
  }

  function bindFeederExtras() {
    const restartDump = $('#btn-restart-dump1090');
    if (restartDump) restartDump.addEventListener('click', async () => {
      const ok = await confirmModal('Restart dump1090?', 'The receiver will drop its current aircraft list briefly.');
      if (!ok) return;
      try { await window.api.post('/api/settings/feeders/dump1090/restart'); toast('dump1090 restarting'); refreshFeederStatus(); }
      catch (_) { toast('Restart failed', true); }
    });

    const restartFeeders = $('#btn-restart-feeders');
    if (restartFeeders) restartFeeders.addEventListener('click', async () => {
      const ok = await confirmModal('Restart feeders?', 'PiAware and FR24 will briefly disconnect and reconnect.');
      if (!ok) return;
      try { await window.api.post('/api/settings/feeders/restart'); toast('Feeders restarting'); refreshFeederStatus(); }
      catch (_) { toast('Restart failed', true); }
    });

    function pollInstallJob(jobId, btn, out, label) {
      var poll = setInterval(async function () {
        try {
          var s = await window.api.get('/api/settings/install/status/' + jobId);
          if (out) {
            var lines = '';
            if (s.current) lines += 'Installing ' + s.current + '…\n';
            lines += s.elapsed + 's elapsed';
            if (s.output) lines += '\n\n' + s.output;
            out.textContent = lines;
            out.scrollTop = out.scrollHeight;
          }
          if (s.status === 'done' || s.status === 'failed') {
            clearInterval(poll);
            if (out) {
              out.textContent = s.output || (s.ok ? 'Done.' : 'Failed.');
              out.scrollTop = out.scrollHeight;
            }
            toast(s.ok ? (label + ' installed') : (label + ' had errors — see log'), !s.ok);
            btn.disabled = false;
            btn.textContent = 'Install ' + label;
            refreshFeederStatus();
          }
        } catch (e) {
          clearInterval(poll);
          if (out) out.textContent += '\nLost contact with server: ' + (e.message || e) + '\n';
          btn.disabled = false;
          btn.textContent = 'Install ' + label;
          refreshFeederStatus();
        }
      }, 3000);
    }

    function bindInstall(btnId, outId, url, label) {
      var btn = $('#' + btnId);
      var out = $('#' + outId);
      if (!btn) return;
      btn.addEventListener('click', async function () {
        var ok = await confirmModal(
          'Install ' + label + '?',
          'This will download and install packages. The device needs internet access. ' +
          'Building from source may take 5–10 minutes.'
        );
        if (!ok) return;
        btn.disabled = true;
        btn.textContent = 'Installing…';
        if (out) { out.hidden = false; out.textContent = 'Starting installation…\n'; }
        toast('Installing ' + label + '…');
        try {
          var r = await window.api.post(url);
          if (r && r.job_id) {
            if (out) out.textContent = 'Installation started (job ' + r.job_id + ')…\n';
            pollInstallJob(r.job_id, btn, out, label);
          } else {
            if (out) out.textContent = 'Unexpected response: ' + JSON.stringify(r) + '\n';
            btn.disabled = false;
            btn.textContent = 'Install ' + label;
          }
        } catch (e) {
          var msg = (e && e.data && e.data.error) || e.message || 'Request failed';
          if (out) out.textContent = 'ERROR: ' + msg + '\n';
          toast(msg, true);
          btn.disabled = false;
          btn.textContent = 'Install ' + label;
        }
      });
    }

    bindInstall('btn-install-adsb', 'adsb-install-output',
                '/api/settings/feeders/dump1090/install', 'ADS-B stack');
    bindInstall('btn-install-fr24', 'fr24-install-output',
                '/api/settings/feeders/fr24/install', 'FR24 feeder');
  }

  // ------------------------------------------------------------------
  // Data section
  // ------------------------------------------------------------------
  async function refreshDataUsage() {
    if (!$('#data-usage-box')) return;
    try {
      const r = await window.api.get('/api/settings/data/usage');
      const sz = $('#data-db-size'); if (sz) sz.textContent = fmtBytes(r.db_size);
      const fr = $('#data-disk-free'); if (fr) fr.textContent = fmtBytes(r.disk_free);
      const dp = $('#data-db-path');  if (dp && r.db_path) dp.textContent = r.db_path;
    } catch (_) {}
  }

  function bindDataExtras() {
    const wireAction = (id, url, defaultTitle, defaultBody, success) => {
      const btn = $('#' + id);
      if (!btn) return;
      btn.addEventListener('click', async () => {
        const ok = await confirmModal(
          btn.dataset.confirmTitle || defaultTitle,
          btn.dataset.confirmBody  || defaultBody
        );
        if (!ok) return;
        try { await window.api.post(url); toast(success); refreshDataUsage(); }
        catch (e) { toast('Failed', true); }
      });
    };
    wireAction('btn-data-vacuum', '/api/settings/data/vacuum', 'Vacuum database now?', 'This rewrites the SQLite file. Brief I/O spike.', 'Vacuumed');
    wireAction('btn-data-prune',  '/api/settings/data/prune',  'Prune old rows?',     'This removes flight history older than your retention setting.', 'Pruned');
    wireAction('btn-data-wipe',   '/api/settings/data/wipe',   'Wipe all flight history?', 'This deletes every aircraft, flight, and enrichment row. Settings are kept.', 'Wiped');

    // Aircraft log
    refreshAircraftLog();
    const loadMore = $('#alog-load-more');
    if (loadMore) loadMore.addEventListener('click', () => {
      alogOffset += 100;
      refreshAircraftLog(true);
    });
    const resetAll = $('#btn-alog-reset-all');
    if (resetAll) resetAll.addEventListener('click', async () => {
      const ok = await confirmModal(
        resetAll.dataset.confirmTitle || 'Reset all sighting counts?',
        resetAll.dataset.confirmBody  || 'Deletes the entire aircraft log.'
      );
      if (!ok) return;
      try {
        await window.api.post('/api/dashboard/aircraft-log/reset', {});
        toast('Aircraft log reset');
        alogOffset = 0;
        refreshAircraftLog();
      } catch (_) { toast('Failed to reset', true); }
    });
  }

  let alogOffset = 0;
  async function refreshAircraftLog(append) {
    try {
      const stats = await window.api.get('/api/dashboard/aircraft-log/stats');
      const ta = $('#alog-total-aircraft');
      const ts = $('#alog-total-sightings');
      const te = $('#alog-earliest');
      if (ta) ta.textContent = fmtNumber(stats.total_aircraft || 0);
      if (ts) ts.textContent = fmtNumber(stats.total_sightings || 0);
      if (te) te.textContent = stats.earliest ? new Date(stats.earliest).toLocaleDateString() : '—';
    } catch (_) {}

    try {
      const data = await window.api.get('/api/dashboard/aircraft-log?sort=count&limit=100&offset=' + (append ? alogOffset : 0));
      const tbody = $('#aircraft-log-tbody');
      if (!tbody) return;
      if (!append) tbody.innerHTML = '';
      (data.aircraft || []).forEach(ac => {
        const tr = document.createElement('tr');
        tr.innerHTML =
          '<td><code>' + escapeHtml(ac.icao.toUpperCase()) + '</code></td>' +
          '<td>' + escapeHtml(ac.callsign || '—') + '</td>' +
          '<td>' + escapeHtml(ac.airline || '—') + '</td>' +
          '<td><strong>' + fmtNumber(ac.sighting_count) + '</strong></td>' +
          '<td class="muted small">' + fmtDateShort(ac.first_seen) + '</td>' +
          '<td class="muted small">' + fmtDateShort(ac.last_seen) + '</td>' +
          '<td><button type="button" class="btn btn-sm btn-danger alog-remove" data-icao="' + escapeAttr(ac.icao) + '">×</button></td>';
        tbody.appendChild(tr);
      });
      const more = $('#alog-load-more');
      if (more) more.hidden = (data.aircraft || []).length < 100;

      tbody.querySelectorAll('.alog-remove').forEach(btn => {
        btn.addEventListener('click', async () => {
          try {
            await window.api.post('/api/dashboard/aircraft-log/reset', { icao: btn.dataset.icao });
            btn.closest('tr').remove();
            toast('Removed ' + btn.dataset.icao.toUpperCase());
          } catch (_) { toast('Failed', true); }
        });
      });
    } catch (_) {}
  }

  function fmtDateShort(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: '2-digit' }); }
    catch (_) { return iso.slice(0, 10); }
  }

  // ------------------------------------------------------------------
  // Alerts section / watchlist
  // ------------------------------------------------------------------
  async function refreshWatchlist() {
    const list = $('#watchlist-list');
    if (!list) return;
    try {
      const r = await window.api.get('/api/settings/alerts/watchlist');
      const items = (r && r.items) || [];
      if (!items.length) {
        list.innerHTML = '<li class="muted">No watchlist entries yet.</li>';
        return;
      }
      list.innerHTML = '';
      items.forEach(it => {
        const li = document.createElement('li');
        li.className = 'saved-wifi-item';
        li.innerHTML = `
          <span class="sw-ssid">${escapeHtml(it.ident)}</span>
          <span class="muted small">${escapeHtml(it.note || '')}</span>
          <button type="button" class="btn btn-danger" data-watch-remove="${escapeAttr(it.ident)}">Remove</button>`;
        list.appendChild(li);
      });
    } catch (_) {
      list.innerHTML = '<li class="muted">Could not load watchlist.</li>';
    }
  }

  // Render the live hardware truthfulness panel (sensor + buzzer).
  // Lives inside the Alerts section so operators can immediately tell
  // whether the buzzer they're tuning will actually beep and whether the
  // temperature in the topbar is real DHT22 data or mock.
  async function refreshHardwareStatus() {
    const wrap = $('#hw-status');
    if (!wrap) return;
    try {
      const r = await window.api.get('/api/settings/hardware/status');
      const s = (r && r.sensor) || {};
      const b = (r && r.buzzer) || {};

      const setText = (id, txt) => { const el = $('#' + id); if (el) el.textContent = txt; };
      const setDot  = (id, ok) => {
        const el = $('#' + id);
        if (!el) return;
        el.classList.toggle('hw-ok',  !!ok);
        el.classList.toggle('hw-bad', !ok);
      };

      // Sensor row
      const srcLabel = ({
        dht22:        'DHT22 (live)',
        dht22_cached: 'DHT22 (cached)',
        mock:         'Mock (no sensor)',
      })[s.source] || (s.source || 'unknown');
      setDot('hw-sensor-dot', s.available && !s.mock);
      setText('hw-sensor-source', srcLabel);
      setText('hw-sensor-temp',
        s.temperature_f != null ? `${s.temperature_f}°F` : '—');
      setText('hw-sensor-pin', s.pin != null ? `pin ${s.pin}` : '');
      setText('hw-sensor-error', s.last_error || '');

      // Buzzer row
      setDot('hw-buzz-dot', b.available);
      const buzzState = !b.available
        ? 'not available'
        : b.enabled ? 'available, enabled' : 'available, disabled';
      setText('hw-buzz-state', buzzState);
      setText('hw-buzz-pin', b.pin ? `pin ${b.pin}` : '');
      setText('hw-buzz-error', b.last_error || '');
    } catch (_) {
      // Leave placeholders alone.
    }
  }

  function bindAlertExtras() {
    const f = $('#form-watchlist-add');
    if (f) f.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(f);
      try {
        await window.api.post('/api/settings/alerts/watchlist/add', { ident: fd.get('ident'), note: fd.get('note') || '' });
        f.reset(); toast('Added'); refreshWatchlist();
      } catch (_) { toast('Add failed', true); }
    });
    document.addEventListener('click', async (e) => {
      const r = e.target.closest('[data-watch-remove]');
      if (!r) return;
      const ident = r.dataset.watchRemove;
      const ok = await confirmModal('Remove from watchlist?', ident);
      if (!ok) return;
      try { await window.api.post('/api/settings/alerts/watchlist/remove', { ident }); toast('Removed'); refreshWatchlist(); }
      catch (_) { toast('Remove failed', true); }
    });

    // Buzzer test button — single source of truth for "does this beep".
    // Always toasts the structured backend result (available/enabled/error)
    // instead of a generic success/fail, so the operator can tell whether
    // it's a wiring problem, a config switch, or a permission issue.
    const buzz = $('#btn-buzzer-test');
    if (buzz) buzz.addEventListener('click', async () => {
      buzz.disabled = true;
      const orig = buzz.textContent;
      buzz.textContent = 'Beeping…';
      try {
        const r = await window.api.post('/api/settings/alerts/buzzer/test');
        if (r && r.ok) {
          toast('Buzzer test sent');
        } else {
          let msg = (r && r.message) || 'Buzzer test failed';
          if (r && r.last_error) msg += ` — ${r.last_error}`;
          if (r && r.available === false) msg += ' (no buzzer detected)';
          else if (r && r.enabled === false) msg += ' (buzzer disabled in config)';
          toast(msg, true);
          if (window.uiModal) window.uiModal.alert(msg, 'Buzzer test');
        }
      } catch (e) {
        const msg = (e && e.data && (e.data.message || e.data.error)) || 'Buzzer test failed';
        toast(msg, true);
        if (window.uiModal) window.uiModal.alert(msg, 'Buzzer test');
      } finally {
        buzz.textContent = orig;
        buzz.disabled = false;
        refreshHardwareStatus();
      }
    });

    const hwRefresh = $('#btn-hw-refresh');
    if (hwRefresh) hwRefresh.addEventListener('click', refreshHardwareStatus);
  }

  // ------------------------------------------------------------------
  // Updates / backup / power
  // ------------------------------------------------------------------
  async function refreshBackups() {
    const list = $('#backup-list');
    if (!list) return;
    try {
      const r = await window.api.get('/api/settings/updates/backups');
      const items = (r && r.backups) || [];
      if (!items.length) { list.innerHTML = '<li class="muted">No backups yet.</li>'; return; }
      list.innerHTML = '';
      items.forEach(b => {
        const li = document.createElement('li');
        li.className = 'saved-wifi-item';
        li.innerHTML = `
          <span class="sw-ssid">${escapeHtml(b.name)}</span>
          <span class="muted small">${escapeHtml(b.created || '')} · ${fmtBytes(b.size)}</span>
          <button type="button" class="btn" data-backup-restore="${escapeAttr(b.name)}">Restore</button>
          <button type="button" class="btn btn-danger" data-backup-delete="${escapeAttr(b.name)}">Delete</button>`;
        list.appendChild(li);
      });
    } catch (_) { list.innerHTML = '<li class="muted">Could not load backups.</li>'; }
  }

  function renderReleaseNotes(release, container) {
    if (!container) return;
    if (!release || !release.notes || !release.notes.length) {
      container.innerHTML = '<p class="muted">No release notes available.</p>';
      return;
    }
    let html = '';
    if (release.title) html += '<p style="margin:0 0 6px;font-weight:600;">' + escapeHtml(release.title) + '</p>';
    html += '<ul>';
    release.notes.forEach(n => { html += '<li>' + escapeHtml(n) + '</li>'; });
    html += '</ul>';
    container.innerHTML = html;
  }

  function renderReleaseHistory(releases, container) {
    if (!container) return;
    if (!releases || !releases.length) { container.innerHTML = '<p class="muted">No history available.</p>'; return; }
    let html = '';
    releases.forEach(rel => {
      const d = rel.date ? new Date(rel.date + 'T00:00:00').toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : '';
      html += '<div class="release-history-entry">';
      html += '<div class="release-header">';
      html += '<span class="release-version">v' + escapeHtml(rel.version) + '</span>';
      if (rel.title) html += '<span class="release-title">' + escapeHtml(rel.title) + '</span>';
      if (d) html += '<span class="release-date muted">' + escapeHtml(d) + '</span>';
      html += '</div>';
      if (rel.notes && rel.notes.length) {
        html += '<div class="release-notes"><ul>';
        rel.notes.forEach(n => { html += '<li>' + escapeHtml(n) + '</li>'; });
        html += '</ul></div>';
      }
      html += '</div>';
    });
    container.innerHTML = html;
  }

  function bindUpdateExtras() {
    const out = $('#update-output');
    const setOut = (t, show) => {
      if (!out) return;
      out.textContent = t;
      out.style.display = show ? '' : 'none';
    };

    // Load current release notes on page load
    (async function loadCurrentRelease() {
      try {
        const r = await window.api.get('/api/settings/updates');
        if (r && r.current_release) {
          renderReleaseNotes(r.current_release, $('#current-release-notes'));
          const dateEl = $('#current-release-date');
          if (dateEl && r.current_release.date) {
            dateEl.textContent = new Date(r.current_release.date + 'T00:00:00').toLocaleDateString(
              undefined, { year: 'numeric', month: 'long', day: 'numeric' }
            );
          }
        }
      } catch (_) {}
    })();

    const check = $('#btn-update-check');
    if (check) check.addEventListener('click', async () => {
      check.disabled = true;
      check.textContent = 'Checking…';
      setOut('', false);
      try {
        const r = await window.api.post('/api/settings/updates/check');
        if (!r.ok) {
          setOut(r.message || 'Check failed.', true);
          $('#upd-status-text').textContent = r.message || 'Check failed.';
        } else {
          const statusEl = $('#upd-status-text');
          const checkedTime = r.checked_at
            ? new Date(r.checked_at).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
            : 'just now';

          if (r.behind === 0) {
            if (statusEl) statusEl.textContent = 'You are up to date. Checked ' + checkedTime;
            const block = $('#latest-available-block');
            if (block) block.style.display = 'none';
          } else {
            if (statusEl) statusEl.textContent = 'Update available! Checked ' + checkedTime;
            const block = $('#latest-available-block');
            if (block) block.style.display = '';

            const verEl = $('#latest-version');
            if (verEl) verEl.textContent = r.remote_version ? 'v' + r.remote_version : 'New version';

            const dateEl = $('#latest-date');
            if (dateEl && r.remote_date) {
              dateEl.textContent = new Date(r.remote_date).toLocaleDateString(
                undefined, { year: 'numeric', month: 'long', day: 'numeric' }
              );
            }

            const badgeEl = $('#latest-badge');
            if (badgeEl && r.behind) {
              badgeEl.className = 'release-badge new';
              badgeEl.textContent = r.behind + ' update' + (r.behind === 1 ? '' : 's');
            }

            renderReleaseNotes(r.latest_release, $('#latest-release-notes'));
          }

          // Show full release history if available
          if (r.all_releases && r.all_releases.length > 1) {
            const histCard = $('#card-release-history');
            if (histCard) histCard.style.display = '';
            renderReleaseHistory(r.all_releases, $('#release-history-list'));
          }
        }
      } catch (_) { setOut('Check failed — connection error.', true); }
      finally { check.disabled = false; check.textContent = 'Check for updates'; }
    });

    const apply = $('#btn-update-apply');
    if (apply) apply.addEventListener('click', async () => {
      const ok = await confirmModal(apply.dataset.confirmTitle || 'Apply updates?', apply.dataset.confirmBody || '');
      if (!ok) return;
      setOut('Applying update…', true);
      try {
        const r = await window.api.post('/api/settings/updates/apply');
        setOut(r.message || (r.ok ? 'Update applied — service restarting.' : 'Apply failed'), true);
        if (r.ok) {
          setTimeout(() => waitForHealthz().then(() => location.reload()), 1500);
        }
      } catch (_) {
        setOut('Service restarting — reloading page…', true);
        setTimeout(() => waitForHealthz().then(() => location.reload()), 1500);
      }
    });

    const bk = $('#btn-backup-now');
    if (bk) bk.addEventListener('click', async () => {
      try { await window.api.post('/api/settings/updates/backup'); toast('Backup created'); refreshBackups(); }
      catch (_) { toast('Backup failed', true); }
    });

    document.addEventListener('click', async (e) => {
      const r = e.target.closest('[data-backup-restore]');
      if (r) {
        const name = r.dataset.backupRestore;
        const ok = await confirmModal('Restore backup?', `Replaces current config with ${name}. The app will restart.`);
        if (!ok) return;
        try { await window.api.post('/api/settings/updates/backup/restore', { name }); toast('Restoring…'); }
        catch (_) { toast('Restore failed', true); }
        return;
      }
      const d = e.target.closest('[data-backup-delete]');
      if (d) {
        const name = d.dataset.backupDelete;
        const ok = await confirmModal('Delete backup?', name);
        if (!ok) return;
        try { await window.api.post('/api/settings/updates/backup/delete', { name }); toast('Deleted'); refreshBackups(); }
        catch (_) { toast('Delete failed', true); }
      }
    });

    // Power buttons — toast the actual server response so a failed
    // reboot doesn't leave the operator looking at a green "Rebooting…"
    // for ten minutes. Backend returns {ok, message} from _power_action,
    // and we surface the message verbatim on failure.
    const power = (id, url, successMsg) => {
      const b = $('#' + id);
      if (!b) return;
      b.addEventListener('click', async () => {
        const ok = await confirmModal(b.dataset.confirmTitle, b.dataset.confirmBody);
        if (!ok) return;
        b.disabled = true;
        try {
          const r = await window.api.post(url);
          if (r && r.ok === false) {
            const msg = r.message || 'Failed';
            toast(msg, true);
            if (window.uiModal) window.uiModal.alert(msg, 'Power action');
          } else {
            toast((r && r.message) || successMsg);
          }
        } catch (e) {
          // The reboot path may drop the socket between dbus accepting
          // the call and Flask flushing the response; treat dropped
          // connections as success-in-progress only for reboot/shutdown.
          if (id === 'btn-reboot' || id === 'btn-shutdown') {
            toast(successMsg);
            return;
          }
          const msg = (e && e.data && (e.data.message || e.data.error)) || 'Failed';
          toast(msg, true);
          if (window.uiModal) window.uiModal.alert(msg, 'Power action');
        } finally {
          b.disabled = false;
        }
      });
    };
    power('btn-restart-app',     '/api/settings/updates/restart-app',     'App restarting');
    power('btn-restart-network', '/api/settings/updates/restart-network', 'Network restarting');
    power('btn-reboot',          '/api/settings/updates/reboot',          'Rebooting');
    power('btn-shutdown',        '/api/settings/updates/shutdown',        'Shutting down');

    // Restart-app special handling: wait for healthz then reload
    const restartBtn = $('#btn-restart-app');
    if (restartBtn) {
      const origHandler = restartBtn.onclick;
      restartBtn.addEventListener('click', () => {
        setTimeout(async () => {
          const up = await waitForHealthz(30, 1000);
          if (up) location.reload();
        }, 2000);
      });
    }
  }

  // ------------------------------------------------------------------
  // Diagnostics section
  // ------------------------------------------------------------------
  async function refreshDiagnostics() {
    if (!$('#diag-cpu-tile')) return;
    try {
      const r = await window.api.get('/api/settings/diagnostics/system');
      const set = (id, val, sub) => {
        const v = $('#' + id + '-value'); if (v) v.textContent = val || '—';
        const s = $('#' + id + '-sub');   if (s && sub != null) s.textContent = sub;
      };
      set('diag-cpu',    r.cpu_load != null ? r.cpu_load.toFixed(2) : '—', `${r.cpu_count || ''} cores`);
      set('diag-mem',    r.mem_used_pct != null ? r.mem_used_pct + '%' : '—', `${fmtBytes(r.mem_used)} / ${fmtBytes(r.mem_total)}`);
      set('diag-temp',   r.cpu_temp_c != null ? r.cpu_temp_c.toFixed(1) + '°C' : '—', '');
      set('diag-uptime', fmtUptime(r.uptime), '');
      const hn = $('#diag-hostname'); if (hn) hn.textContent = r.hostname || '—';
      const kn = $('#diag-kernel');   if (kn) kn.textContent = r.kernel || '—';
    } catch (_) {}

    const list = $('#svc-list');
    if (list) {
      try {
        const r = await window.api.get('/api/settings/diagnostics/services');
        const items = (r && r.services) || [];
        if (!items.length) { list.innerHTML = '<li class="muted">No services reported.</li>'; }
        else {
          list.innerHTML = '';
          items.forEach(s => {
            const li = document.createElement('li');
            li.className = 'svc-row svc-' + (s.state || 'unknown');
            li.innerHTML = `<span class="svc-name">${escapeHtml(s.name)}</span>
                            <span class="svc-state">${escapeHtml(s.state || '?')}</span>`;
            list.appendChild(li);
          });
        }
      } catch (_) { list.innerHTML = '<li class="muted">Could not query services.</li>'; }
    }

    const tail = $('#diag-log-tail');
    if (tail) {
      try {
        const r = await window.api.get('/api/settings/diagnostics/log-tail');
        tail.textContent = (r && r.lines && r.lines.join('\n')) || '(empty)';
      } catch (_) { tail.textContent = 'Could not load log.'; }
    }
  }

  function bindDiagnosticsExtras() {
    const r = $('#btn-svc-refresh');   if (r) r.addEventListener('click', refreshDiagnostics);
    const t = $('#btn-log-refresh');   if (t) t.addEventListener('click', refreshDiagnostics);
    const sc = $('#btn-selfcheck');
    if (sc) sc.addEventListener('click', async () => {
      const out = $('#selfcheck-output');
      if (out) { out.hidden = false; out.textContent = 'Running…'; }
      try {
        const r = await window.api.post('/api/settings/diagnostics/selfcheck');
        if (out) out.textContent = (r && r.report) || JSON.stringify(r, null, 2);
      } catch (_) { if (out) out.textContent = 'Self-check failed.'; }
    });
  }

  // ------------------------------------------------------------------
  // Tiny escape helpers
  // ------------------------------------------------------------------
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }
  function escapeAttr(s) { return escapeHtml(s); }

  // ------------------------------------------------------------------
  // System status banner (General section)
  // ------------------------------------------------------------------
  async function refreshSystemStatus() {
    const banner = $('#system-status-banner');
    if (!banner) return;
    try {
      const r = await window.api.get('/api/settings/system-status');
      banner.dataset.level = r.overall || 'green';
      const label = $('#sys-status-label');
      const items = $('#sys-status-items');
      const labels = { green: 'All systems nominal', amber: 'Attention needed', red: 'Critical issue' };
      if (label) label.textContent = labels[r.overall] || 'Unknown';
      if (items && Array.isArray(r.checks)) {
        const parts = r.checks
          .filter(c => c.level !== 'green')
          .map(c => c.name + ': ' + c.detail);
        if (parts.length) {
          items.textContent = parts.join(' · ');
        } else {
          items.textContent = r.checks.map(c => c.name + ': ' + c.detail).join(' · ');
        }
      }
    } catch (_) {
      banner.dataset.level = '';
      const label = $('#sys-status-label');
      if (label) label.textContent = 'Could not check system health';
    }
  }

  // ------------------------------------------------------------------
  // Kiosk display card (lives in Display section)
  // ------------------------------------------------------------------
  function bindKioskCards() {
    const slider = document.getElementById('kiosk-interval');
    const label = document.getElementById('kiosk-interval-label');
    if (slider && label) slider.addEventListener('input', () => {
      label.textContent = slider.value + 's';
    });
    const mapSlider = document.getElementById('kiosk-map-interval');
    const mapLabel = document.getElementById('kiosk-map-interval-label');
    if (mapSlider && mapLabel) mapSlider.addEventListener('input', () => {
      mapLabel.textContent = mapSlider.value + 's';
    });

    const save = document.getElementById('kiosk-cards-save');
    if (save) save.addEventListener('click', async () => {
      const boxes = $$('[name="kiosk_card"]:checked');
      const cards = boxes.map(b => b.value);
      const showMap = (document.getElementById('kiosk-show-map') || {}).checked !== false;
      const interval = parseInt((document.getElementById('kiosk-interval') || {}).value) || 8;
      const mapInterval = parseInt((document.getElementById('kiosk-map-interval') || {}).value) || 15;
      save.disabled = true;
      try {
        const r = await window.api.post('/api/settings/kiosk-cards', {
          cards, show_map: showMap, interval, map_interval: mapInterval,
        });
        if (r && r.ok === false) {
          toast(r.error || 'Save failed', true);
        } else {
          toast('Kiosk display saved');
        }
      } catch (e) {
        toast('Save failed', true);
      } finally {
        save.disabled = false;
      }
    });
  }

  // ------------------------------------------------------------------
  // Location section (inline save + geocode)
  // ------------------------------------------------------------------
  function bindLocationExtras() {
    // Source toggle
    $$('[name="location_source"]').forEach(r => {
      r.addEventListener('change', function () {
        const manual = this.value === 'manual';
        const mg = document.getElementById('loc-manual-group');
        const cg = document.getElementById('loc-coords-group');
        const gs = document.getElementById('loc-gps-status');
        if (mg) mg.hidden = !manual;
        if (cg) cg.hidden = !manual;
        if (gs) gs.hidden = manual;
      });
    });
    const srcRadio = document.querySelector('[name="location_source"]:checked');
    if (srcRadio) {
      const gs = document.getElementById('loc-gps-status');
      if (gs) gs.hidden = srcRadio.value === 'manual';
    }

    // GPS status poll — red/amber/green states:
    //   green = live fix (modemmanager, gpsd)
    //   amber = static fallback (config, cached)
    //   red   = no fix at all
    async function pollGps() {
      try {
        const d = await window.api.get('/api/settings/location');
        const g = d.gps || {};
        const el = document.getElementById('loc-gps-state');
        if (el) {
          const state = g.state || 'no_fix';
          const src = (g.source || '').toLowerCase();
          el.classList.remove('gps-green', 'gps-amber', 'gps-red');
          if (state === 'fix_acquired' && (src === 'modemmanager' || src === 'gpsd')) {
            el.textContent = 'Live Fix';
            el.classList.add('gps-green');
          } else if (state === 'static' || src === 'config' || src.includes('cached')) {
            el.textContent = 'Static (' + (g.source || 'config') + ')';
            el.classList.add('gps-amber');
          } else if (state === 'no_fix') {
            el.textContent = 'No Fix';
            el.classList.add('gps-red');
          } else {
            el.textContent = state;
          }
        }
        const srcEl = document.getElementById('loc-gps-source');
        if (srcEl) srcEl.textContent = g.source || '—';
        const coords = document.getElementById('loc-gps-coords');
        if (coords && g.lat && g.lon) {
          coords.textContent = Number(g.lat).toFixed(5) + ', ' + Number(g.lon).toFixed(5);
        }
      } catch (_) {}
    }
    if (document.getElementById('loc-gps-state')) {
      pollGps();
      setInterval(pollGps, 10000);
    }

    // Geocode
    const geocodeBtn = document.getElementById('loc-geocode-btn');
    const geocodeStatus = document.getElementById('loc-geocode-status');
    if (geocodeBtn) geocodeBtn.addEventListener('click', async () => {
      const addr = (document.getElementById('loc-address') || {}).value || '';
      if (!addr.trim()) return;
      geocodeBtn.disabled = true;
      if (geocodeStatus) geocodeStatus.textContent = 'Looking up…';
      try {
        const d = await window.api.post('/api/settings/location/geocode', { address: addr });
        if (d.ok === false) {
          if (geocodeStatus) geocodeStatus.textContent = d.error || 'Failed';
          toast(d.error || 'Geocode failed', true);
        } else {
          const lat = document.getElementById('loc-lat');
          const lon = document.getElementById('loc-lon');
          if (lat) lat.value = d.lat;
          if (lon) lon.value = d.lon;
          if (geocodeStatus) geocodeStatus.textContent = d.display_name || 'Found';
          toast('Address found');
        }
      } catch (e) {
        if (geocodeStatus) geocodeStatus.textContent = 'Error';
        toast('Geocode failed', true);
      } finally {
        geocodeBtn.disabled = false;
      }
    });

    // Save location
    const locSave = document.getElementById('loc-save');
    if (locSave) locSave.addEventListener('click', async () => {
      const src = (document.querySelector('[name="location_source"]:checked') || {}).value || 'gps';
      const payload = { location_source: src };
      if (src === 'manual') {
        payload.latitude = parseFloat((document.getElementById('loc-lat') || {}).value);
        payload.longitude = parseFloat((document.getElementById('loc-lon') || {}).value);
        payload.location_address = (document.getElementById('loc-address') || {}).value || '';
      }
      locSave.disabled = true;
      try {
        const d = await window.api.post('/api/settings/location', payload);
        if (d.ok === false) {
          toast(d.error || 'Save failed', true);
        } else {
          toast('Location saved');
        }
      } catch (e) {
        toast('Save failed', true);
      } finally {
        locSave.disabled = false;
      }
    });
  }

  // ------------------------------------------------------------------
  // Speed test
  // ------------------------------------------------------------------
  async function refreshSpeedTestHistory() {
    const list = $('#speed-test-history');
    if (!list) return;
    try {
      const r = await window.api.get('/api/settings/network/speedtest/history');
      const items = (r && r.results) || [];
      if (!items.length) {
        list.innerHTML = '<li class="muted">No speed tests yet.</li>';
        return;
      }
      list.innerHTML = '';
      items.forEach(t => {
        const li = document.createElement('li');
        li.className = 'saved-wifi-item';
        const dl = t.download_mbps != null ? t.download_mbps.toFixed(1) : '—';
        const ul = t.upload_mbps != null ? t.upload_mbps.toFixed(1) : '—';
        const ping = t.ping_ms != null ? Math.round(t.ping_ms) : '—';
        const iface = t.interface || '?';
        const ts = t.ts || '';
        li.innerHTML = `
          <span class="sw-ssid">${escapeHtml(ts)}</span>
          <span class="muted small">${escapeHtml(iface)}${t.ip ? ' · ' + escapeHtml(t.ip) : ''}</span>
          <span class="muted small">↓${dl} ↑${ul} Mbps · ${ping}ms</span>`;
        list.appendChild(li);
      });
    } catch (_) {
      list.innerHTML = '<li class="muted">Could not load history.</li>';
    }
  }

  function bindSpeedTest() {
    const btn = $('#btn-speed-test');
    if (!btn) return;
    const output = $('#speed-test-output');
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      const orig = btn.textContent;
      btn.textContent = 'Testing…';
      if (output) { output.hidden = false; output.textContent = 'Running speed test…'; }
      toast('Speed test started');
      try {
        const r = await window.api.post('/api/settings/network/speedtest');
        if (r && r.ok) {
          const dl = r.download_mbps != null ? r.download_mbps.toFixed(1) : '—';
          const ul = r.upload_mbps != null ? r.upload_mbps.toFixed(1) : '—';
          const ping = r.ping_ms != null ? Math.round(r.ping_ms) : '—';
          const iface = r.interface || 'unknown';
          const line = `Download: ${dl} Mbps · Upload: ${ul} Mbps · Ping: ${ping} ms · via ${iface}`;
          if (output) output.textContent = line;
          toast(`Down: ${dl} / Up: ${ul} Mbps via ${iface}`);
          refreshSpeedTestHistory();
        } else {
          const msg = (r && r.error) || 'Speed test failed';
          if (output) output.textContent = msg;
          toast(msg, true);
        }
      } catch (e) {
        const msg = (e && e.data && e.data.error) || 'Speed test failed';
        if (output) output.textContent = msg;
        toast(msg, true);
      } finally {
        btn.textContent = orig;
        btn.disabled = false;
      }
    });

    refreshSpeedTestHistory();
  }

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------
  // ------------------------------------------------------------------
  // Copy All button for .log-pre blocks
  // ------------------------------------------------------------------
  function bindCopyButtons() {
    $$('.log-pre').forEach(pre => {
      if (pre.querySelector('.copy-all-btn')) return;
      var wrap = document.createElement('div');
      wrap.style.position = 'relative';
      pre.parentNode.insertBefore(wrap, pre);
      wrap.appendChild(pre);
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'copy-all-btn';
      btn.textContent = 'Copy All';
      btn.addEventListener('click', function () {
        var text = pre.textContent || '';
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () {
            btn.textContent = 'Copied!';
            setTimeout(function () { btn.textContent = 'Copy All'; }, 1500);
          }).catch(function () {
            fallbackCopy(text, btn);
          });
        } else {
          fallbackCopy(text, btn);
        }
      });
      wrap.appendChild(btn);
    });
  }

  function fallbackCopy(text, btn) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); btn.textContent = 'Copied!'; }
    catch (_) { btn.textContent = 'Failed'; }
    document.body.removeChild(ta);
    setTimeout(function () { btn.textContent = 'Copy All'; }, 1500);
  }

  document.addEventListener('DOMContentLoaded', () => {
    bindRail();
    bindForms();
    bindSliders();
    bindConfirmModal();
    bindNetworkExtras();
    bindFeederExtras();
    bindDataExtras();
    bindAlertExtras();
    bindUpdateExtras();
    bindDiagnosticsExtras();
    bindKioskCards();
    bindLocationExtras();
    bindSpeedTest();
    bindCopyButtons();
    refreshSystemStatus();
  });

})();
