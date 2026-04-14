/* SkyTrack on-device kiosk display.
 *
 * This script drives /kiosk — the fullscreen Chromium target running on
 * the Pi's HDMI output. It is intentionally separate from every other
 * page in the portal: no topbar, no auth UI, no fetch interceptor. It
 * only needs to do four things:
 *
 *   1. While the device is NOT configured, show the looping setup video
 *      with the device ID pill at the top.
 *   2. If the setup video fails to load, fall back to a canvas-particles
 *      background with centered "SETUP REQUIRED / WiFi / URL / Device ID"
 *      text.
 *   3. The moment /api/auth/status flips to configured=true, stop the
 *      setup video, play the boot video once, then redirect to /dashboard.
 *   4. If the kiosk reloads after the device is already configured (e.g.
 *      after a power cycle), skip straight to the boot video.
 *
 * It deliberately polls /api/auth/status on a short interval rather than
 * holding open a Socket.IO connection — the kiosk runs locally and a
 * cheap fetch every 1.5s is plenty.
 */
(function () {
  'use strict';

  const STATUS_URL = '/api/auth/status';
  const POLL_INTERVAL_MS = 1500;
  const SETUP_VIDEO_FAIL_TIMEOUT_MS = 2500;
  const BOOT_VIDEO_FAIL_TIMEOUT_MS = 3000;
  const BOOT_VIDEO_HARD_TIMEOUT_MS = 30000;

  // ------------------------------------------------------------------
  // State
  // ------------------------------------------------------------------
  /** @type {'init'|'setup-video'|'setup-fallback'|'boot-video'|'redirecting'} */
  let stage = 'init';
  let pollTimer = null;
  let particles = null;

  // ------------------------------------------------------------------
  // DOM helpers
  // ------------------------------------------------------------------
  function $(id) { return document.getElementById(id); }

  function show(el)  { if (el) el.hidden = false; }
  function hide(el)  { if (el) el.hidden = true; }

  function showSetupVideo() {
    if (stage === 'setup-video') return;
    stage = 'setup-video';
    stopParticles();

    hide($('kiosk-boot-video'));
    hide($('kiosk-particles'));
    hide($('kiosk-fallback'));

    const v = $('kiosk-setup-video');
    show(v);
    show($('kiosk-device-id-overlay'));

    let failed = false;
    const failTimer = setTimeout(() => {
      // If the video file isn't present (or the codec is unsupported)
      // the readyState will still be 0 here; flip to the fallback.
      if (v.readyState < 2 && stage === 'setup-video') {
        failed = true;
        showSetupFallback();
      }
    }, SETUP_VIDEO_FAIL_TIMEOUT_MS);

    v.addEventListener('canplay', () => {
      if (failed) return;
      clearTimeout(failTimer);
    }, { once: true });
    v.addEventListener('error', () => {
      clearTimeout(failTimer);
      if (stage === 'setup-video') showSetupFallback();
    }, { once: true });

    // Some browsers still need an explicit play() call after autoplay
    // is ignored; muted+playsinline should let it through but we belt-
    // and-brace it.
    try {
      const p = v.play();
      if (p && typeof p.catch === 'function') {
        p.catch(() => { /* will be picked up by the failTimer */ });
      }
    } catch (_e) { /* same */ }
  }

  function showSetupFallback() {
    if (stage === 'setup-fallback') return;
    stage = 'setup-fallback';

    const v = $('kiosk-setup-video');
    if (v) { try { v.pause(); } catch (_e) {} }
    hide(v);
    hide($('kiosk-boot-video'));
    hide($('kiosk-device-id-overlay'));

    show($('kiosk-particles'));
    show($('kiosk-fallback'));
    startParticles();
  }

  function showBootVideo() {
    if (stage === 'boot-video' || stage === 'redirecting') return;
    stage = 'boot-video';
    stopPolling();
    stopParticles();

    const sv = $('kiosk-setup-video');
    if (sv) { try { sv.pause(); } catch (_e) {} }

    hide(sv);
    hide($('kiosk-particles'));
    hide($('kiosk-fallback'));
    hide($('kiosk-device-id-overlay'));

    const b = $('kiosk-boot-video');
    show(b);

    const goDashboard = () => {
      if (stage === 'redirecting') return;
      stage = 'redirecting';
      window.location.href = '/dashboard';
    };

    let bootFailed = false;
    const bootFailTimer = setTimeout(() => {
      if (b.readyState < 2 && stage === 'boot-video') {
        bootFailed = true;
        goDashboard();
      }
    }, BOOT_VIDEO_FAIL_TIMEOUT_MS);

    b.addEventListener('canplay', () => {
      if (bootFailed) return;
      clearTimeout(bootFailTimer);
    }, { once: true });
    b.addEventListener('error', () => {
      clearTimeout(bootFailTimer);
      goDashboard();
    }, { once: true });
    b.addEventListener('ended', goDashboard, { once: true });

    // Hard safety: if 'ended' never fires (some Chromium codecs fire
    // 'pause' instead) just bail after 30s.
    setTimeout(() => {
      if (stage === 'boot-video') goDashboard();
    }, BOOT_VIDEO_HARD_TIMEOUT_MS);

    try {
      const p = b.play();
      if (p && typeof p.catch === 'function') {
        p.catch(() => { /* picked up by failTimer */ });
      }
    } catch (_e) { /* same */ }
  }

  // ------------------------------------------------------------------
  // Status polling
  // ------------------------------------------------------------------
  async function checkStatus() {
    try {
      const r = await fetch(STATUS_URL, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!r.ok) return;
      const data = await r.json();
      if (data && data.configured) {
        showBootVideo();
      }
    } catch (_e) {
      // Transient — leave the current stage in place. The kiosk should
      // never go blank just because one poll failed.
    }
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(checkStatus, POLL_INTERVAL_MS);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // ------------------------------------------------------------------
  // Canvas particles (QDRN-style — slow drifting dots with subtle
  // connecting lines, blue + amber accents).
  // ------------------------------------------------------------------
  function startParticles() {
    if (particles) return;
    const canvas = $('kiosk-particles');
    if (!canvas || !canvas.getContext) return;
    const ctx = canvas.getContext('2d');

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    function resize() {
      canvas.width = Math.floor(window.innerWidth * dpr);
      canvas.height = Math.floor(window.innerHeight * dpr);
      canvas.style.width = window.innerWidth + 'px';
      canvas.style.height = window.innerHeight + 'px';
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.scale(dpr, dpr);
    }
    resize();
    window.addEventListener('resize', resize);

    const NUM = Math.min(110, Math.max(60, Math.round(window.innerWidth / 18)));
    const dots = [];
    for (let i = 0; i < NUM; i++) {
      dots.push({
        x: Math.random() * window.innerWidth,
        y: Math.random() * window.innerHeight,
        vx: (Math.random() - 0.5) * 0.18,
        vy: (Math.random() - 0.5) * 0.18,
        r: Math.random() * 1.6 + 0.4,
        amber: Math.random() < 0.18,
      });
    }

    let raf = null;
    function frame() {
      const w = window.innerWidth, h = window.innerHeight;
      ctx.clearRect(0, 0, w, h);

      // Lines between nearby dots
      for (let i = 0; i < dots.length; i++) {
        for (let j = i + 1; j < dots.length; j++) {
          const a = dots[i], b = dots[j];
          const dx = a.x - b.x, dy = a.y - b.y;
          const d2 = dx * dx + dy * dy;
          if (d2 < 130 * 130) {
            const alpha = 0.18 * (1 - Math.sqrt(d2) / 130);
            ctx.strokeStyle = 'rgba(120, 170, 255,' + alpha.toFixed(3) + ')';
            ctx.lineWidth = 0.6;
            ctx.beginPath();
            ctx.moveTo(a.x, a.y);
            ctx.lineTo(b.x, b.y);
            ctx.stroke();
          }
        }
      }

      // Dots
      for (let i = 0; i < dots.length; i++) {
        const d = dots[i];
        d.x += d.vx; d.y += d.vy;
        if (d.x < -10) d.x = w + 10;
        if (d.x > w + 10) d.x = -10;
        if (d.y < -10) d.y = h + 10;
        if (d.y > h + 10) d.y = -10;
        ctx.beginPath();
        ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
        ctx.fillStyle = d.amber
          ? 'rgba(255, 188, 110, 0.85)'
          : 'rgba(140, 188, 255, 0.80)';
        ctx.fill();
      }

      raf = requestAnimationFrame(frame);
    }
    raf = requestAnimationFrame(frame);

    particles = {
      stop: () => {
        if (raf) cancelAnimationFrame(raf);
        window.removeEventListener('resize', resize);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
      },
    };
  }

  function stopParticles() {
    if (particles && particles.stop) {
      particles.stop();
    }
    particles = null;
  }

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', () => {
    const initiallyConfigured = document.body.dataset.configured === '1';
    if (initiallyConfigured) {
      // Device is already configured — Chromium just reloaded /kiosk
      // (e.g. after a power cycle). Play the boot video once, then
      // hand off to /dashboard.
      showBootVideo();
    } else {
      // First-boot state — looping setup video, polling for config.
      showSetupVideo();
      startPolling();
      // Also do an immediate status check so a fast wizard finish
      // (under 1.5s) doesn't get stuck on the setup video for a tick.
      checkStatus();
    }
  });
})();
