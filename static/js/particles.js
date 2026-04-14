/* SkyTrack — shared animated particle background.
 *
 * qdrn.io-flavored drifting particle field. Three design goals:
 *
 *   1. Visible. Each particle is a soft glow disk, not a 1px pixel. On a
 *      dark surface it reads as a "starfield with halos" rather than
 *      dust. On a light surface it fades to a subtle ambient sparkle.
 *
 *   2. Branded, not overwhelming. Most particles pick up the cool accent
 *      (--accent, SkyTrack blue); a small minority use the warm accent
 *      (--accent-warm) for pop. No jagged connecting-line mesh — the old
 *      version drew O(n²) lines every frame which both looked busy and
 *      tanked CPU on a Pi.
 *
 *   3. Performant on a Pi 4 at 1080p. Caps the device-pixel ratio at
 *      1.25, caps the particle count from config + screen area, and
 *      stops rendering entirely when the tab is hidden or the user has
 *      `prefers-reduced-motion`.
 *
 * Controls surfaced via <html> data-* attributes (rendered from config):
 *
 *   data-anim="full"|"reduced"|"off"    — animation level
 *                                          off           = gradient only
 *                                          reduced       = ~40% density, slower
 *                                          full (default)= baseline
 *
 *   data-particle-density="0..100"      — operator-tunable density slider
 *                                          (Settings → Display → Particle density)
 *                                          0 disables the field entirely.
 */
(function () {
  'use strict';

  const host = document.getElementById('particles-bg');
  if (!host) return;

  // ---- Honor system + config preferences ----------------------------------
  const prefersReduce = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const htmlDs = document.documentElement.dataset;
  const bodyDs = document.body ? document.body.dataset : {};
  const anim   = (htmlDs.anim || bodyDs.anim || 'full').toLowerCase();
  if (anim === 'off' || prefersReduce) return;

  const densityRaw = parseInt(htmlDs.particleDensity || bodyDs.particleDensity || '80', 10);
  const density01  = Math.max(0, Math.min(1, (isFinite(densityRaw) ? densityRaw : 80) / 100));
  if (density01 <= 0.01) return;

  // ---- Canvas setup -------------------------------------------------------
  const canvas = document.createElement('canvas');
  canvas.className = 'particles-canvas';
  host.appendChild(canvas);

  const ctx = canvas.getContext('2d', { alpha: true });
  // Cap DPR at 1.25 — full DPR on a Pi 1080p screen (DPR 1) is fine, but
  // on a Retina laptop DPR 2 doubles the fill-rate cost for no visual gain
  // at these particle sizes.
  const dpr = Math.min(window.devicePixelRatio || 1, 1.25);

  const css    = getComputedStyle(document.documentElement);
  const accent = (css.getPropertyValue('--accent')      || '#4d8bff').trim();
  const warm   = (css.getPropertyValue('--accent-warm') || '#ffae59').trim();
  const ACCENT_RGB = hexToRgb(accent);
  const WARM_RGB   = hexToRgb(warm);

  // "Reduced" anim → softer speed & count on top of the density slider.
  const reducedFactor = (anim === 'reduced') ? 0.45 : 1;

  let W = 0, H = 0;
  let dots = [];
  let raf = null;
  let running = false;

  function hexToRgb(h) {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(h || '');
    if (!m) return [77, 139, 255];
    return [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)];
  }

  function sizeCanvas() {
    W = window.innerWidth;
    H = window.innerHeight;
    canvas.width  = Math.floor(W * dpr);
    canvas.height = Math.floor(H * dpr);
    canvas.style.width  = W + 'px';
    canvas.style.height = H + 'px';
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);
  }

  function seedDots() {
    // Density is driven by screen area, density slider, and anim level.
    // Cap at 90 so a 4K monitor doesn't accidentally spawn hundreds of
    // particles. Floor at 18 so the canvas never looks empty when enabled.
    const areaBase = (W * H) / 18000;                  // ~80 on 1920x1080
    const target   = Math.max(18, Math.min(
      90,
      Math.round(areaBase * density01 * reducedFactor),
    ));

    const out = new Array(target);
    for (let i = 0; i < target; i++) {
      const isWarm = Math.random() < 0.15;
      out[i] = {
        x:  Math.random() * W,
        y:  Math.random() * H,
        // Drift speed — slow enough to feel meditative, not busy.
        vx: (Math.random() - 0.5) * 0.14 * reducedFactor,
        vy: (Math.random() - 0.5) * 0.14 * reducedFactor,
        // Core radius in CSS px. Each draw paints a much larger halo.
        r:  1.0 + Math.random() * 2.2,
        // Phase for gentle twinkle. Keeps particles from looking static
        // even when their drift is slow.
        phase: Math.random() * Math.PI * 2,
        speed: 0.012 + Math.random() * 0.020,
        warm: isWarm,
      };
    }
    dots = out;
  }

  function frame() {
    if (!running) return;
    // Clear with alpha 0 — we sit over the CSS gradient on .particles-bg,
    // so the container provides the base color/gradient and we only
    // paint the sparkle layer.
    ctx.clearRect(0, 0, W, H);

    ctx.globalCompositeOperation = 'lighter';

    for (let i = 0; i < dots.length; i++) {
      const d = dots[i];
      d.x += d.vx;
      d.y += d.vy;
      d.phase += d.speed;

      // Wrap around edges so particles stream continuously.
      if (d.x < -20)    d.x = W + 20;
      if (d.x > W + 20) d.x = -20;
      if (d.y < -20)    d.y = H + 20;
      if (d.y > H + 20) d.y = -20;

      const twinkle = 0.65 + 0.35 * Math.sin(d.phase);
      const rgb = d.warm ? WARM_RGB : ACCENT_RGB;
      const haloR = d.r * 6.5;

      // Soft radial halo — this is what gives the qdrn.io glow look.
      const grad = ctx.createRadialGradient(d.x, d.y, 0, d.x, d.y, haloR);
      grad.addColorStop(0.00, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${(0.55 * twinkle).toFixed(3)})`);
      grad.addColorStop(0.35, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${(0.16 * twinkle).toFixed(3)})`);
      grad.addColorStop(1.00, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0)`);
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(d.x, d.y, haloR, 0, Math.PI * 2);
      ctx.fill();

      // Tight bright core on top for definition.
      ctx.beginPath();
      ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${(0.85 * twinkle).toFixed(3)})`;
      ctx.fill();
    }

    ctx.globalCompositeOperation = 'source-over';

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

  // ---- Lifecycle hooks ----------------------------------------------------
  let resizeTimer = null;
  function onResize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      sizeCanvas();
      seedDots();
    }, 150);
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
