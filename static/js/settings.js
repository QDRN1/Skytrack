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
      try {
        const u = new URL(window.location.href);
        u.hash = name;
        history.replaceState(null, '', u.toString());
      } catch (_) {}
      // Lazy refresh hooks: kick off live data when a section becomes visible.
      if (name === 'network')      refreshNetworkStatus();
      if (name === 'feeders')      refreshFeederStatus();
      if (name === 'diagnostics')  refreshDiagnostics();
      if (name === 'data')         refreshDataUsage();
      if (name === 'updates')      refreshBackups();
      if (name === 'alerts')       refreshWatchlist();
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
  // Confirm modal
  // ------------------------------------------------------------------
  let _confirmResolve = null;
  function confirmModal(title, body) {
    const modal = $('#confirm-modal');
    if (!modal) return Promise.resolve(window.confirm(`${title}\n\n${body || ''}`));
    $('#confirm-title', modal).textContent = title || 'Are you sure?';
    $('#confirm-body',  modal).textContent = body  || '';
    modal.hidden = false;
    return new Promise(resolve => { _confirmResolve = resolve; });
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

    const regen = $('#btn-hot-regen');
    if (regen) regen.addEventListener('click', () => withConfirm(
      Object.assign(regen, { dataset: Object.assign(regen.dataset, {
        confirmTitle: regen.dataset.confirmTitle || 'Regenerate hotspot password?',
        confirmBody:  regen.dataset.confirmBody  || 'Anyone currently connected will be kicked off.'
      })}),
      async () => {
        try {
          const r = await window.api.post('/api/settings/network/hotspot/regenerate');
          toast('Hotspot password regenerated');
          if (r.password) { const c = $('#net-hot-pw'); if (c) c.textContent = r.password; }
        } catch (e) { toast('Failed to regenerate', true); }
      }
    ));

    const restartHs = $('#btn-hot-restart');
    if (restartHs) restartHs.addEventListener('click', () => withConfirm(
      Object.assign(restartHs, { dataset: Object.assign(restartHs.dataset, {
        confirmTitle: 'Restart hotspot?',
        confirmBody:  'You may lose this session for ~10 seconds if you are connected over the hotspot.'
      })}),
      async () => {
        try { await window.api.post('/api/settings/network/hotspot/restart'); toast('Hotspot restarting'); }
        catch (e) { toast('Restart failed', true); }
      }
    ));

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
      const r = await window.api.get('/api/settings/feeders/status');
      const setTile = (id, st) => {
        const t = $('#feed-' + id + '-tile');
        const v = $('#feed-' + id + '-value');
        const s = $('#feed-' + id + '-sub');
        const state = (st && st.state) || 'off';
        const label = {
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
    } catch (_) {}
  }

  function bindFeederExtras() {
    const r = $('#btn-restart-dump1090');
    if (r) r.addEventListener('click', async () => {
      const ok = await confirmModal('Restart dump1090?', 'The receiver will drop its current aircraft list briefly.');
      if (!ok) return;
      try { await window.api.post('/api/settings/feeders/dump1090/restart'); toast('dump1090 restarting'); refreshFeederStatus(); }
      catch (_) { toast('Restart failed', true); }
    });
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

  function bindUpdateExtras() {
    const out = $('#update-output');
    const setOut = t => { if (out) out.textContent = t; };

    const check = $('#btn-update-check');
    if (check) check.addEventListener('click', async () => {
      setOut('Checking…');
      try { const r = await window.api.post('/api/settings/updates/check'); setOut(r.message || (r.ok ? 'Up to date' : 'Failed')); const lt = $('#upd-latest'); if (lt && r.latest) lt.textContent = r.latest; }
      catch (_) { setOut('Check failed'); }
    });

    const apply = $('#btn-update-apply');
    if (apply) apply.addEventListener('click', async () => {
      const ok = await confirmModal(apply.dataset.confirmTitle || 'Apply updates?', apply.dataset.confirmBody || '');
      if (!ok) return;
      setOut('Applying…');
      try { const r = await window.api.post('/api/settings/updates/apply'); setOut(r.message || 'Done'); }
      catch (_) { setOut('Apply failed'); }
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

    // Power buttons
    const power = (id, url, msg) => {
      const b = $('#' + id);
      if (!b) return;
      b.addEventListener('click', async () => {
        const ok = await confirmModal(b.dataset.confirmTitle, b.dataset.confirmBody);
        if (!ok) return;
        try { await window.api.post(url); toast(msg); }
        catch (_) { toast('Failed', true); }
      });
    };
    power('btn-restart-app',     '/api/settings/updates/restart-app',     'App restarting');
    power('btn-restart-network', '/api/settings/updates/restart-network', 'Network restarting');
    power('btn-reboot',          '/api/settings/updates/reboot',          'Rebooting');
    power('btn-shutdown',        '/api/settings/updates/shutdown',        'Shutting down');
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
  // Boot
  // ------------------------------------------------------------------
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
  });

})();
