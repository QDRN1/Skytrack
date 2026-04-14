/* SkyTrack — shared animated particle background.
 *
 * Finds `#particles-bg` in the page, injects a full-size canvas inside it,
 * and runs a light drifting-dot field with neighbor-line connections.
 * Works on top of the existing gradient already applied to .particles-bg.
 *
 * Honors:
 *   - prefers-reduced-motion   → no animation, leave the gradient alone.
 *   - data-anim="off"          on <html> or <body> → same as reduced motion
 *   - data-anim="reduced"      → half the particles, half the speed
 *   - document.visibilityState → pauses when the tab is hidden
 *   - window resize            → re-lays out the canvas (debounced)
 *
 * Theme-aware colors read CSS custom properties (--accent, --accent-warm)
 * at startup so the field automatically matches the current theme.
 */
(function () {
  'use strict';

  const host = document.getElementById('particles-bg');
  if (!host) return;

  const prefersReduce = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const anim = (document.documentElement.dataset.anim ||
                document.body.dataset.anim || 'full').toLowerCase();
  if (anim === 'off' || prefersReduce) return;

  const canvas = document.createElement('canvas');
  canvas.className = 'particles-canvas';
  host.appendChild(canvas);

  const ctx = canvas.getContext('2d');
  const dpr = Math.min(window.devicePixelRatio || 1, 2);

  const css = getComputedStyle(document.documentElement);
  const accent = (css.getPropertyValue('--accent') || '#4d8bff').trim();
  const warm   = (css.getPropertyValue('--accent-warm') || '#ffae59').trim();

  // Reduced mode: half as many dots, softer motion.
  const scale = anim === 'reduced' ? 0.5 : 1;

  let W = 0, H = 0;
  let dots = [];
  let raf = null;
  let running = false;

  function sizeCanvas() {
    W = window.innerWidth;
    H = window.innerHeight;
    canvas.width = Math.floor(W * dpr);
    canvas.height = Math.floor(H * dpr);
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);
  }

  function seedDots() {
    const base = Math.round(W / 22);
    const target = Math.min(110, Math.max(40, Math.round(base * scale)));
    dots = new Array(target);
    for (let i = 0; i < target; i++) {
      dots[i] = {
        x: Math.random() * W,
        y: Math.random() * H,
        vx: (Math.random() - 0.5) * 0.20 * scale,
        vy: (Math.random() - 0.5) * 0.20 * scale,
        r: Math.random() * 1.5 + 0.5,
        warm: Math.random() < 0.18,
      };
    }
  }

  function hexToRgb(h) {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(h);
    if (!m) return [120, 170, 255];
    return [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)];
  }
  const ACCENT_RGB = hexToRgb(accent);
  const WARM_RGB   = hexToRgb(warm);
  const LINK_DIST  = 140;

  function frame() {
    if (!running) return;
    ctx.clearRect(0, 0, W, H);

    // connecting lines first (they sit behind the dots)
    for (let i = 0; i < dots.length; i++) {
      for (let j = i + 1; j < dots.length; j++) {
        const a = dots[i], b = dots[j];
        const dx = a.x - b.x, dy = a.y - b.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < LINK_DIST * LINK_DIST) {
          const t = 1 - Math.sqrt(d2) / LINK_DIST;
          const alpha = 0.18 * t;
          ctx.strokeStyle = `rgba(${ACCENT_RGB[0]},${ACCENT_RGB[1]},${ACCENT_RGB[2]},${alpha.toFixed(3)})`;
          ctx.lineWidth = 0.6;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
    }

    // dots
    for (let i = 0; i < dots.length; i++) {
      const d = dots[i];
      d.x += d.vx;
      d.y += d.vy;
      if (d.x < -8) d.x = W + 8;
      if (d.x > W + 8) d.x = -8;
      if (d.y < -8) d.y = H + 8;
      if (d.y > H + 8) d.y = -8;
      const rgb = d.warm ? WARM_RGB : ACCENT_RGB;
      ctx.beginPath();
      ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0.80)`;
      ctx.fill();
    }

    raf = requestAnimationFrame(frame);
  }

  function start() {
    if (running) return;
    running = true;
    raf = requestAnimationFrame(frame);
  }
  function stop() {
    running = false;
    if (raf) cancelAnimationFrame(raf);
    raf = null;
  }

  let resizeTimer = null;
  function onResize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      sizeCanvas();
      seedDots();
    }, 120);
  }

  function onVisibility() {
    if (document.visibilityState === 'hidden') stop();
    else start();
  }

  sizeCanvas();
  seedDots();
  start();
  window.addEventListener('resize', onResize, { passive: true });
  document.addEventListener('visibilitychange', onVisibility);
})();
