/* Fallback full-page login form.
 *
 * The preferred sign-in path is the in-page modal in base.html, which
 * is opened automatically by static/js/auth.js whenever a protected
 * route returns 401. This page is the no-JS / direct-URL fallback.
 *
 * Admin is PIN-only — 4-8 digit numeric PIN is the single credential.
 */
(function () {
  document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('login-form');
    const err = document.getElementById('login-error');
    if (!form) return;

    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      if (err) err.hidden = true;

      const data = new FormData(form);
      const pin = (data.get('pin') || '').trim();
      const next = data.get('next') || '/dashboard';

      if (!/^[0-9]{4,8}$/.test(pin)) {
        if (err) { err.textContent = 'Enter your 4–8 digit admin PIN.'; err.hidden = false; }
        return;
      }

      try {
        const result = await window.api.post('/api/auth/login', { pin, next });
        if (result && result.ok) {
          window.location.href = result.next || next;
          return;
        }
        if (err) { err.textContent = (result && result.error) || 'Sign-in failed'; err.hidden = false; }
      } catch (e) {
        if (err) { err.textContent = (e.data && e.data.error) || e.message || 'Sign-in failed'; err.hidden = false; }
      }
    });
  });
})();
