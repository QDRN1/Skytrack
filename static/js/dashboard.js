/* =======================================================================
   SkyTrack Dashboard — Client-side JS
   WebSocket, Leaflet map, burn-in prevention, stats rotation
   ======================================================================= */

(function () {
  'use strict';

  const CFG = window.SKYTRACK_CONFIG || {};
  const CENTER = [CFG.latitude || 44.602016, CFG.longitude || -92.494604];
  const ZOOM = CFG.mapZoom || 8;

  // -----------------------------------------------------------------------
  // Clock
  // -----------------------------------------------------------------------
  function updateClock() {
    const el = document.getElementById('clock');
    if (!el) return;
    const now = new Date();
    el.textContent = now.toLocaleTimeString('en-US', {
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true,
    });
  }
  setInterval(updateClock, 1000);
  updateClock();

  // -----------------------------------------------------------------------
  // WebSocket
  // -----------------------------------------------------------------------
  const socket = io({ transports: ['websocket', 'polling'] });

  socket.on('connect', function () {
    console.log('[SkyTrack] WebSocket connected');
  });

  socket.on('disconnect', function () {
    console.warn('[SkyTrack] WebSocket disconnected');
  });

  // -----------------------------------------------------------------------
  // Weather
  // -----------------------------------------------------------------------
  const WEATHER_ICONS = {
    'Sunny': '☀️', 'Clear': '🌙', 'Partly Cloudy': '⛅', 'Partly cloudy': '⛅',
    'Cloudy': '☁️', 'Overcast': '☁️', 'Mist': '🌫️', 'Fog': '🌫️',
    'Light Rain': '🌦️', 'Rain': '🌧️', 'Heavy Rain': '🌧️',
    'Light Snow': '🌨️', 'Snow': '❄️', 'Heavy Snow': '❄️',
    'Thunderstorm': '⛈️', 'Blizzard': '🌨️', 'Freezing Rain': '🧊',
  };

  function getWeatherSymbol(condition, iconUrl) {
    // If we have a real icon URL from WeatherAPI, use it
    if (iconUrl && iconUrl.startsWith('//')) {
      return '<img src="https:' + iconUrl + '" alt="' + condition + '">';
    }
    // Fallback to emoji
    return WEATHER_ICONS[condition] || '🌡️';
  }

  socket.on('weather_update', function (data) {
    if (!data || !data.current) return;

    const c = data.current;

    // Icon
    const iconEl = document.getElementById('weather-icon');
    iconEl.innerHTML = getWeatherSymbol(c.condition, c.icon);

    // Temperature
    document.getElementById('weather-temp').innerHTML =
      Math.round(c.temp_f) + '&deg;F';

    // Condition
    document.getElementById('weather-condition').textContent = c.condition;

    // Hi / Lo
    const hiloEl = document.getElementById('weather-hilo');
    hiloEl.querySelector('.weather-hi').innerHTML =
      'H: ' + Math.round(c.high_f) + '&deg;';
    hiloEl.querySelector('.weather-lo').innerHTML =
      'L: ' + Math.round(c.low_f) + '&deg;';

    // Forecast
    const fcEl = document.getElementById('weather-forecast');
    fcEl.innerHTML = '';
    if (data.forecast) {
      data.forecast.forEach(function (day) {
        const div = document.createElement('div');
        div.className = 'forecast-day';
        div.innerHTML =
          '<span class="fc-label">' + day.day + '</span>' +
          '<span class="fc-icon">' + getWeatherSymbol(day.condition, day.icon) + '</span>' +
          '<span class="fc-temps">' +
          '<span class="fc-hi">' + Math.round(day.high_f) + '&deg;</span>' +
          '<span class="fc-lo">' + Math.round(day.low_f) + '&deg;</span>' +
          '</span>';
        fcEl.appendChild(div);
      });
    }

    // Status badge
    const statusEl = document.getElementById('weather-status');
    if (data.mock) {
      statusEl.textContent = 'DEMO DATA';
    } else if (data.offline) {
      statusEl.textContent = 'OFFLINE — cached ' + (data.last_update || '');
    } else if (data.cached) {
      statusEl.textContent = 'cached';
    } else {
      statusEl.textContent = '';
    }
  });

  // -----------------------------------------------------------------------
  // Leaflet Map
  // -----------------------------------------------------------------------
  var map = L.map('map', {
    center: CENTER,
    zoom: ZOOM,
    zoomControl: false,
    attributionControl: true,
  });

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: '&copy; OpenStreetMap',
  }).addTo(map);

  // Optional: add a subtle radius circle (60 mi ≈ 96.6 km)
  L.circle(CENTER, {
    radius: 96560,  // 60 miles in meters
    color: '#002D72',
    fillColor: '#002D72',
    fillOpacity: 0.03,
    weight: 1,
    opacity: 0.25,
    dashArray: '6 4',
  }).addTo(map);

  // Center marker
  L.circleMarker(CENTER, {
    radius: 5,
    color: '#002D72',
    fillColor: '#A3C940',
    fillOpacity: 1,
    weight: 2,
  }).addTo(map).bindTooltip('QDRNode SkyTrack', { permanent: false });

  // Aircraft markers layer
  var planeMarkers = {};

  function createPlaneIcon(track, hex) {
    // Deterministic color based on hex
    var hue = (parseInt(hex.replace(/[^0-9a-f]/gi, '').slice(0, 4), 16) % 360);
    var color = 'hsl(' + hue + ', 70%, 45%)';

    return L.divIcon({
      className: 'plane-icon',
      html: '<svg width="24" height="24" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" ' +
        'style="transform: rotate(' + (track || 0) + 'deg);">' +
        '<path d="M12 2L8 10H3l2 4h5l-2 6h2l4-4 4 4h2l-2-6h5l2-4h-5L12 2z" ' +
        'fill="' + color + '" stroke="#fff" stroke-width="0.5"/></svg>',
      iconSize: [24, 24],
      iconAnchor: [12, 12],
    });
  }

  function updateAircraftOnMap(aircraft) {
    var seen = {};

    aircraft.forEach(function (ac) {
      if (!ac.lat || !ac.lon) return;
      seen[ac.hex] = true;

      if (planeMarkers[ac.hex]) {
        // Update existing marker
        planeMarkers[ac.hex].setLatLng([ac.lat, ac.lon]);
        planeMarkers[ac.hex].setIcon(createPlaneIcon(ac.track, ac.hex));
      } else {
        // New marker
        var marker = L.marker([ac.lat, ac.lon], {
          icon: createPlaneIcon(ac.track, ac.hex),
        }).addTo(map);

        var label = ac.flight ? ac.flight : ac.hex;
        marker.bindTooltip(label + '<br>' +
          (ac.altitude ? ac.altitude.toLocaleString() + ' ft' : '') +
          (ac.speed ? ' | ' + ac.speed + ' kts' : ''), {
          direction: 'top', offset: [0, -12],
        });

        planeMarkers[ac.hex] = marker;
      }
    });

    // Remove stale markers
    Object.keys(planeMarkers).forEach(function (hex) {
      if (!seen[hex]) {
        map.removeLayer(planeMarkers[hex]);
        delete planeMarkers[hex];
      }
    });
  }

  // -----------------------------------------------------------------------
  // Aircraft data + Stats
  // -----------------------------------------------------------------------
  var latestAircraftData = null;
  var freqMode = 'today';  // alternates: 'today' | 'alltime'

  socket.on('aircraft_update', function (data) {
    if (!data) return;
    latestAircraftData = data;

    // Update map
    updateAircraftOnMap(data.aircraft || []);

    // Update total
    document.getElementById('total-count').textContent =
      (data.total_today || 0).toLocaleString();

    // Update frequent plane (current mode)
    updateFrequentTile();
  });

  function updateFrequentTile() {
    if (!latestAircraftData) return;

    var info, label;
    if (freqMode === 'today') {
      info = latestAircraftData.most_frequent_today;
      label = 'Most Frequent Plane Today';
    } else {
      info = latestAircraftData.most_frequent_alltime;
      label = 'Most Frequent Plane All-Time';
    }

    document.getElementById('freq-label').textContent = label;

    if (info) {
      document.getElementById('freq-flight').textContent =
        info.flight || info.hex;
      document.getElementById('freq-detail').textContent =
        info.messages.toLocaleString() + ' messages';
    } else {
      document.getElementById('freq-flight').textContent = '--';
      document.getElementById('freq-detail').textContent = 'no data';
    }
  }

  // Alternate most-frequent tile every 15 seconds
  setInterval(function () {
    freqMode = freqMode === 'today' ? 'alltime' : 'today';
    updateFrequentTile();
  }, 15000);

  // -----------------------------------------------------------------------
  // Network / Connectivity indicators
  // -----------------------------------------------------------------------
  socket.on('health_update', function (data) {
    if (!data || !data.network) return;

    var wifiEl = document.getElementById('wifi-indicator');
    var cellEl = document.getElementById('cell-indicator');

    wifiEl.classList.toggle('active', !!data.network.wifi);
    cellEl.classList.toggle('active', !!data.network.cellular);
  });

  // -----------------------------------------------------------------------
  // Boot Splash Video
  // -----------------------------------------------------------------------
  (function () {
    var bootOverlay = document.getElementById('boot-overlay');
    var bootVideo = document.getElementById('boot-video');
    if (!bootOverlay || !bootVideo) return;

    function dismissBoot() {
      bootOverlay.classList.remove('visible');
      bootOverlay.classList.add('hidden');
      // Clean up after fade-out transition
      setTimeout(function () {
        bootVideo.pause();
        bootVideo.removeAttribute('src');
        bootVideo.load();
        map.invalidateSize();
      }, 900);
    }

    bootVideo.addEventListener('ended', dismissBoot);

    // Fallback: if video fails to load or play, dismiss after 2s
    bootVideo.addEventListener('error', function () {
      setTimeout(dismissBoot, 500);
    });

    // Auto-play the boot video
    var playPromise = bootVideo.play();
    if (playPromise !== undefined) {
      playPromise.catch(function () {
        // Autoplay blocked (unlikely since muted), dismiss splash
        setTimeout(dismissBoot, 500);
      });
    }

    // Safety net: dismiss after 30s no matter what
    setTimeout(dismissBoot, 30000);
  })();

  // -----------------------------------------------------------------------
  // Burn-in Prevention
  // -----------------------------------------------------------------------

  // 1) Micro-jitter: shift main content by a few pixels every 5 minutes
  setInterval(function () {
    var jX = Math.round((Math.random() - 0.5) * 6);  // -3 to +3
    var jY = Math.round((Math.random() - 0.5) * 6);
    var main = document.getElementById('main-content');
    if (main) {
      main.style.transform = 'translate(' + jX + 'px, ' + jY + 'px)';
    }
  }, CFG.jitterInterval || 300000);

  // 2) Full-screen refresh videos every 60 minutes
  //    Plays Green Plane video, then Blue Plane video, then returns to dashboard
  var refreshVideos = [
    CFG.videoRefreshGreen || '/static/video/SkyTrack%20Refresh%20-%20Green%20Plane-V1.mp4',
    CFG.videoRefreshBlue  || '/static/video/SkyTrack%20Refresh%20-%20Blue%20Plane-V1.mp4',
  ];

  function showRefreshScene() {
    var overlay = document.getElementById('refresh-overlay');
    var video = document.getElementById('refresh-video');
    if (!overlay || !video) return;

    var index = 0;

    function playNext() {
      if (index >= refreshVideos.length) {
        // All scenes done — hide overlay, return to dashboard
        overlay.classList.remove('visible');
        overlay.classList.add('hidden');
        video.pause();
        video.removeAttribute('src');
        video.load();
        setTimeout(function () { map.invalidateSize(); }, 900);
        return;
      }
      video.src = refreshVideos[index];
      video.load();
      video.play().catch(function () { playNext(); });
      index++;
    }

    video.onended = playNext;
    video.onerror = playNext;

    // Show overlay and start first video
    overlay.classList.remove('hidden');
    overlay.classList.add('visible');
    playNext();

    // Safety net: hide after 60s no matter what
    setTimeout(function () {
      if (overlay.classList.contains('visible')) {
        overlay.classList.remove('visible');
        overlay.classList.add('hidden');
        video.pause();
        map.invalidateSize();
      }
    }, 60000);
  }

  setInterval(showRefreshScene, CFG.refreshInterval || 3600000);

  // -----------------------------------------------------------------------
  // Window resize: keep Leaflet map happy
  // -----------------------------------------------------------------------
  window.addEventListener('resize', function () {
    map.invalidateSize();
  });

  // Initial map size fix after DOM paints
  setTimeout(function () { map.invalidateSize(); }, 200);

})();
