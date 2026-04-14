/* Portal theme toggle.
 *
 * This controls ONLY the admin portal (/dashboard, /settings, /network, /logs).
 * The kiosk display (/kiosk) uses its own template and is always dark — the
 * appliance ships dark-themed so HDMI screens don't blast white pixels at
 * night. Admins can still toggle their own portal here.
 *
 * Three modes, cycled by the button:
 *   dark  — forced dark
 *   light — forced light
 *   auto  — follow the OS / browser `prefers-color-scheme`
 *
 * Choice is persisted in localStorage. On first load we honor the server's
 * config.default_theme (rendered into <html data-theme="…">) if no local
 * override has been set yet. Admins who want the default to apply to every
 * new browser can change it in Settings → General.
 */
(function () {
  'use strict';

  var root = document.documentElement;
  var STORAGE_KEY = 'skytrack.theme';
  var MODES = ['dark', 'light', 'auto'];
  var LABELS = { dark: 'Dark', light: 'Light', auto: 'Auto' };
  var ICONS  = { dark: '◐', light: '☀', auto: '◑' };

  // ---- Initial resolve -----------------------------------------------------
  // The server already rendered data-theme from config.default_theme. Only
  // override it if the operator has a localStorage preference.
  var saved = null;
  try { saved = localStorage.getItem(STORAGE_KEY); } catch (_) { /* private mode */ }
  if (saved && MODES.indexOf(saved) >= 0) {
    applyMode(saved);
  } else {
    // No local preference — keep whatever the server said, but still
    // expand "auto" into dark/light for the CSS variables.
    applyMode(root.getAttribute('data-theme') || 'dark');
  }

  // Keep "auto" mode responsive to OS theme changes.
  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    var handler = function () {
      if (getMode() === 'auto') applyMode('auto');
    };
    if (mq.addEventListener) mq.addEventListener('change', handler);
    else if (mq.addListener) mq.addListener(handler);
  }

  // ---- Wire the toggle -----------------------------------------------------
  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    refreshButton(btn);
    btn.addEventListener('click', function () {
      var cur = getMode();
      var next = MODES[(MODES.indexOf(cur) + 1) % MODES.length];
      setMode(next);
      refreshButton(btn);
    });
  });

  // ---- Internals -----------------------------------------------------------
  function getMode() {
    var m = root.getAttribute('data-theme-mode');
    return (m && MODES.indexOf(m) >= 0) ? m : 'dark';
  }

  function setMode(mode) {
    try { localStorage.setItem(STORAGE_KEY, mode); } catch (_) {}
    applyMode(mode);
  }

  function applyMode(mode) {
    if (MODES.indexOf(mode) < 0) mode = 'dark';
    root.setAttribute('data-theme-mode', mode);
    var effective = mode;
    if (mode === 'auto') {
      var prefersDark = window.matchMedia &&
        window.matchMedia('(prefers-color-scheme: dark)').matches;
      effective = prefersDark ? 'dark' : 'light';
    }
    root.setAttribute('data-theme', effective);
  }

  function refreshButton(btn) {
    var mode = getMode();
    btn.textContent = ICONS[mode] || '◐';
    btn.setAttribute(
      'title',
      'Portal theme: ' + LABELS[mode] + ' (click to change — portal only, not kiosk)'
    );
    btn.setAttribute('aria-label', 'Portal theme: ' + LABELS[mode]);
  }
})();
