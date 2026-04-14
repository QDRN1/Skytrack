/* Theme toggle — persisted in localStorage. */
(function () {
  const root = document.documentElement;
  const saved = localStorage.getItem('skytrack.theme');
  if (saved) root.setAttribute('data-theme', saved);

  document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;
    btn.addEventListener('click', () => {
      const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      localStorage.setItem('skytrack.theme', next);
    });
  });
})();
