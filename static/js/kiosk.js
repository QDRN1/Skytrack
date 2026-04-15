/* SkyTrack on-device kiosk display.
 *
 * Drives /kiosk — the fullscreen Chromium target on the appliance HDMI
 * output. Branches on the canonical onboarding state machine returned
 * by /api/onboarding/state and never navigates anywhere else (except
 * the file:// splash if the runtime watchdog trips on a backend death).
 *
 * Visual stages map 1:1 to the onboarding stages in onboarding.py:
 *
 *   boot_video         → one-shot intro video (auto-advances)
 *   setup_video        → looping branded explainer (auto-advances after a
 *                        short hold so the operator sees it once)
 *   pin_required       → on-screen 4–8 digit PIN keypad (first entry)
 *   pin_confirm        → re-enter the same PIN
 *   setup_offer        → "Set Up Now / Skip For Now" prompt
 *   setup_now_hotspot  → hotspot credentials reveal card
 *   operational        → permanent compact live dashboard
 *
 * The poll loop calls /api/onboarding/state every ~1.5s so the kiosk
 * follows along when the admin's phone wizard pushes the state machine
 * forward, or when a super-user resets onboarding from the console.
 *
 * Runtime watchdog: in parallel with everything else we poll /healthz
 * and fall back to the file:// splash if the backend stops answering
 * for HEALTH_FALLBACK_MS. The splash continues polling and bounces back
 * to /kiosk the moment the backend recovers.
 */
