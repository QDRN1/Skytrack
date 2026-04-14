/* SkyTrack on-device kiosk display.
 *
 * Drives /kiosk — the fullscreen Chromium target running on the Pi's HDMI
 * output. This is the ENTIRE operator experience on the appliance; it never
 * navigates to another page (except to fall back to the file:// splash if
 * the backend dies at runtime), never scrolls, never shows browser chrome.
 *
 * Three visual stages only:
 *
 *   1. setup-video    — unconfigured: loop the branded setup video with a
 *                       device-ID pill at the top. If the video file fails
 *                       to decode, fall back to a canvas-particles
 *                       background + "SETUP REQUIRED" text card.
 *
 *   2. boot-video     — one-shot transition played the moment configured
 *                       flips true (fresh setup) OR when the kiosk loads
 *                       already-configured (e.g. after a reboot).
 *
 *   3. operational    — permanent live-display mode. Compact 2x2 stat grid
 *                       with branded header (device/clock/weather) and
 *                       footer (device-id + radar URL). Polls the public
 *                       dashboard APIs every ~5s. No redirects, no scroll,
 *                       no browser chrome.
 *
 * Status polling checks /api/auth/status on a short interval so a fresh
 * setup finish on the admin's phone transitions the kiosk automatically.
 *
 * RUNTIME WATCHDOG:
 * In parallel with everything else we poll /healthz every 5s. If the
 * backend stops answering for HEALTH_FALLBACK_MS we navigate back to the
 * file:// splash (with ?from=kiosk so it jumps straight to the "service
 * unavailable" video state). The splash continues polling and will bounce
 * back to /kiosk the moment the backend recovers. Without this, a runtime
 * app crash would leave the kiosk rendering stale data forever.
 */
