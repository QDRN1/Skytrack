/* SkyTrack in-app modal dialogs (alert / confirm / prompt).
 *
 * Native window.alert / confirm / prompt are banned on kiosk hardware
 * because:
 *   1. The touch keyboard doesn't always come up reliably for a native
 *      prompt — operators end up staring at an invisible dialog.
 *   2. Chromium-kiosk sometimes renders them off-screen in portrait.
 *   3. They carry no SkyTrack branding and no role-gating hooks.
 *
 * This module installs a single modal root element lazily on first use
 * and exposes `window.uiModal.alert(msg)`, `window.uiModal.confirm(msg)`,
 * and `window.uiModal.prompt(msg, defaultValue)`, each of which returns
 * a Promise. The promise resolves with:
 *     alert()   → true when dismissed
 *     confirm() → true on OK, false on Cancel / Escape
 *     prompt()  → string on OK, null on Cancel / Escape
 *
 * Calling code should therefore look like:
 *     if (!(await uiModal.confirm('Restart hotspot?'))) return;
 *
 * The module also exports a `uiModal.install()` shim that replaces the
 * native functions with Promise-returning versions, so legacy code that
 * uses `if (confirm(...))` without await continues to work for display
 * but is flagged with a one-shot console warning.
 */
(function () {
  const NS = 'ui-modal';
  let root = null;

  function mount() {
    if (root) return root;
    root = document.createElement('div');
    root.className = NS + '-root';
    root.setAttribute('aria-live', 'polite');
    root.hidden = true;
    root.innerHTML = `
      <div class="${NS}-backdrop" data-dismiss></div>
      <div class="${NS}-card" role="dialog" aria-modal="true"
           aria-labelledby="${NS}-title">
        <header class="${NS}-head">
          <h2 id="${NS}-title" class="${NS}-title">SkyTrack</h2>
        </header>
        <p class="${NS}-body"></p>
        <div class="${NS}-input-wrap" hidden>
          <input type="text" class="${NS}-input" autocomplete="off">
        </div>
        <div class="${NS}-actions">
          <button type="button" class="btn" data-role="cancel">Cancel</button>
          <button type="button" class="btn primary" data-role="ok">OK</button>
        </div>
      </div>
    `;
    document.body.appendChild(root);
    return root;
  }

  function show({ kind, title, body, defaultValue }) {
    mount();
    const titleEl = root.querySelector(`.${NS}-title`);
    const bodyEl  = root.querySelector(`.${NS}-body`);
    const inputWrap = root.querySelector(`.${NS}-input-wrap`);
    const input  = root.querySelector(`.${NS}-input`);
    const cancelBtn = root.querySelector('[data-role="cancel"]');
    const okBtn = root.querySelector('[data-role="ok"]');

    titleEl.textContent = title || 'SkyTrack';
    bodyEl.textContent  = body || '';

    const wantsInput = (kind === 'prompt');
    inputWrap.hidden  = !wantsInput;
    if (wantsInput) {
      input.value = defaultValue == null ? '' : String(defaultValue);
    }

    const wantsCancel = (kind !== 'alert');
    cancelBtn.hidden = !wantsCancel;
    okBtn.textContent = (kind === 'alert') ? 'OK' : 'Confirm';

    root.hidden = false;
    // Delay focus so the browser has a frame to render the overlay first.
    setTimeout(() => (wantsInput ? input : okBtn).focus(), 30);

    return new Promise((resolve) => {
      function cleanup(result) {
        root.hidden = true;
        okBtn.removeEventListener('click', onOk);
        cancelBtn.removeEventListener('click', onCancel);
        root.removeEventListener('click', onBackdrop);
        document.removeEventListener('keydown', onKey);
        input.removeEventListener('keydown', onInputKey);
        resolve(result);
      }
      function okResult() {
        if (kind === 'prompt') return input.value;
        return true;
      }
      function cancelResult() {
        if (kind === 'prompt') return null;
        if (kind === 'alert')  return true;
        return false;
      }
      function onOk() { cleanup(okResult()); }
      function onCancel() { cleanup(cancelResult()); }
      function onBackdrop(ev) {
        if (ev.target && ev.target.matches('[data-dismiss]')) onCancel();
      }
      function onKey(ev) {
        if (ev.key === 'Escape') { ev.preventDefault(); onCancel(); }
        else if (ev.key === 'Enter' && !wantsInput) { ev.preventDefault(); onOk(); }
      }
      function onInputKey(ev) {
        if (ev.key === 'Enter') { ev.preventDefault(); onOk(); }
      }
      okBtn.addEventListener('click', onOk);
      cancelBtn.addEventListener('click', onCancel);
      root.addEventListener('click', onBackdrop);
      document.addEventListener('keydown', onKey);
      if (wantsInput) input.addEventListener('keydown', onInputKey);
    });
  }

  const api = {
    alert(body, title) {
      return show({ kind: 'alert', title: title || 'Notice', body: body || '' });
    },
    confirm(body, title) {
      return show({ kind: 'confirm', title: title || 'Are you sure?', body: body || '' });
    },
    prompt(body, defaultValue, title) {
      return show({ kind: 'prompt', title: title || 'Input needed',
                    body: body || '', defaultValue: defaultValue || '' });
    },
    toast(msg, kind) {
      // Thin wrapper so legacy "alert-as-toast" calls can be migrated to
      // the existing toast stack without importing auth.js directly.
      if (window.auth && window.auth.toast) {
        window.auth.toast(msg, kind || 'info');
      } else {
        api.alert(msg);
      }
    },
  };

  // Expose on window. Don't override native functions — code that wants
  // the modal must call uiModal.* explicitly, which makes the migration
  // explicit in code review instead of silent.
  window.uiModal = api;
})();