(function () {
  'use strict';

  // ----- Endpoints -----------------------------------------------------
  const ONBOARD_URL = '/api/onboarding/state';
  const ADVANCE_URL = '/api/onboarding/advance';
  const PIN_URL     = '/api/onboarding/pin';
  const RESET_URL   = '/api/onboarding/reset';
  const CARDS_URL   = '/api/dashboard/cards';
  const WEATHER_URL = '/api/weather';
  const DEVICE_URL  = '/api/device';
  const HEALTH_URL  = '/healthz';
  const HOTSPOT_URL = '/api/hotspot/health';
  const SPLASH_URL  = 'file:///opt/skytrack/static/splash/index.html?from=kiosk';

  // ----- Cadences ------------------------------------------------------
  const ONB_POLL_MS         = 1500;
  const OP_CARDS_POLL_MS    = 5000;
  const OP_WEATHER_POLL_MS  = 5 * 60 * 1000;
  const OP_DEVICE_POLL_MS   = 60 * 1000;
  const SETUP_VIDEO_FAIL_MS = 2500;
  const SETUP_VIDEO_HOLD_MS = 6000;
  const BOOT_VIDEO_FAIL_MS  = 3000;
  const BOOT_VIDEO_HARD_MS  = 12000;
  const HEALTH_POLL_MS      = 5000;
  const HEALTH_FALLBACK_MS  = 30000;
  // Hotspot truth probe — poll faster than onboarding so the card
  // feels alive while the operator waits for a phone to join.
  const HOTSPOT_POLL_MS     = 3000;
  // Grace window before a non-usable hotspot gets flagged as "broken".
  // On a cold boot hostapd + dnsmasq + the wlan0 IP can legitimately
  // take 15-20 seconds to all come up, so we show "Starting hotspot…"
  // for the first STARTING_GRACE_MS of the card's lifetime and only
  // flip to the red "broken" state after that window closes.
  const STARTING_GRACE_MS   = 20000;

  // ----- Mutable state -------------------------------------------------
  // visual = the screen currently painted in the DOM. NOT the persistent
  // server stage — that's `serverStage` below.
  let visual      = 'init';
  let serverStage = (document.body && document.body.dataset.stage) || 'boot_video';
  let pinSetCached = (document.body && document.body.dataset.pinSet === '1');

  let onbTimer = null, opCardsTimer = null, opWxTimer = null;
  let opDevTimer = null, opClockTimer = null, healthTimer = null;
  let hotspotTimer = null;
  // Timestamp at which the hotspot card first became visible. Used as
  // the anchor for the STARTING_GRACE_MS window — we avoid flashing a
  // red "not usable" state during the normal 15-20s cold-boot window
  // while services are still coming up.
  let hotspotCardShownAt = 0;
  let particles = null;

  // PIN keypad local buffers (the first PIN never leaves the kiosk; we
  // only POST after a local match against the second entry).
  let pinFirst = '';
  let pinBuf   = '';

  // Health watchdog — timestamp of last successful /healthz response.
  let lastHealthyAt = Date.now();
  let fellBack = false;

  // ----- DOM helpers ---------------------------------------------------
  function $(id)    { return document.getElementById(id); }
  function show(el) { if (el) el.hidden = false; }
  function hide(el) { if (el) el.hidden = true; }
  function setText(id, v) {
    const el = $(id);
    if (el && el.textContent !== v) el.textContent = v;
  }

  function hideAllScreens() {
    hide($('kiosk-setup-video'));
    hide($('kiosk-boot-video'));
    hide($('kiosk-particles'));
    hide($('kiosk-fallback'));
    hide($('kiosk-device-id-overlay'));
    hide($('kiosk-pin'));
    hide($('kiosk-offer'));
    hide($('kiosk-hotspot'));
    hide($('kiosk-op'));
    stopParticles();
    stopOperationalTimers();
    stopHotspotHealthPoll();
    const sv = $('kiosk-setup-video');
    const bv = $('kiosk-boot-video');
    if (sv) { try { sv.pause(); } catch (_e) {} }
    if (bv) { try { bv.pause(); } catch (_e) {} }
  }

  // ----- Stage dispatch ------------------------------------------------
  // applyStage is the single source of truth that decides which DOM
  // screen is visible. The poll loop calls it on every tick; it bails
  // immediately if the visual already matches the requested stage.
  function applyStage(stage) {
    if (stage === serverStage && visual !== 'init') return;
    serverStage = stage;
    switch (stage) {
      case 'boot_video':        showBootVideo(true);   break;
      case 'setup_video':       showSetupVideo();      break;
      case 'pin_required':      showPinPad('first');   break;
      case 'pin_confirm':       showPinPad('confirm'); break;
      case 'setup_offer':       showSetupOffer();      break;
      case 'setup_now_hotspot': showHotspotCard();     break;
      case 'operational':       showOperational();     break;
      default:                  showSetupFallback();
    }
  }

  // ----- Boot video stage --------------------------------------------
  // Plays the one-shot boot loop. On a fresh boot we tell the server to
  // advance to setup_video when it ends; on an already-configured device
  // the server stage is operational, so we only land here as a transient
  // visual when the user manually advance back through setup.
  function showBootVideo(advanceWhenDone) {
    if (visual === 'boot-video') return;
    visual = 'boot-video';
    hideAllScreens();
    const b = $('kiosk-boot-video');
    show(b);

    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      if (advanceWhenDone && serverStage === 'boot_video') {
        advanceServer('setup_video').catch(() => {});
      }
    };

    const failTimer = setTimeout(finish, BOOT_VIDEO_FAIL_MS);
    setTimeout(finish, BOOT_VIDEO_HARD_MS);

    if (b) {
      b.addEventListener('canplay', () => clearTimeout(failTimer), { once: true });
      b.addEventListener('error', finish, { once: true });
      b.addEventListener('ended', finish, { once: true });
      try {
        const p = b.play();
        if (p && typeof p.catch === 'function') p.catch(() => {});
      } catch (_e) {}
    } else {
      finish();
    }
  }

  // ----- Setup video stage -------------------------------------------
  // Loops the branded explainer. Hold for SETUP_VIDEO_HOLD_MS so the
  // operator gets a clean read of it, then ask the server to advance to
  // pin_required. The poll loop will paint the keypad on the next tick.
  function showSetupVideo() {
    if (visual === 'setup-video') return;
    visual = 'setup-video';
    hideAllScreens();
    const v = $('kiosk-setup-video');
    show(v);
    show($('kiosk-device-id-overlay'));

    let advanced = false;
    const askAdvance = () => {
      if (advanced) return;
      advanced = true;
      setTimeout(() => {
        if (serverStage === 'setup_video') {
          advanceServer('pin_required').catch(() => {});
        }
      }, SETUP_VIDEO_HOLD_MS);
    };

    const failTimer = setTimeout(() => {
      if (v && v.readyState < 2 && visual === 'setup-video') {
        showSetupFallback();
        askAdvance();
      }
    }, SETUP_VIDEO_FAIL_MS);

    if (v) {
      v.addEventListener('canplay', () => {
        clearTimeout(failTimer);
        askAdvance();
      }, { once: true });
      v.addEventListener('error', () => {
        clearTimeout(failTimer);
        showSetupFallback();
        askAdvance();
      }, { once: true });
      try {
        const p = v.play();
        if (p && typeof p.catch === 'function') p.catch(() => {});
      } catch (_e) {}
    } else {
      showSetupFallback();
      askAdvance();
    }
  }

  function showSetupFallback() {
    if (visual === 'setup-fallback') return;
    visual = 'setup-fallback';
    hide($('kiosk-setup-video'));
    hide($('kiosk-boot-video'));
    show($('kiosk-particles'));
    show($('kiosk-fallback'));
    startParticles();
  }

  // ----- PIN keypad ---------------------------------------------------
  // Drives both pin_required (first entry) and pin_confirm (re-entry).
  // The first PIN is held in JS only; we POST to /api/onboarding/pin
  // once both entries match locally. On a mismatch we POST to
  // /api/onboarding/reset and re-paint the first keypad.
  //
  // Resume safety: pinFirst is in-memory only, but the server stage is
  // persistent across reboots/reloads. Any code path that lands on
  // 'confirm' without a live in-memory pinFirst — a Pi reboot mid-flow,
  // a kiosk reload triggered by the /healthz watchdog, a Chromium
  // relaunch — would otherwise paint "Confirm Admin PIN" with nothing
  // to confirm against. The operator would type a PIN, hit Enter, get
  // a mismatch, get kicked to "Set Admin PIN", then back to "Confirm
  // Admin PIN" — three keypad screens instead of two. showPinPad
  // detects this and rewinds to 'first' both locally and on the server.
  function showPinPad(mode) {
    if (mode === 'confirm' && !pinFirst) {
      // Tell the server to rewind so a parallel poll doesn't paint us
      // right back into pin_confirm. Fire-and-forget: the local view
      // is already going to 'first', and the next onboarding poll will
      // confirm the server is in sync.
      serverStage = 'pin_required';
      fetch(RESET_URL, { method: 'POST', credentials: 'same-origin' })
        .catch(() => {});
      mode = 'first';
    }
    const visualName = (mode === 'confirm') ? 'pin-confirm' : 'pin-required';
    if (visual === visualName) return;
    visual = visualName;
    hideAllScreens();
    show($('kiosk-pin'));
    if (mode === 'confirm') {
      setText('kiosk-pin-title', 'Confirm Admin PIN');
      setText('kiosk-pin-sub',   'Re-enter the same digits');
    } else {
      setText('kiosk-pin-title', 'Set Admin PIN');
      setText('kiosk-pin-sub',   'Choose 4 to 8 digits');
      pinFirst = '';
    }
    pinBuf = '';
    paintPinDots();
    setText('kiosk-pin-error', '\u00A0');
  }

  function paintPinDots() {
    const wrap = $('kiosk-pin-dots');
    if (!wrap) return;
    const slots = Math.max(4, Math.min(pinBuf.length || 4, 8));
    let html = '';
    for (let i = 0; i < slots; i++) {
      html += '<span class="pin-dot' + (i < pinBuf.length ? ' filled' : '') + '"></span>';
    }
    wrap.innerHTML = html;
  }

  function pinKey(k) {
    if (pinBuf.length >= 8) return;
    pinBuf += k;
    paintPinDots();
  }
  function pinClear() {
    pinBuf = '';
    paintPinDots();
    setText('kiosk-pin-error', '\u00A0');
  }
  function pinError(msg) {
    setText('kiosk-pin-error', msg);
  }

  async function pinSubmit() {
    if (pinBuf.length < 4) { pinError('Enter at least 4 digits'); return; }
    if (pinBuf.length > 8) { pinError('PIN is too long'); return; }
    if (!/^\d+$/.test(pinBuf)) { pinError('Digits only'); return; }

    if (visual === 'pin-required') {
      pinFirst = pinBuf;
      pinBuf = '';
      try {
        await advanceServer('pin_confirm');
        showPinPad('confirm');
      } catch (_e) {
        pinError('Could not advance — try again');
      }
      return;
    }

    if (visual === 'pin-confirm') {
      if (pinBuf !== pinFirst) {
        pinFirst = '';
        pinBuf   = '';
        pinError('PINs did not match — start over');
        try {
          await fetch(RESET_URL, { method: 'POST', credentials: 'same-origin' });
        } catch (_e) {}
        showPinPad('first');
        return;
      }
      try {
        const r = await fetch(PIN_URL, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
          body: JSON.stringify({ pin: pinFirst, confirm: pinBuf }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok || !data.ok) {
          pinError(data.error || 'PIN save failed');
          pinFirst = ''; pinBuf = '';
          return;
        }
        pinSetCached = true;
        pinFirst = ''; pinBuf = '';
        // Server already advanced us to setup_offer; force one
        // immediate poll so the screen swaps without waiting for the
        // next 1.5s tick.
        pollOnboarding();
      } catch (e2) {
        pinError(e2.message || 'Network error');
      }
    }
  }

  // ----- Setup-offer prompt ------------------------------------------
  function showSetupOffer() {
    if (visual === 'setup-offer') return;
    visual = 'setup-offer';
    hideAllScreens();
    show($('kiosk-offer'));
  }

  // ----- Hotspot reveal card -----------------------------------------
  // Credentials come from /api/onboarding/state, which only exposes the
  // password while the persistent stage IS setup_now_hotspot. That keeps
  // the cred bundle off the wire any other time.
  function showHotspotCard() {
    if (visual === 'hotspot') return;
    visual = 'hotspot';
    hideAllScreens();
    show($('kiosk-hotspot'));
    // Phase 2.2: start the live hotspot truth poll while this card is
    // visible. The anchor timestamp drives STARTING_GRACE_MS so we
    // don't flash "hotspot broken" during the normal cold-boot window.
    hotspotCardShownAt = Date.now();
    paintHotspotStatus(null);  // reset to "Checking…" instantly
    startHotspotHealthPoll();
  }

  // ----- Operational stage -------------------------------------------
  function showOperational() {
    if (visual === 'operational') return;
    visual = 'operational';
    hideAllScreens();
    show($('kiosk-op'));

    startClock();
    refreshCards();
    refreshWeather();
    refreshDevice();

    if (!opCardsTimer) opCardsTimer = setInterval(refreshCards,   OP_CARDS_POLL_MS);
    if (!opWxTimer)    opWxTimer    = setInterval(refreshWeather, OP_WEATHER_POLL_MS);
    if (!opDevTimer)   opDevTimer   = setInterval(refreshDevice,  OP_DEVICE_POLL_MS);
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
    } catch (_e) { /* transient */ }
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
        const clean = String(data.radar_url).replace(/^https?:\/\//i, '');
        setText('op-radar-url', clean);
      }
    } catch (_e) { /* transient */ }
  }

  // ----- Onboarding state polling ------------------------------------
  // Single engine that keeps the kiosk in sync with whatever happens
  // elsewhere (phone wizard, super-user console, hotspot watchdog).
  async function pollOnboarding() {
    try {
      const r = await fetch(ONBOARD_URL, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!r.ok) return;
      const s = await r.json();
      if (!s || typeof s.stage !== 'string') return;
      pinSetCached = !!s.pin_set;
      // The state endpoint surfaces hotspot creds while the stage is
      // setup_now_hotspot. Paint them whenever we can — covers both the
      // first-paint case and a Chromium reload mid-flow.
      if (s.stage === 'setup_now_hotspot') {
        if (s.hotspot_password) setText('kiosk-hs-pw', s.hotspot_password);
        if (s.hotspot_ssid)     setText('kiosk-hs-ssid', s.hotspot_ssid);
        if (s.hotspot_gateway)  setText('kiosk-hs-url', 'http://' + s.hotspot_gateway);
      }
      applyStage(s.stage);
    } catch (_e) { /* transient */ }
  }

  async function advanceServer(to) {
    const body = (to ? JSON.stringify({ to: to }) : '{}');
    const r = await fetch(ADVANCE_URL, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: body,
    });
    if (!r.ok) throw new Error('advance failed: ' + r.status);
    const data = await r.json().catch(() => ({}));
    if (data && data.state && data.state.stage) {
      applyStage(data.state.stage);
    }
    return data;
  }

  function startOnboardingPolling() {
    if (onbTimer) return;
    onbTimer = setInterval(pollOnboarding, ONB_POLL_MS);
  }
  function stopOnboardingPolling() {
    if (onbTimer) { clearInterval(onbTimer); onbTimer = null; }
  }

  // ----- Hotspot truth poll (phase 2.2) ------------------------------
  // Runs ONLY while the setup_now_hotspot card is on screen. Every
  // HOTSPOT_POLL_MS we fetch /api/hotspot/health (public, no auth) and
  // paint a live state line under the credentials. The UI state
  // machine below NEVER trusts the `stations` count on its own — we
  // explicitly require `usable && ap_mode && stations > 0` before
  // showing "connected", because on a broken AP the station dump can
  // still lie (see the on-Pi validation of 2.5.5 where stations=1
  // while ap_mode=false).
  async function pollHotspotHealth() {
    try {
      const ctl = typeof AbortController === 'function' ? new AbortController() : null;
      const to  = ctl ? setTimeout(() => ctl.abort(), 2500) : null;
      const r = await fetch(HOTSPOT_URL, {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { Accept: 'application/json' },
        signal: ctl ? ctl.signal : undefined,
      });
      if (to) clearTimeout(to);
      if (!r || !r.ok) { paintHotspotStatus(null); return; }
      const h = await r.json();
      paintHotspotStatus(h);
    } catch (_e) {
      // Transient fetch error — keep whatever state is on screen
      // rather than flapping to "checking". The next tick will refresh.
    }
  }

  // Map a /api/hotspot/health response (or null for "no data yet")
  // onto one of the five visual states defined in templates/kiosk.html.
  function paintHotspotStatus(h) {
    const el = $('kiosk-hs-status');
    if (!el) return;
    const textEl = el.querySelector('.hs-text');
    const setState = (cls, text) => {
      el.className = cls;
      if (textEl) textEl.textContent = text;
    };

    // 1) No response yet — first paint / transient network error.
    if (!h) {
      setState('hs-checking', 'Checking hotspot status…');
      return;
    }

    // 2) Healthy + a station is truly associated (AP mode confirmed).
    //    This is the ONLY branch that trusts h.stations > 0, and even
    //    then only after we've confirmed usable && ap_mode.
    if (h.usable && h.ap_mode && h.stations > 0) {
      const word = h.stations === 1 ? 'device' : 'devices';
      setState('hs-connected',
        h.stations + ' ' + word + ' connected — continue setup when ready');
      return;
    }

    // 3) Healthy, waiting for a phone to join.
    if (h.usable) {
      setState('hs-waiting',
        'Hotspot live — waiting for your device to join "' + (h.ssid || 'SkyTrack-Portal') + '"');
      return;
    }

    // 4) Not usable yet. Decide between "starting" (grace window) and
    //    "broken" (persistent failure). Hard rfkill or a soft-block
    //    is never a transient startup state, so we skip straight to
    //    the red state even inside the grace window.
    const hardFault = !!(h.rfkill_hard_blocked || h.rfkill_soft_blocked);
    const withinGrace = hotspotCardShownAt > 0
      && (Date.now() - hotspotCardShownAt) < STARTING_GRACE_MS;

    if (!hardFault && withinGrace) {
      setState('hs-starting', 'Starting hotspot…');
      return;
    }

    // 5) Persistent failure. Surface the first real reason so the
    //    operator can tell WHY. Truncate to keep the card tidy.
    const reasons = Array.isArray(h.degraded_reasons) ? h.degraded_reasons : [];
    const first = reasons[0] || 'hotspot not usable';
    const extra = reasons.length > 1 ? ' (+' + (reasons.length - 1) + ' more)' : '';
    setState('hs-broken', 'Hotspot not usable: ' + first + extra);
  }

  function startHotspotHealthPoll() {
    if (hotspotTimer) return;
    // Fire one immediate poll so the UI paints within the first tick
    // rather than sitting on "Checking…" for HOTSPOT_POLL_MS.
    pollHotspotHealth();
    hotspotTimer = setInterval(pollHotspotHealth, HOTSPOT_POLL_MS);
  }
  function stopHotspotHealthPoll() {
    if (hotspotTimer) { clearInterval(hotspotTimer); hotspotTimer = null; }
    hotspotCardShownAt = 0;
  }

  // ----- Health watchdog ---------------------------------------------
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

    if (ok) { lastHealthyAt = Date.now(); return; }
    if ((Date.now() - lastHealthyAt) >= HEALTH_FALLBACK_MS) {
      fellBack = true;
      stopOnboardingPolling();
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

  // ----- Canvas particles (setup-state fallback background) ---------
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

  // ----- Boot --------------------------------------------------------
  document.addEventListener('DOMContentLoaded', () => {
    // PIN keypad delegation — one listener for all 12 buttons.
    const pad = $('kiosk-pin-pad');
    if (pad) {
      pad.addEventListener('click', (e) => {
        const btn = e.target.closest('.pin-key');
        if (!btn) return;
        if (btn.dataset.action === 'clear') { pinClear();  return; }
        if (btn.dataset.action === 'enter') { pinSubmit(); return; }
        if (btn.dataset.key !== undefined)  { pinKey(btn.dataset.key); }
      });
      // Also support a hardware keyboard (helpful for development).
      window.addEventListener('keydown', (e) => {
        if (visual !== 'pin-required' && visual !== 'pin-confirm') return;
        if (e.key >= '0' && e.key <= '9') { pinKey(e.key); e.preventDefault(); }
        else if (e.key === 'Backspace')   { pinBuf = pinBuf.slice(0, -1); paintPinDots(); e.preventDefault(); }
        else if (e.key === 'Enter')       { pinSubmit(); e.preventDefault(); }
        else if (e.key === 'Escape')      { pinClear(); e.preventDefault(); }
      });
    }

    // Setup-offer buttons
    const setupNow  = $('kiosk-offer-now');
    const setupSkip = $('kiosk-offer-skip');
    if (setupNow)  setupNow.addEventListener('click',  () => advanceServer('setup_now_hotspot').catch(() => {}));
    if (setupSkip) setupSkip.addEventListener('click', () => advanceServer('operational').catch(() => {}));

    // Hotspot Continue button
    const hsDone = $('kiosk-hotspot-done');
    if (hsDone) hsDone.addEventListener('click', () => advanceServer('operational').catch(() => {}));

    // Resume safety (guard #1): on a fresh page load pinFirst is
    // definitionally empty, so a persisted server stage of pin_confirm
    // is always orphaned. Rewind it to pin_required before the first
    // paint, otherwise the operator sees a flash of "Confirm Admin PIN"
    // before guard #2 (inside showPinPad) catches it on the next tick.
    if (serverStage === 'pin_confirm') {
      serverStage = 'pin_required';
      fetch(RESET_URL, { method: 'POST', credentials: 'same-origin' })
        .catch(() => {});
    }

    // Paint whatever stage the server stamped on the body. If this is
    // a fresh boot we land on boot_video; if the device was previously
    // operational we go straight to the live dashboard.
    applyStage(serverStage);

    // Always start polling — the kiosk follows along with the phone
    // wizard and any super-user state changes.
    startOnboardingPolling();
    startHealthWatchdog();
  });

  // Hard reload safety — wipe timers so the new page doesn't inherit
  // them via the back/forward cache.
  window.addEventListener('beforeunload', () => {
    stopOnboardingPolling();
    stopOperationalTimers();
    stopParticles();
    if (healthTimer) { clearInterval(healthTimer); healthTimer = null; }
  });
})();
