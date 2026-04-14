/* Fallback full-page login form.
 *
 * The preferred sign-in path is the in-page modal in base.html, which
 * is opened automatically by static/js/auth.js whenever a protected
 * route returns 401. This page is the no-JS / direct-URL fallback.
 *
 * Either the admin password OR the optional PIN authenticates the
 * admin role; the server accepts both fields and tries each.
 */
(function () {
  document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('login-form');
    const err = document.getElementById('login-error');
    if (!form) return;

    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      err.hidden = true;

      const data = new FormData(form);
      const password = data.get('password') || '';
      const pin = (data.get('pin') || '').trim();
      const next = data.get('next') || '/dashboard';
      if (!password && !pin) {
        err.textContent = 'Enter your admin password or PIN.';
        err.hidden = false;
        return;
      }

      try {
        const result = await window.api.post('/api/auth/login', {
          password, pin, next,
        });
        if (result && result.ok) {
          window.location.href = result.next || next;
          return;
        }
        err.textContent = (result && result.error) || 'Sign-in failed';
        err.hidden = false;
      } catch (e) {
        err.textContent = (e.data && e.data.error) || e.message || 'Sign-in failed';
        err.hidden = false;
      }
    });
  });
})();