(function () {
  'use strict';

  const STATUS_URL  = '/api/auth/status';
  const CARDS_URL   = '/api/dashboard/cards';
  const WEATHER_URL = '/api/weather';
  const DEVICE_URL  = '/api/device';
  const HEALTH_URL  = '/healthz';
  const SPLASH_URL  = 'file:///opt/skytrack/static/splash/index.html?from=kiosk';

  const STATUS_POLL_MS        = 1500;   // only runs in setup-video stage
  const OP_CARDS_POLL_MS      = 5000;
  const OP_WEATHER_POLL_MS    = 5 * 60 * 1000;
  const OP_DEVICE_POLL_MS     = 60 * 1000;
  const SETUP_VIDEO_FAIL_MS   = 2500;
  const BOOT_VIDEO_FAIL_MS    = 3000;
  const BOOT_VIDEO_HARD_MS    = 30000;

  // Runtime watchdog — fall back to splash if /healthz is down > this long.
  const HEALTH_POLL_MS        = 5000;
  const HEALTH_FALLBACK_MS    = 30000;

  // ------------------------------------------------------------------
  // State
  // ------------------------------------------------------------------
  /** @type {'init'|'setup-video'|'setup-fallback'|'boot-video'|'operational'} */
  let stage = 'init';

  let statusTimer   = null;
  let opCardsTimer  = null;
  let opWxTimer     = null;
  let opDevTimer    = null;
  let opClockTimer  = null;
  let healthTimer   = null;
  let particles     = null;

  // Watchdog — timestamp of last successful /healthz response.
  // Seeded to now() so a slow first response doesn't trigger a bounce.
  let lastHealthyAt = Date.now();
  let fellBack      = false;

  // ------------------------------------------------------------------
  // DOM helpers
  // ------------------------------------------------------------------
  function $(id)    { return document.getElementById(id); }
  function show(el) { if (el) el.hidden = false; }
  function hide(el) { if (el) el.hidden = true; }

  function setText(id, value) {
    const el = $(id);
    if (el && el.textContent !== value) el.textContent = value;
  }

  // ------------------------------------------------------------------
  // Setup-video stage (unconfigured)
  // ------------------------------------------------------------------
  function showSetupVideo() {
    if (stage === 'setup-video') return;
    stage = 'setup-video';
    stopParticles();
    hideOp();

    hide($('kiosk-boot-video'));
    hide($('kiosk-particles'));
    hide($('kiosk-fallback'));

    const v = $('kiosk-setup-video');
    show(v);
    show($('kiosk-device-id-overlay'));

    let failed = false;
    const failTimer = setTimeout(() => {
      if (v && v.readyState < 2 && stage === 'setup-video') {
        failed = true;
        showSetupFallback();
      }
    }, SETUP_VIDEO_FAIL_MS);

    if (v) {
      v.addEventListener('canplay', () => {
        if (failed) return;
        clearTimeout(failTimer);
      }, { once: true });
      v.addEventListener('error', () => {
        clearTimeout(failTimer);
        if (stage === 'setup-video') showSetupFallback();
      }, { once: true });

      try {
        const p = v.play();
        if (p && typeof p.catch === 'function') {
          p.catch(() => { /* handled by failTimer */ });
        }
      } catch (_e) { /* same */ }
    }
  }

  function showSetupFallback() {
    if (stage === 'setup-fallback') return;
    stage = 'setup-fallback';

    const v = $('kiosk-setup-video');
    if (v) { try { v.pause(); } catch (_e) {} }
    hide(v);
    hide($('kiosk-boot-video'));
    hide($('kiosk-device-id-overlay'));
    hideOp();

    show($('kiosk-particles'));
    show($('kiosk-fallback'));
    startParticles();
  }

  // ------------------------------------------------------------------
  // Boot-video stage (one-shot transition)
  // ------------------------------------------------------------------
  function showBootVideo() {
    if (stage === 'boot-video' || stage === 'operational') return;
    stage = 'boot-video';
    stopStatusPolling();
    stopParticles();

    const sv = $('kiosk-setup-video');
    if (sv) { try { sv.pause(); } catch (_e) {} }

    hide(sv);
    hide($('kiosk-particles'));
    hide($('kiosk-fallback'));
    hide($('kiosk-device-id-overlay'));
    hideOp();

    const b = $('kiosk-boot-video');
    show(b);

    const done = () => {
      if (stage === 'operational') return;
      showOperational();
    };

    let bootFailed = false;
    const bootFailTimer = setTimeout(() => {
      if (b && b.readyState < 2 && stage === 'boot-video') {
        bootFailed = true;
        done();
      }
    }, BOOT_VIDEO_FAIL_MS);

    if (b) {
      b.addEventListener('canplay', () => {
        if (bootFailed) return;
        clearTimeout(bootFailTimer);
      }, { once: true });
      b.addEventListener('error', () => {
        clearTimeout(bootFailTimer);
        done();
      }, { once: true });
      b.addEventListener('ended', done, { once: true });

      // Hard safety — some Chromium codecs fire 'pause' instead of 'ended'
      setTimeout(() => {
        if (stage === 'boot-video') done();
      }, BOOT_VIDEO_HARD_MS);

      try {
        const p = b.play();
        if (p && typeof p.catch === 'function') {
          p.catch(() => { /* handled by timers */ });
        }
      } catch (_e) { /* same */ }
    } else {
      done();
    }
  }

  // ------------------------------------------------------------------
  // Operational stage (permanent live display)
  // ------------------------------------------------------------------
  function showOperational() {
    if (stage === 'operational') return;
    stage = 'operational';
    stopStatusPolling();
    stopParticles();

    const sv = $('kiosk-setup-video');
    const bv = $('kiosk-boot-video');
    if (sv) { try { sv.pause(); } catch (_e) {} }
    if (bv) { try { bv.pause(); } catch (_e) {} }

    hide(sv);
    hide(bv);
    hide($('kiosk-particles'));
    hide($('kiosk-fallback'));
    hide($('kiosk-device-id-overlay'));

    show($('kiosk-op'));

    startClock();
    refreshCards();
    refreshWeather();
    refreshDevice();

    if (!opCardsTimer) opCardsTimer = setInterval(refreshCards,  OP_CARDS_POLL_MS);
    if (!opWxTimer)    opWxTimer    = setInterval(refreshWeather, OP_WEATHER_POLL_MS);
    if (!opDevTimer)   opDevTimer   = setInterval(refreshDevice,  OP_DEVICE_POLL_MS);
  }

  function hideOp() {
    hide($('kiosk-op'));
    stopOperationalTimers();
  }

  function stopOperationalTimers() {
    if (opCardsTimer) { clearInterval(opCardsTimer); opCardsTimer = null; }
    if (opWxTimer)    { clearInterval(opWxTimer);    opWxTimer    = null; }
    if (opDevTimer)   { clearInterval(opDevTimer);   opDevTimer   = null; }
    if (opClockTimer) { clearInterval(opClockTimer); opClockTimer = null; }
  }

  function startClock() {
    const tick = () => {
      const now = new Date();
      const hh = String(now.getHours()).padStart(2, '0');
      const mm = String(now.getMinutes()).padStart(2, '0');
      setText('op-clock-time', hh + ':' + mm);
      const opts = { weekday: 'short', month: 'short', day: 'numeric' };
      try {
        setText('op-clock-date', now.toLocaleDateString(undefined, opts).toUpperCase());
      } catch (_e) {
        setText('op-clock-date', '');
      }
    };
    tick();
    if (!opClockTimer) opClockTimer = setInterval(tick, 15 * 1000);
  }

  function fmtNumber(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    try { return Number(n).toLocaleString(); } catch (_e) { return String(n); }
  }

  async function refreshCards() {
    try {
      const r = await fetch(CARDS_URL, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!r.ok) return;
      const data = await r.json();

      if (data.now) {
        setText('op-now-value', fmtNumber(data.now.count));
        setText('op-now-sub',   data.now.window || 'last 5 min');
      }
      if (data.today) {
        setText('op-today-value', fmtNumber(data.today.count));
        setText('op-today-sub',   data.today.window || 'today (UTC)');
      }
      if (data.busiest) {
        const hour = data.busiest.hour;
        if (hour === null || hour === undefined || hour === '') {
          setText('op-busy-value', '—');
          setText('op-busy-sub',   'last 24 h');
        } else {
          const h = String(hour).padStart(2, '0') + ':00';
          setText('op-busy-value', h);
          const n = data.busiest.count;
          setText('op-busy-sub', (n !== undefined && n !== null)
            ? fmtNumber(n) + ' aircraft'
            : 'last 24 h');
        }
      }
      if (data.last) {
        const last = data.last;
        const label = (last.callsign && last.callsign.trim())
          || (last.icao && last.icao.toUpperCase())
          || null;
        if (label) {
          setText('op-last-value', label);
          const bits = [];
          if (last.altitude_ft !== null && last.altitude_ft !== undefined) {
            const ft = Number(last.altitude_ft);
            if (!isNaN(ft)) {
              const fl = Math.round(ft / 100);
              bits.push('FL' + String(fl).padStart(3, '0'));
            }
          }
          if (last.speed_kts !== null && last.speed_kts !== undefined) {
            const kts = Number(last.speed_kts);
            if (!isNaN(kts)) bits.push(Math.round(kts) + ' kt');
          }
          setText('op-last-sub', bits.length ? bits.join(' · ') : 'last 5 min');
        } else {
          setText('op-last-value', '—');
          setText('op-last-sub',   'awaiting contacts');
        }
      } else {
        setText('op-last-value', '—');
        setText('op-last-sub',   'awaiting contacts');
      }
    } catch (_e) {
      /* Transient — leave stale values; the kiosk must never go blank. */
    }
  }

  async function refreshWeather() {
    try {
      const r = await fetch(WEATHER_URL, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!r.ok) return;
      const data = await r.json();
      const cur = (data && data.current) || null;
      if (!cur) return;
      if (cur.temp_f !== undefined && cur.temp_f !== null) {
        setText('op-wx-temp', Math.round(Number(cur.temp_f)) + '°');
      }
      if (cur.condition) {
        setText('op-wx-cond', String(cur.condition));
      }
    } catch (_e) { /* transient */ }
  }

  async function refreshDevice() {
    try {
      const r = await fetch(DEVICE_URL, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!r.ok) return;
      const data = await r.json();
      if (data && data.radar_url) {
        // Strip protocol so the footer doesn't look like a browser URL bar
        const clean = String(data.radar_url).replace(/^https?:\/\//i, '');
        setText('op-radar-url', clean);
      }
    } catch (_e) { /* transient */ }
  }

  // ------------------------------------------------------------------
  // Setup-state status polling (only while unconfigured)
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
    } catch (_e) { /* transient */ }
  }

  function startStatusPolling() {
    if (statusTimer) return;
    statusTimer = setInterval(checkStatus, STATUS_POLL_MS);
  }
  function stopStatusPolling() {
    if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
  }

  // ------------------------------------------------------------------
  // Runtime healthz watchdog — fall back to splash if the backend dies.
  // ------------------------------------------------------------------
  async function checkHealth() {
    if (fellBack) return;
    let ok = false;
    try {
      const ctl = typeof AbortController === 'function' ? new AbortController() : null;
      const to  = ctl ? setTimeout(() => ctl.abort(), 2500) : null;
      const r = await fetch(HEALTH_URL, {
        credentials: 'same-origin',
        cache: 'no-store',
        signal: ctl ? ctl.signal : undefined,
      });
      if (to) clearTimeout(to);
      if (r && r.ok) ok = true;
    } catch (_e) { /* treated as failure */ }

    if (ok) {
      lastHealthyAt = Date.now();
      return;
    }
    const down = Date.now() - lastHealthyAt;
    if (down >= HEALTH_FALLBACK_MS) {
      fellBack = true;
      // Clean up before we navigate away, otherwise the splash inherits
      // leftover timers via back/forward cache.
      stopStatusPolling();
      stopOperationalTimers();
      stopParticles();
      if (healthTimer) { clearInterval(healthTimer); healthTimer = null; }
      try { window.location.replace(SPLASH_URL); }
      catch (_e) { window.location.href = SPLASH_URL; }
    }
  }

  function startHealthWatchdog() {
    if (healthTimer) return;
    lastHealthyAt = Date.now();
    healthTimer = setInterval(checkHealth, HEALTH_POLL_MS);
  }

  // ------------------------------------------------------------------
  // Canvas particles (only used as a setup-state fallback)
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
    if (particles && particles.stop) particles.stop();
    particles = null;
  }

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', () => {
    const initiallyConfigured = document.body.dataset.configured === '1';
    if (initiallyConfigured) {
      // Already configured — play the boot video once, then settle into the
      // operational stage. Never redirect anywhere (unless the runtime
      // watchdog trips).
      showBootVideo();
    } else {
      // First-boot — loop the setup video and poll for completion.
      showSetupVideo();
      startStatusPolling();
      // Immediate check so a fast wizard finish doesn't linger for a tick.
      checkStatus();
    }
    // Runtime watchdog runs in every stage.
    startHealthWatchdog();
  });

  // If Chromium is hard-reloaded mid-session, clean up any timers first.
  window.addEventListener('beforeunload', () => {
    stopStatusPolling();
    stopOperationalTimers();
    stopParticles();
    if (healthTimer) { clearInterval(healthTimer); healthTimer = null; }
  });
})();
