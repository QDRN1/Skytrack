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
  const SKIP_URL    = '/api/onboarding/skip';
  const CARDS_URL    = '/api/dashboard/cards';
  const WEATHER_URL  = '/api/weather';
  const DEVICE_URL   = '/api/device';
  const HEALTH_URL   = '/healthz';
  const HOTSPOT_URL  = '/api/hotspot/health';
  const SPLASH_URL   = 'file:///opt/skytrack/static/splash/index.html?from=kiosk';
  const GPS_URL      = '/api/gps';
  const AIRLINES_URL = '/api/dashboard/airlines';
  const TREND_URL    = '/api/dashboard/trend';
  const POS_URL      = '/api/dashboard/positions';
  const KIOSK_CFG_URL = '/api/dashboard/kiosk-config';
  const TEMP_ALARM_URL = '/api/device-temp-alarm';
  const NET_STATUS_URL = '/api/network/status';

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
    hide($('kiosk-welcome'));
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
      case 'welcome':           showWelcome();         break;
      case 'pin_required':      showPinPad('first');   break;
      case 'pin_confirm':       showPinPad('confirm'); break;
      case 'setup_offer':       showSetupOffer();      break;
      case 'setup_now_hotspot': showHotspotCard();     break;
      case 'operational':       showOperational();     break;
      default:                  showSetupFallback();
    }
  }

  // ----- Welcome screen (Phase 2 stabilization) ----------------------
  // Lands between the looping setup video and the PIN keypad. The
  // operator sees branding + device info with a primary "Continue
  // setup" button that advances to pin_required and a secondary
  // "Set up later" escape that posts to /api/onboarding/skip with
  // source="welcome". The skip endpoint force-transitions the server
  // to operational with pin_set unchanged (false), and the next poll
  // tick will paint the operational dashboard.
  function showWelcome() {
    if (visual === 'welcome') return;
    visual = 'welcome';
    hideAllScreens();
    show($('kiosk-welcome'));
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
        // Phase 2 stabilization: advance to the new welcome card instead
        // of jumping straight into pin_required. The welcome card is
        // where the operator gets an explicit "Set up later" escape.
        if (serverStage === 'setup_video') {
          advanceServer('welcome').catch(() => {});
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
  // once both entries match locally. On a mismatch we rewind the
  // server stage back to pin_required via /api/onboarding/advance
  // (pin_confirm → pin_required is an allowed transition) and
  // re-paint the first keypad.
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
  //
  // NOTE: this used to POST to /api/onboarding/reset, but that endpoint
  // is now the admin-gated factory reset and rejects pre-auth callers.
  // The pre-auth pin-pad rewind is a legitimate transition along the
  // state machine, so we use /advance with {to: "pin_required"} — the
  // onboarding state machine allows pin_confirm → pin_required.
  function showPinPad(mode) {
    if (mode === 'confirm' && !pinFirst) {
      // Tell the server to rewind so a parallel poll doesn't paint us
      // right back into pin_confirm. Fire-and-forget: the local view
      // is already going to 'first', and the next onboarding poll will
      // confirm the server is in sync.
      serverStage = 'pin_required';
      fetch(ADVANCE_URL, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ to: 'pin_required' }),
      }).catch(() => {});
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
        // Rewind the server stage back to pin_required so the poll
        // loop doesn't immediately repaint us into pin_confirm. This
        // used to hit /api/onboarding/reset which is now the admin-
        // gated factory reset — we use /advance instead since the
        // pin_confirm → pin_required edge is an allowed transition.
        try {
          await fetch(ADVANCE_URL, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ to: 'pin_required' }),
          });
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
  // ===== CAROUSEL STATE =================================================
  let carouselPage = 0;
  let carouselPages = [];         // live NodeList of visible pages
  let carouselAutoTimer = null;
  let carouselInterval = 8000;
  let carouselMapInterval = 15000;
  let carouselMap = null;
  let carouselMarkers = {};
  let mapInitialized = false;
  let trendChart = null;
  // Touch/drag state
  let dragStartX = 0, dragDx = 0, isDragging = false;

  function showOperational() {
    if (visual === 'operational') return;
    visual = 'operational';
    hideAllScreens();
    show($('kiosk-op'));

    startClock();
    initMapEagerly();
    initCarousel();
    refreshCards();
    refreshWeather();
    refreshDevice();
    refreshAirlines();
    refreshTrend();
    refreshMap();
    startTempAlarmPoll();
    startNetStatusPoll();

    if (!opCardsTimer) opCardsTimer = setInterval(function () {
      refreshCards(); refreshAirlines(); refreshTrend(); refreshMap();
    }, OP_CARDS_POLL_MS);
    if (!opWxTimer)    opWxTimer    = setInterval(refreshWeather, OP_WEATHER_POLL_MS);
    if (!opDevTimer)   opDevTimer   = setInterval(refreshDevice,  OP_DEVICE_POLL_MS);
  }

  function stopOperationalTimers() {
    if (opCardsTimer) { clearInterval(opCardsTimer); opCardsTimer = null; }
    if (opWxTimer)    { clearInterval(opWxTimer);    opWxTimer    = null; }
    if (opDevTimer)   { clearInterval(opDevTimer);   opDevTimer   = null; }
    if (opClockTimer) { clearInterval(opClockTimer); opClockTimer = null; }
    stopCarouselAuto();
    stopTempAlarmPoll();
    stopNetStatusPoll();
  }

  // ----- Carousel initialization & config --------------------------------
  function initCarousel() {
    fetch(KIOSK_CFG_URL, { credentials: 'same-origin', headers: { Accept: 'application/json' }, cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (cfg) {
        carouselInterval = (cfg.interval || 8) * 1000;
        buildCarouselPages(cfg.cards || []);
        setTimeout(startCarouselAuto, 500);
        initSwipe();
        startIdleWatch(cfg.sleep_minutes || 0);
      })
      .catch(function () {
        buildCarouselPages([]);
        setTimeout(startCarouselAuto, 500);
        initSwipe();
      });
  }

  function buildCarouselPages(enabledCards) {
    var track = $('carousel-track');
    if (!track) return;
    var allCards = track.querySelectorAll('.op-card[data-card]');
    var cardMap = {};
    allCards.forEach(function (c) { cardMap[c.getAttribute('data-card')] = c; });

    // Default if config returns empty
    if (!enabledCards || !enabledCards.length) {
      enabledCards = ['aircraft_now','aircraft_today','busiest_hour','last_aircraft',
                      'weather','top_airlines','activity_trend','device_info'];
    }

    // Collect enabled card elements in order
    var enabled = [];
    enabledCards.forEach(function (id) {
      if (cardMap[id]) enabled.push(cardMap[id]);
    });

    // Remove old page containers
    var oldPages = track.querySelectorAll('.carousel-page');
    oldPages.forEach(function (p) { p.remove(); });

    // Pair cards into pages of 2
    for (var i = 0; i < enabled.length; i += 2) {
      var page = document.createElement('div');
      page.className = 'carousel-page';
      page.setAttribute('data-page', 'cards-' + Math.ceil((i + 1) / 2));
      page.appendChild(enabled[i]);
      if (enabled[i + 1]) {
        page.appendChild(enabled[i + 1]);
      } else {
        page.classList.add('full-width');
      }
      track.appendChild(page);
    }

    // Convert to a real Array (not a live NodeList) to prevent stale references
    carouselPages = Array.from(track.querySelectorAll('.carousel-page'));
    carouselPage = 0;
    if (carouselPages.length > 0) {
      goToPage(0, false);
    }
    buildDots();
  }

  function buildDots() {
    var container = $('carousel-dots');
    if (!container) return;
    container.innerHTML = '';
    for (var i = 0; i < carouselPages.length; i++) {
      var dot = document.createElement('span');
      dot.className = 'carousel-dot' + (i === 0 ? ' active' : '');
      dot.setAttribute('data-idx', i);
      dot.addEventListener('click', (function (idx) {
        return function () { goToPage(idx, true); resetCarouselAuto(); };
      })(i));
      container.appendChild(dot);
    }
  }

  function goToPage(idx, animate) {
    if (!carouselPages.length) return;
    if (idx < 0) idx = carouselPages.length - 1;
    if (idx >= carouselPages.length) idx = 0;
    carouselPage = idx;
    var track = $('carousel-track');
    if (!track) return;
    if (animate === false) {
      track.style.transition = 'none';
    } else {
      track.style.transition = '';
    }
    track.style.transform = 'translateX(' + (-idx * 100) + '%)';
    if (animate === false) {
      requestAnimationFrame(function () {
        track.style.transition = '';
      });
    }
    // Update dots
    var dots = ($('carousel-dots') || {}).children || [];
    for (var d = 0; d < dots.length; d++) {
      dots[d].classList.toggle('active', d === idx);
    }
  }

  function currentPageInterval() {
    return carouselInterval;
  }

  function nextPage() {
    if (carouselPages.length < 2) { scheduleNextAuto(); return; }
    try {
      goToPage(carouselPage + 1, true);
    } catch (_e) { /* never break the chain */ }
    scheduleNextAuto();
  }

  function scheduleNextAuto() {
    // Only clear the timer — never kill the heartbeat here.
    if (carouselAutoTimer) { clearTimeout(carouselAutoTimer); carouselAutoTimer = null; }
    carouselAutoTimer = setTimeout(nextPage, carouselInterval);
  }

  var carouselHeartbeat = null;

  function startCarouselAuto() {
    scheduleNextAuto();
    if (!carouselHeartbeat) {
      carouselHeartbeat = setInterval(function () {
        if (visual === 'operational' && !carouselAutoTimer && carouselPages.length > 0) {
          scheduleNextAuto();
        }
      }, 8000);
    }
  }
  function stopCarouselAuto() {
    if (carouselAutoTimer) { clearTimeout(carouselAutoTimer); carouselAutoTimer = null; }
    if (carouselHeartbeat) { clearInterval(carouselHeartbeat); carouselHeartbeat = null; }
  }
  function resetCarouselAuto() {
    scheduleNextAuto();
  }

  // ----- Touch / mouse swipe ---------------------------------------------
  function initSwipe() {
    var el = $('op-carousel');
    if (!el) return;
    el.addEventListener('touchstart', onDragStart, { passive: true });
    el.addEventListener('touchmove', onDragMove, { passive: false });
    el.addEventListener('touchend', onDragEnd, { passive: true });
    el.addEventListener('touchcancel', onDragEnd, { passive: true });
    el.addEventListener('mousedown', onDragStart);
    el.addEventListener('mousemove', onDragMove);
    el.addEventListener('mouseup', onDragEnd);
    el.addEventListener('mouseleave', onDragEnd);
  }

  function clientX(e) {
    if (e.touches && e.touches.length) return e.touches[0].clientX;
    return e.clientX;
  }

  function onDragStart(e) {
    isDragging = true;
    dragStartX = clientX(e);
    dragDx = 0;
    var track = $('carousel-track');
    if (track) track.classList.add('dragging');
    // Pause auto-advance timer but keep the heartbeat alive so recovery works.
    if (carouselAutoTimer) { clearTimeout(carouselAutoTimer); carouselAutoTimer = null; }
  }

  function onDragMove(e) {
    if (!isDragging) return;
    dragDx = clientX(e) - dragStartX;
    var track = $('carousel-track');
    if (!track) return;
    var base = -carouselPage * 100;
    var el = $('op-carousel');
    var w = el ? el.offsetWidth : window.innerWidth;
    var pct = (dragDx / w) * 100;
    track.style.transform = 'translateX(' + (base + pct) + '%)';
    if (e.cancelable) e.preventDefault();
  }

  function onDragEnd() {
    if (!isDragging) return;
    isDragging = false;
    var track = $('carousel-track');
    if (track) track.classList.remove('dragging');
    var threshold = 50;
    if (dragDx < -threshold) {
      goToPage(carouselPage + 1, true);
    } else if (dragDx > threshold) {
      goToPage(carouselPage - 1, true);
    } else {
      goToPage(carouselPage, true);
    }
    resetCarouselAuto();
  }

  // ----- Clock -----------------------------------------------------------
  // HH:MM only (no seconds). Self-healing: if the interval ever dies
  // (browser throttle, background tab, crash), the visibilitychange
  // handler and a 15s watchdog restart it.
  function startClock() {
    var tick = function () {
      try {
        var now = new Date();
        var hh = String(now.getHours()).padStart(2, '0');
        var mm = String(now.getMinutes()).padStart(2, '0');
        setText('op-clock-time', hh + ':' + mm);
        var opts = { weekday: 'short', month: 'short', day: 'numeric' };
        try {
          setText('op-clock-date', now.toLocaleDateString(undefined, opts).toUpperCase());
        } catch (_e) {
          setText('op-clock-date', '');
        }
      } catch (_e) { /* never let the tick die */ }
    };
    tick();
    if (opClockTimer) clearInterval(opClockTimer);
    opClockTimer = setInterval(tick, 1000);
  }

  // Self-healing: restart the clock if the interval dies for any reason.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && visual === 'operational') {
      startClock();
    }
  });
  // Watchdog: every 15s, verify the clock timer is alive.
  setInterval(function () {
    if (visual === 'operational' && !opClockTimer) {
      startClock();
    }
  }, 15000);

  function fmtNumber(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    try { return Number(n).toLocaleString(); } catch (_e) { return String(n); }
  }

  // ----- Data refresh ----------------------------------------------------
  async function refreshCards() {
    try {
      var r = await fetch(CARDS_URL, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!r.ok) return;
      var data = await r.json();

      if (data.now) {
        setText('op-now-value', fmtNumber(data.now.count));
        setText('op-now-sub',   data.now.window || 'last 5 min');
      }
      if (data.today) {
        setText('op-today-value', fmtNumber(data.today.count));
        setText('op-today-sub',   data.today.window || 'today (UTC)');
      }
      if (data.busiest) {
        var hour = data.busiest.hour;
        if (hour === null || hour === undefined || hour === '') {
          setText('op-busy-value', '—');
          setText('op-busy-sub',   'last 24 h');
        } else {
          var h = String(hour).padStart(2, '0') + ':00';
          setText('op-busy-value', h);
          var n = data.busiest.count;
          setText('op-busy-sub', (n !== undefined && n !== null)
            ? fmtNumber(n) + ' aircraft'
            : 'last 24 h');
        }
      }
      if (data.last) {
        var last = data.last;
        var label = (last.callsign && last.callsign.trim())
          || (last.icao && last.icao.toUpperCase())
          || null;
        if (label) {
          setText('op-last-value', label);
          var bits = [];
          if (last.altitude_ft !== null && last.altitude_ft !== undefined) {
            var ft = Number(last.altitude_ft);
            if (!isNaN(ft)) {
              var fl = Math.round(ft / 100);
              bits.push('FL' + String(fl).padStart(3, '0'));
            }
          }
          if (last.speed_kts !== null && last.speed_kts !== undefined) {
            var kts = Number(last.speed_kts);
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
      var r = await fetch(WEATHER_URL, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!r.ok) return;
      var data = await r.json();
      var cur = (data && data.current) || null;
      if (!cur) return;
      if (cur.temp_f !== undefined && cur.temp_f !== null) {
        setText('op-wx-big-temp', Math.round(Number(cur.temp_f)) + '°F');
      }
      if (cur.condition) {
        setText('op-wx-big-cond', String(cur.condition));
      }
    } catch (_e) { /* transient */ }
  }

  async function refreshDevice() {
    try {
      var r = await fetch(DEVICE_URL, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!r.ok) return;
      var data = await r.json();
      if (data && data.radar_url) {
        var clean = String(data.radar_url).replace(/^https?:\/\//i, '');
        setText('op-radar-url', clean);
      }
      if (data && data.device) {
        setText('op-device-id', data.device.device_id || '—');
        var sub = [];
        if (data.device.short_id) sub.push(data.device.short_id);
        if (data.radar_url) sub.push(String(data.radar_url).replace(/^https?:\/\//i, ''));
        setText('op-device-sub', sub.join(' · ') || '—');
      }
    } catch (_e) { /* transient */ }
  }

  async function refreshAirlines() {
    try {
      var r = await fetch(AIRLINES_URL + '?range=24h', {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!r.ok) return;
      var data = await r.json();
      if (Array.isArray(data) && data.length) {
        var lines = data.slice(0, 5).map(function (d) {
          return (d.airline || d.callsign_prefix || '?') + '  ' + fmtNumber(d.count);
        });
        setText('op-airlines-value', lines.join('\n'));
      } else {
        setText('op-airlines-value', '—');
      }
    } catch (_e) { /* transient */ }
  }

  async function refreshTrend() {
    try {
      var canvas = $('op-trend-canvas');
      if (!canvas) return;
      var r = await fetch(TREND_URL + '?range=24h', {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!r.ok) return;
      var data = await r.json();
      if (!Array.isArray(data) || !data.length) return;
      drawSparkline(canvas, data);
    } catch (_e) { /* transient */ }
  }

  function drawSparkline(canvas, data) {
    var ctx = canvas.getContext('2d');
    if (!ctx) return;
    var w = canvas.width = canvas.parentElement.clientWidth || 400;
    var h = canvas.height = canvas.parentElement.clientHeight * 0.55 || 140;
    ctx.clearRect(0, 0, w, h);

    var pts = data.map(function (d) { return d.n || d.count || 0; });
    var max = Math.max.apply(null, pts) || 1;
    var pad = 10;
    var stepX = (w - pad * 2) / Math.max(pts.length - 1, 1);

    // Gradient fill
    var grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, 'rgba(77, 139, 255, 0.35)');
    grad.addColorStop(1, 'rgba(77, 139, 255, 0.02)');

    ctx.beginPath();
    ctx.moveTo(pad, h - pad);
    for (var i = 0; i < pts.length; i++) {
      var x = pad + i * stepX;
      var y = h - pad - ((pts[i] / max) * (h - pad * 2));
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    // Close fill
    ctx.lineTo(pad + (pts.length - 1) * stepX, h - pad);
    ctx.lineTo(pad, h - pad);
    ctx.fillStyle = grad;
    ctx.fill();

    // Stroke line
    ctx.beginPath();
    for (var j = 0; j < pts.length; j++) {
      var x2 = pad + j * stepX;
      var y2 = h - pad - ((pts[j] / max) * (h - pad * 2));
      if (j === 0) ctx.moveTo(x2, y2);
      else ctx.lineTo(x2, y2);
    }
    ctx.strokeStyle = 'rgba(77, 139, 255, 0.9)';
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  // ----- Map (Leaflet, eager — always-visible pane) ----------------------
  function initMapEagerly() {
    if (mapInitialized) return;
    if (typeof L === 'undefined') return;
    var el = $('op-map');
    if (!el) return;
    mapInitialized = true;

    fetch(GPS_URL, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (g) {
        var lat = (g && g.lat) || 44.6;
        var lon = (g && g.lon) || -92.5;
        var zoom = (g && g.map_zoom) || 9;
        carouselMap = L.map(el, { zoomControl: true, attributionControl: false })
          .setView([lat, lon], zoom);
        L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
          maxZoom: 18,
        }).addTo(carouselMap);
        L.circleMarker([lat, lon], {
          radius: 8, fillColor: '#4d8bff', fillOpacity: 0.85,
          color: '#fff', weight: 2,
          className: 'home-pulse',
        }).addTo(carouselMap).bindPopup('Your location');
        setTimeout(function () { carouselMap.invalidateSize(); }, 300);
        refreshMap();
      })
      .catch(function () {});
  }

  async function refreshMap() {
    if (!carouselMap) return;
    try {
      var r = await fetch(POS_URL, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!r.ok) return;
      var aircraft = await r.json();
      if (!Array.isArray(aircraft)) return;

      var seen = {};
      aircraft.forEach(function (ac) {
        if (!ac.lat || !ac.lon) return;
        var key = ac.icao || ac.hex || (ac.lat + ',' + ac.lon);
        seen[key] = true;
        if (carouselMarkers[key]) {
          carouselMarkers[key].setLatLng([ac.lat, ac.lon]);
        } else {
          var icon = L.divIcon({
            className: 'aircraft-marker',
            html: '<svg viewBox="0 0 24 24" width="22" height="22" style="transform:rotate(' +
              (ac.track || 0) + 'deg)"><path d="M12 2L4 20h3l5-6 5 6h3z" fill="#ffae59" stroke="#000" stroke-width="0.5"/></svg>',
            iconSize: [22, 22],
            iconAnchor: [11, 11],
          });
          carouselMarkers[key] = L.marker([ac.lat, ac.lon], { icon: icon })
            .addTo(carouselMap)
            .bindPopup((ac.callsign || ac.icao || '?') + '<br>' +
              (ac.altitude_ft ? ac.altitude_ft + ' ft' : ''));
        }
      });
      // Remove stale
      Object.keys(carouselMarkers).forEach(function (k) {
        if (!seen[k]) {
          carouselMap.removeLayer(carouselMarkers[k]);
          delete carouselMarkers[k];
        }
      });
    } catch (_e) { /* transient */ }
  }

  // ----- Screen dim after idle -----------------------------------------
  var idleTimer = null;
  var idleDimmed = false;
  var idleMinutes = 0;  // 0 = disabled

  function resetIdle() {
    if (idleDimmed) {
      document.body.style.opacity = '';
      idleDimmed = false;
    }
    if (idleMinutes <= 0) return;
    clearTimeout(idleTimer);
    idleTimer = setTimeout(function () {
      document.body.style.opacity = '0.15';
      idleDimmed = true;
    }, idleMinutes * 60 * 1000);
  }

  function startIdleWatch(minutes) {
    idleMinutes = minutes || 0;
    if (idleMinutes <= 0) return;
    ['touchstart', 'touchmove', 'mousemove', 'mousedown', 'keydown'].forEach(function (evt) {
      document.addEventListener(evt, resetIdle, { passive: true });
    });
    resetIdle();
  }

  // ----- Device temp alarm poll ----------------------------------------
  // 3-tier system: green <70°C, amber 70-80°C, red >80°C.
  // The red alarm overlay fires at 80°C (critical). The header chip
  // shows current CPU temp with color coding at all times.
  let tempAlarmTimer = null;
  async function checkTempAlarm() {
    try {
      var r = await fetch(TEMP_ALARM_URL, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!r.ok) return;
      var data = await r.json();

      // Update header CPU temp chip
      var chip = $('op-cpu-temp');
      if (chip && data.cpu_temp_c != null) {
        var temp = data.cpu_temp_c;
        chip.textContent = Math.round(temp) + '°C';
        chip.classList.remove('temp-green', 'temp-amber', 'temp-red');
        if (temp >= 80) {
          chip.classList.add('temp-red');
        } else if (temp >= 70) {
          chip.classList.add('temp-amber');
        } else {
          chip.classList.add('temp-green');
        }
        chip.hidden = false;
      }

      // Critical overlay at threshold
      var overlay = $('temp-alarm-overlay');
      if (!overlay) return;
      if (data.active) {
        var detail = $('temp-alarm-detail');
        if (detail && data.cpu_temp_c != null) {
          detail.textContent = 'CPU temperature: ' + data.cpu_temp_c.toFixed(0) + '°C (limit: ' + (data.threshold_c || 80) + '°C)';
        }
        overlay.hidden = false;
      } else {
        overlay.hidden = true;
      }
    } catch (_e) { /* transient */ }
  }

  function startTempAlarmPoll() {
    if (tempAlarmTimer) return;
    checkTempAlarm();
    tempAlarmTimer = setInterval(checkTempAlarm, 10000);
  }
  function stopTempAlarmPoll() {
    if (tempAlarmTimer) { clearInterval(tempAlarmTimer); tempAlarmTimer = null; }
  }

  // ----- Network status poll (header active connection) ---------------
  let netStatusTimer = null;
  async function checkNetStatus() {
    try {
      var r = await fetch(NET_STATUS_URL, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!r.ok) return;
      var data = await r.json();
      var connEl = $('op-connection');
      var iconEl = $('op-conn-icon');
      var labelEl = $('op-conn-label');
      if (!connEl) return;

      connEl.classList.remove('conn-online', 'conn-offline');
      var primary = data.primary || 'none';
      var online = !!data.internet;

      if (online) {
        connEl.classList.add('conn-online');
        if (primary === 'wifi') {
          var ssid = (data.wifi && data.wifi.ssid) || 'WiFi';
          if (iconEl) iconEl.textContent = '◉';
          if (labelEl) labelEl.textContent = ssid;
        } else if (primary === 'cellular') {
          var tech = (data.cellular && data.cellular.access_tech) || 'LTE';
          if (iconEl) iconEl.textContent = '▲';
          if (labelEl) labelEl.textContent = tech.toUpperCase();
        } else if (primary === 'ethernet') {
          if (iconEl) iconEl.textContent = '⬤';
          if (labelEl) labelEl.textContent = 'Ethernet';
        } else {
          if (iconEl) iconEl.textContent = '◉';
          if (labelEl) labelEl.textContent = 'Online';
        }
      } else {
        connEl.classList.add('conn-offline');
        if (iconEl) iconEl.textContent = '○';
        if (labelEl) labelEl.textContent = 'Offline';
      }
    } catch (_e) { /* transient */ }
  }

  function startNetStatusPoll() {
    if (netStatusTimer) return;
    checkNetStatus();
    netStatusTimer = setInterval(checkNetStatus, 30000);
  }
  function stopNetStatusPoll() {
    if (netStatusTimer) { clearInterval(netStatusTimer); netStatusTimer = null; }
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

  // Phase 2 stabilization — onboarding escape hatch. Wraps
  // /api/onboarding/skip which force-transitions to operational from a
  // whitelisted source. For source="hotspot" the server probes
  // /api/hotspot/health first and records the snapshot in network_logs
  // BEFORE advancing — we don't need to pass any health data from here.
  //
  // Fire-and-forget: the UI transitions on the server's response, and
  // if the request fails we still paint operational locally so the
  // operator is never trapped. The onboarding poll loop will resync on
  // the next tick.
  async function skipOnboarding(source) {
    try {
      const r = await fetch(SKIP_URL, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify({ source: source }),
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok && data && data.state && data.state.stage) {
        applyStage(data.state.stage);
        return data;
      }
    } catch (_e) { /* fall through to local failsafe */ }
    // Server rejected or network died. Paint operational so we don't
    // strand the operator on the escape screen; the next poll tick
    // will reconcile with whatever the server actually persisted.
    applyStage('operational');
    return null;
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

    // Welcome screen buttons (Phase 2 stabilization)
    const welcomeContinue = $('kiosk-welcome-continue');
    const welcomeLater    = $('kiosk-welcome-later');
    if (welcomeContinue) {
      welcomeContinue.addEventListener('click', () => {
        advanceServer('pin_required').catch(() => {});
      });
    }
    if (welcomeLater) {
      welcomeLater.addEventListener('click', () => {
        skipOnboarding('welcome');
      });
    }

    // Setup-offer buttons
    const setupNow  = $('kiosk-offer-now');
    const setupSkip = $('kiosk-offer-skip');
    if (setupNow)  setupNow.addEventListener('click',  () => advanceServer('setup_now_hotspot').catch(() => {}));
    if (setupSkip) setupSkip.addEventListener('click', () => advanceServer('operational').catch(() => {}));

    // PIN keypad escape hatch (Phase 2 stabilization)
    const pinSkip = $('kiosk-pin-skip');
    if (pinSkip) {
      pinSkip.addEventListener('click', () => {
        skipOnboarding('pin');
      });
    }

    // Hotspot Continue button + skip (Phase 2 stabilization)
    const hsDone = $('kiosk-hotspot-done');
    if (hsDone) hsDone.addEventListener('click', () => advanceServer('operational').catch(() => {}));
    const hsSkip = $('kiosk-hotspot-skip');
    if (hsSkip) {
      hsSkip.addEventListener('click', () => {
        // source=hotspot tells the server to capture the live
        // /api/hotspot/health snapshot into network_logs before the
        // state flips to operational.
        skipOnboarding('hotspot');
      });
    }

    // Resume safety (guard #1): on a fresh page load pinFirst is
    // definitionally empty, so a persisted server stage of pin_confirm
    // is always orphaned. Rewind it to pin_required before the first
    // paint, otherwise the operator sees a flash of "Confirm Admin PIN"
    // before guard #2 (inside showPinPad) catches it on the next tick.
    // Uses /advance (pin_confirm → pin_required is allowed) rather
    // than /reset, because /reset is now the admin-gated factory
    // reset and rejects pre-auth callers.
    if (serverStage === 'pin_confirm') {
      serverStage = 'pin_required';
      fetch(ADVANCE_URL, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ to: 'pin_required' }),
      }).catch(() => {});
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
