/* Login: PIN keypad + admin password fallback. */
(function () {
  document.addEventListener('DOMContentLoaded', () => {
    let pin = '';
    const dots = document.querySelectorAll('#pin-display span');
    const err = document.getElementById('login-error');

    const renderDots = () => {
      dots.forEach((dot, i) => dot.classList.toggle('filled', i < pin.length));
    };

    const submitPin = async () => {
      if (pin.length < 4) {
        err.textContent = 'PIN must be at least 4 digits';
        err.hidden = false;
        return;
      }
      try {
        const result = await window.api.post('/login', { role: 'pin', pin });
        if (result.ok) {
          window.location.href = result.next || '/dashboard';
        }
      } catch (e) {
        err.textContent = e.data && e.data.error || 'Login failed';
        err.hidden = false;
        pin = '';
        renderDots();
      }
    };

    document.querySelectorAll('.key').forEach((btn) => {
      btn.addEventListener('click', () => {
        const k = btn.dataset.key;
        err.hidden = true;
        if (k === 'clear') { pin = ''; renderDots(); return; }
        if (k === 'enter') { submitPin(); return; }
        if (pin.length < 8) { pin += k; renderDots(); }
      });
    });

    document.addEventListener('keydown', (e) => {
      if (/^[0-9]$/.test(e.key) && pin.length < 8) { pin += e.key; renderDots(); }
      else if (e.key === 'Backspace') { pin = pin.slice(0, -1); renderDots(); }
      else if (e.key === 'Enter') { submitPin(); }
    });

    const adminForm = document.getElementById('admin-form');
    if (adminForm) {
      adminForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const password = new FormData(adminForm).get('password') || '';
        try {
          const result = await window.api.post('/login', { role: 'admin', password });
          if (result.ok) window.location.href = result.next || '/dashboard';
        } catch (err2) {
          alert((err2.data && err2.data.error) || 'Admin login failed');
        }
      });
    }
  });
})();
