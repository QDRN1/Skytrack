/* SkyTrack auth modal + 401 interceptor.
 *
 * This script is what makes "click Settings → password modal → keep going"
 * feel polished. It does five things:
 *
 *   1. Tracks the current admin session via /api/auth/status and exposes
 *      window.auth.status() / window.auth.isAdmin() helpers.
 *   2. Wraps every link/button marked [data-auth-trigger="admin"] so that
 *      a click on a protected affordance opens the modal first when the
 *      user isn't signed in. After successful login the original
 *      navigation/callback is replayed.
 *   3. Wraps fetch() so that any 401 from a /api/* endpoint pops the
 *      modal and replays the request once the user signs in.
 *   4. Drives the modal markup that lives in base.html (open/close,
 *      submit handler, error display).
 *   5. Shows a one-off "Logged in as Admin" toast on success and
 *      promotes the topbar "Sign in" → "Sign out".
 *
 * The dashboard never hits this code in its happy path — it stays
 * fully public.
 */
(function () {
  'use strict';

  const STATUS_URL = '/api/auth/status';
  const LOGIN_URL  = '/api/auth/login';
  const LOGOUT_URL = '/logout';

  // ------------------------------------------------------------------
  // State
  // ------------------------------------------------------------------
  const state = {
    status: null,          // last /api/auth/status response
    pending: null,         // { resolve, reject, retry } when modal is open
    modal: null,
    form: null,
    pinInput: null,
    errorEl: null,
    hintEl: null,
  };

  // ------------------------------------------------------------------
  // DOM helpers
  // ------------------------------------------------------------------
  function $(id) { return document.getElementById(id); }

  function bindModal() {
    state.modal   = $('auth-modal');
    state.form    = $('auth-modal-form');
    state.pinInput = $('auth-modal-pin');
    state.errorEl = $('auth-modal-error');
    state.hintEl  = $('auth-modal-hint');
    if (!state.modal) return;

    state.modal.querySelectorAll('[data-auth-close]').forEach((el) => {
      el.addEventListener('click', () => closeModal('cancelled'));
    });
    state.form.addEventListener('submit', onModalSubmit);
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && !state.modal.hidden) closeModal('cancelled');
    });
  }

  function showError(msg) {
    if (!state.errorEl) return;
    state.errorEl.textContent = msg || '';
    state.errorEl.hidden = !msg;
  }

  function openModal(opts) {
    opts = opts || {};
    if (!state.modal) return;
    showError('');
    if (state.pinInput) state.pinInput.value = '';
    if (state.hintEl && opts.hint) state.hintEl.textContent = opts.hint;
    state.modal.hidden = false;
    document.body.classList.add('modal-open');
    setTimeout(() => state.pinInput && state.pinInput.focus(), 50);
    state.pending = state.pending || { resolve: null, reject: null, retry: null };
    if (opts.next) state.pending.next = opts.next;
  }

  function closeModal(reason) {
    if (!state.modal) return;
    state.modal.hidden = true;
    document.body.classList.remove('modal-open');
    if (state.pending && reason === 'cancelled' && state.pending.reject) {
      state.pending.reject(new Error('auth cancelled'));
    }
    if (reason === 'cancelled') {
      state.pending = null;
    }
  }

  // ------------------------------------------------------------------
  // Toast
  // ------------------------------------------------------------------
  function toast(message, kind) {
    const stack = $('toast-stack');
    if (!stack) { console.log('[toast]', message); return; }
    const el = document.createElement('div');
    el.className = 'toast toast-' + (kind || 'info');
    el.textContent = message;
    stack.appendChild(el);
    setTimeout(() => el.classList.add('toast-show'), 10);
    setTimeout(() => {
      el.classList.remove('toast-show');
      setTimeout(() => el.remove(), 300);
    }, 3500);
  }

  // ------------------------------------------------------------------
  // Status polling (cheap — just for the topbar)
  // ------------------------------------------------------------------
  async function refreshStatus() {
    try {
      const r = await fetch(STATUS_URL, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      if (r.ok) {
        state.status = await r.json();
        applyStatusToTopbar();
      }
    } catch (_e) { /* offline */ }
    return state.status;
  }

  function applyStatusToTopbar() {
    const role = (state.status && state.status.role) || '';
    document.body.dataset.role = role;
    // Note: full topbar repaint is left to a hard reload after sign-in,
    // because the topbar markup is server-rendered and may differ across
    // pages. For client-side we just toggle the visible signin/signout
    // buttons that exist in the current DOM.
    const signin  = $('topbar-signin');
    const signout = $('topbar-signout');
    const roleBadge = $('topbar-role');
    if (state.status && state.status.is_admin) {
      if (signin)  signin.hidden = true;
      if (signout) signout.hidden = false;
      if (roleBadge) roleBadge.hidden = false;
    } else {
      if (signin)  signin.hidden = false;
      if (signout) signout.hidden = true;
      if (roleBadge) roleBadge.hidden = true;
    }
  }

  // ------------------------------------------------------------------
  // Modal submit
  // ------------------------------------------------------------------
  async function onModalSubmit(ev) {
    ev.preventDefault();
    showError('');
    const pin = ((state.pinInput && state.pinInput.value) || '').trim();
    if (!/^[0-9]{4,8}$/.test(pin)) {
      showError('Enter your 4–8 digit admin PIN');
      return;
    }
    try {
      const r = await fetch(LOGIN_URL, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
        },
        body: JSON.stringify({ pin }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.ok) {
        showError((data && data.error) || 'Sign-in failed');
        return;
      }
      // Success
      await refreshStatus();
      toast('Logged in as Admin', 'success');
      const pending = state.pending;
      state.pending = null;
      closeModal('success');

      if (pending && pending.retry) {
        try { pending.retry(); } catch (_e) { /* ignore */ }
        if (pending.resolve) pending.resolve(data);
      } else if (pending && pending.next) {
        window.location.href = pending.next;
      } else if (data.next) {
        // Default after-login behavior: stay on the current page if we
        // got here from a click handler; otherwise follow the server
        // suggested next.
        // We only navigate if we have no pending click target.
      }
    } catch (e) {
      showError(e.message || 'Network error');
    }
  }

  // ------------------------------------------------------------------
  // requireAdmin — public API used by inline click handlers
  // ------------------------------------------------------------------
  function requireAdmin(opts) {
    opts = opts || {};
    return new Promise((resolve, reject) => {
      const proceed = () => resolve();
      if (state.status && state.status.is_admin) {
        proceed();
        return;
      }
      // Need to auth first
      state.pending = {
        resolve,
        reject,
        retry: opts.retry || proceed,
        next: opts.next || null,
      };
      openModal({ hint: opts.hint });
    });
  }

  // ------------------------------------------------------------------
  // Wire data-auth-trigger="admin" affordances
  // ------------------------------------------------------------------
  function bindTriggers() {
    document.querySelectorAll('[data-auth-trigger="admin"]').forEach((el) => {
      // <a href="..."> — open modal first, then navigate
      if (el.tagName === 'A' && el.href) {
        const href = el.href;
        el.addEventListener('click', (ev) => {
          if (state.status && state.status.is_admin) return; // already in
          ev.preventDefault();
          requireAdmin({ next: href, retry: () => { window.location.href = href; } })
            .catch(() => {});
        });
        return;
      }
      // <button> — just open the modal; a separate handler can resume work
      el.addEventListener('click', (ev) => {
        ev.preventDefault();
        const next = el.dataset.authNext || null;
        requireAdmin({
          next,
          retry: next ? () => { window.location.href = next; } : null,
        }).catch(() => {});
      });
    });
  }

  // Also intercept clicks on protected nav links (Settings/Network/Logs)
  function bindProtectedNav() {
    document.querySelectorAll('a.topbar-link[data-protected="admin"]').forEach((a) => {
      const href = a.href;
      a.addEventListener('click', (ev) => {
        if (state.status && state.status.is_admin) return;
        ev.preventDefault();
        requireAdmin({ next: href, retry: () => { window.location.href = href; } })
          .catch(() => {});
      });
    });
  }

  // ------------------------------------------------------------------
  // fetch() interceptor — replay 401s after the user signs in
  // ------------------------------------------------------------------
  function installFetchInterceptor() {
    const orig = window.fetch;
    if (!orig || orig._skytrackWrapped) return;
    const wrapped = async function (input, init) {
      const resp = await orig(input, init);
      if (resp.status !== 401) return resp;
      // Only intercept JSON 401s — page navigations get the auth_gate.html
      const ct = resp.headers.get('content-type') || '';
      if (ct.indexOf('application/json') < 0) return resp;
      // Don't loop on the login/status endpoints themselves
      const url = (typeof input === 'string') ? input : (input && input.url) || '';
      if (url.indexOf('/api/auth/') >= 0) return resp;
      try {
        await requireAdmin({
          retry: null,
          hint: 'This action needs admin sign-in.',
        });
      } catch (_e) {
        return resp;
      }
      // Retry once
      return orig(input, init);
    };
    wrapped._skytrackWrapped = true;
    window.fetch = wrapped;
  }

  // ------------------------------------------------------------------
  // Public surface
  // ------------------------------------------------------------------
  window.auth = {
    status: () => state.status,
    refreshStatus,
    isAdmin: () => !!(state.status && state.status.is_admin),
    requireAdmin,
    openModal,
    closeModal,
    toast,
  };

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', () => {
    bindModal();
    installFetchInterceptor();
    refreshStatus().then(() => {
      bindTriggers();
      bindProtectedNav();
    });
  });
})();
