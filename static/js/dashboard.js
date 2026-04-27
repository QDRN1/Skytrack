/* Dashboard: cards, recent list, top airlines/routes, trend chart, search,
 * weather, and the live aircraft map. Also wires up the Live/1h/6h/24h/7d
 * filter chips that drive every range-aware widget at once.
 */
(function () {
  function altColor(alt) {
    if (!alt || alt <= 0) return '#888888';
    if (alt < 5000)  return '#4ade80';
    if (alt < 15000) return '#facc15';
    if (alt < 30000) return '#fb923c';
    if (alt < 40000) return '#f87171';
    return '#c084fc';
  }

  const state = { range: '24h', map: null, markers: {}, trails: {}, trendChart: null };

  document.addEventListener('DOMContentLoaded', () => {
    bindFilter();
    bindSearch();
    initMap();
    refreshAll();
    setInterval(refreshAll, 15000);
  });

  function bindFilter() {
    document.querySelectorAll('#range-filter .chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        document.querySelectorAll('#range-filter .chip').forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        state.range = chip.dataset.range;
        refreshRangeWidgets();
      });
    });
  }

  function bindSearch() {
    const form = document.getElementById('search-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const q = document.getElementById('search-input').value.trim();
      if (!q) return;
      const rows = await window.api.get(`/api/dashboard/search?q=${encodeURIComponent(q)}&range=${state.range}`);
      renderRecent(rows);
    });
  }

  async function refreshAll() {
    const [cards, recent, weather, positions, trails] = await Promise.allSettled([
      window.api.get('/api/dashboard/cards'),
      window.api.get('/api/dashboard/recent'),
      window.api.get('/api/dashboard/weather'),
      window.api.get('/api/dashboard/positions'),
      window.api.get('/api/dashboard/trails'),
    ]);
    if (cards.status === 'fulfilled')     renderCards(cards.value);
    if (recent.status === 'fulfilled')    renderRecent(recent.value);
    if (weather.status === 'fulfilled')   renderWeather(weather.value);
    if (positions.status === 'fulfilled') renderMap(positions.value);
    if (trails.status === 'fulfilled')    renderTrails(trails.value);
    refreshRangeWidgets();
  }

  async function refreshRangeWidgets() {
    const [airlines, routes, trend] = await Promise.allSettled([
      window.api.get(`/api/dashboard/airlines?range=${state.range}`),
      window.api.get(`/api/dashboard/routes?range=${state.range}`),
      window.api.get(`/api/dashboard/trend?range=${state.range}`),
    ]);
    if (airlines.status === 'fulfilled') renderAirlines(airlines.value);
    if (routes.status === 'fulfilled')   renderRoutes(routes.value);
    if (trend.status === 'fulfilled')    renderTrend(trend.value);
  }

  // -------- Renderers --------

  function renderCards(c) {
    set('#card-now-value', c.now.count);
    set('#card-today-value', c.today.count);
    set('#card-busy-value', c.busiest.count + (c.busiest.hour != null ? ` @${pad(c.busiest.hour)}h UTC` : ''));
    if (c.last) {
      set('#card-last-value', c.last.callsign || c.last.icao);
      set('#card-last-sub', `${c.last.altitude_ft || '?'} ft • ${c.last.speed_kts || '?'} kts`);
    } else {
      set('#card-last-value', '—');
      set('#card-last-sub', 'no aircraft in last 5 min');
    }
  }

  function renderRecent(rows) {
    const tbody = document.getElementById('recent-tbody');
    if (!tbody) return;
    tbody.innerHTML = rows.map(r => `
      <tr>
        <td>${escape(r.icao)}</td>
        <td>${escape(r.callsign || '—')}</td>
        <td>${r.altitude_ft || '—'}</td>
        <td>${r.speed_kts || '—'}</td>
        <td>${escape(r.last_seen || '')}</td>
      </tr>
    `).join('');
  }

  function renderAirlines(rows) {
    const list = document.getElementById('airlines-list');
    if (!list) return;
    list.innerHTML = rows.map(r =>
      `<li><span>${escape(r.airline)}</span><span>${r.n}</span></li>`).join('') ||
      '<li class="muted">no data</li>';
  }

  function renderRoutes(rows) {
    const list = document.getElementById('routes-list');
    if (!list) return;
    list.innerHTML = rows.map(r =>
      `<li><span>${escape(r.origin)} → ${escape(r.destination)}</span><span>${r.n}</span></li>`).join('') ||
      '<li class="muted">no data</li>';
  }

  // Map a condition string to a small emoji so the card has a visual
  // without pulling in an icon font. Keep the list short + case-insensitive.
  function wxEmoji(cond) {
    const c = (cond || '').toLowerCase();
    if (!c) return '·';
    if (c.includes('thunder')) return '⛈';
    if (c.includes('snow') || c.includes('sleet') || c.includes('flurr')) return '❄';
    if (c.includes('rain') || c.includes('drizzle') || c.includes('shower')) return '🌧';
    if (c.includes('fog') || c.includes('mist') || c.includes('haze')) return '🌫';
    if (c.includes('cloud') && c.includes('part')) return '⛅';
    if (c.includes('cloud') || c.includes('overcast')) return '☁';
    if (c.includes('clear') || c.includes('sun')) return '☀';
    return '·';
  }

  function renderWeather(w) {
    if (!w || !w.current) {
      set('#wx-temp', '—');
      set('#wx-cond', 'Weather unavailable');
      const fc = document.getElementById('wx-forecast');
      if (fc) fc.innerHTML = '';
      return;
    }
    const temp = w.current.temp_f;
    set('#wx-temp', temp != null ? `${Math.round(temp)}°` : '—');
    const cond = w.current.condition || '—';
    set('#wx-cond', `${wxEmoji(cond)}  ${cond}`);
    const hi = w.current.high_f, lo = w.current.low_f;
    const hilo = document.getElementById('wx-hilo');
    if (hilo) {
      hilo.textContent = (hi != null && lo != null)
        ? `Hi ${Math.round(hi)}°  ·  Lo ${Math.round(lo)}°`
        : '';
    }
    const fc = document.getElementById('wx-forecast');
    if (fc) {
      fc.innerHTML = (w.forecast || []).map(d => {
        const dh = (d.high_f != null) ? `${Math.round(d.high_f)}°` : '—';
        const dl = (d.low_f  != null) ? `${Math.round(d.low_f)}°`  : '—';
        return `
          <div class="wx-day" title="${escape(d.condition || '')}">
            <div class="wx-day-name">${escape(d.day)}</div>
            <div class="wx-day-icon">${wxEmoji(d.condition)}</div>
            <div class="wx-day-hilo">${dh} / ${dl}</div>
          </div>
        `;
      }).join('');
    }
    const stamp = document.getElementById('wx-stamp');
    if (stamp) {
      const parts = [];
      if (w.provider && w.provider !== 'mock' && w.provider !== 'off') parts.push(w.provider);
      if (w.cached) parts.push('cached');
      if (w.offline) parts.push('offline');
      if (w.mock) parts.push('sample data');
      if (w.provider === 'off') parts.push('disabled');
      if (w.last_update) {
        try {
          parts.push('updated ' + new Date(w.last_update).toLocaleTimeString([],
            { hour: '2-digit', minute: '2-digit' }));
        } catch (_) { /* leave off */ }
      }
      stamp.textContent = parts.join(' · ');
    }
  }

  function renderTrend(data) {
    if (!window.Chart) return;
    const canvas = document.getElementById('trend-chart');
    if (!canvas) return;
    const labels = data.points.map(p => new Date(p.t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }));
    const counts = data.points.map(p => p.n);
    if (state.trendChart) {
      state.trendChart.data.labels = labels;
      state.trendChart.data.datasets[0].data = counts;
      state.trendChart.update('none');
      return;
    }
    state.trendChart = new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: 'Aircraft',
          data: counts,
          borderColor: '#4d8bff',
          backgroundColor: 'rgba(77, 139, 255, 0.15)',
          fill: true,
          tension: 0.3,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        resizeDelay: 150,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } },
          y: { beginAtZero: true, ticks: { precision: 0 } },
        },
      },
    });
  }

  function initMap() {
    const el = document.getElementById('aircraft-map');
    if (!el || !window.L) return;
    state.map = L.map(el).setView(
      [parseFloat(el.dataset.lat) || 0, parseFloat(el.dataset.lon) || 0],
      parseInt(el.dataset.zoom) || 8,
    );
    const tileUrl = el.dataset.tileUrl || 'https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png';
    L.tileLayer(tileUrl, { maxZoom: 18 }).addTo(state.map);
  }

  function _aircraftIcon(track, alt) {
    var color = altColor(alt);
    return L.divIcon({
      className: 'aircraft-marker',
      html: '<svg viewBox="0 0 24 24" width="20" height="20" style="transform:rotate(' +
        (track || 0) + 'deg)"><path d="M12 2L4 20h3l5-6 5 6h3z" fill="' + color + '" stroke="#000" stroke-width="0.5"/></svg>',
      iconSize: [20, 20],
      iconAnchor: [10, 10],
    });
  }

  function renderMap(positions) {
    if (!state.map || !window.L) return;
    const seen = new Set();
    positions.forEach((p) => {
      if (p.lat == null || p.lon == null) return;
      seen.add(p.icao);
      const label = `${escape(p.callsign || p.icao)}<br>${p.altitude_ft || '?'} ft`;
      if (state.markers[p.icao]) {
        state.markers[p.icao].setLatLng([p.lat, p.lon])
          .setIcon(_aircraftIcon(p.track, p.altitude_ft))
          .bindPopup(label);
      } else {
        state.markers[p.icao] = L.marker([p.lat, p.lon], { icon: _aircraftIcon(p.track, p.altitude_ft) })
          .addTo(state.map).bindPopup(label);
      }
    });
    Object.keys(state.markers).forEach((k) => {
      if (!seen.has(k)) {
        state.map.removeLayer(state.markers[k]);
        delete state.markers[k];
      }
    });
  }

  function renderTrails(data) {
    if (!state.map || !window.L) return;
    const seen = new Set();
    Object.keys(data).forEach((icao) => {
      const t = data[icao];
      const pts = t.points || t;
      if (pts.length < 2) return;
      seen.add(icao);
      const color = altColor(t.alt);
      if (state.trails[icao]) {
        state.trails[icao].setLatLngs(pts).setStyle({ color });
      } else {
        state.trails[icao] = L.polyline(pts, {
          color, weight: 2, opacity: 0.45, dashArray: '4 6',
        }).addTo(state.map);
      }
    });
    Object.keys(state.trails).forEach((k) => {
      if (!seen.has(k)) {
        state.map.removeLayer(state.trails[k]);
        delete state.trails[k];
      }
    });
  }

  // -------- helpers --------
  function set(sel, v) { const el = document.querySelector(sel); if (el) el.textContent = v; }
  function pad(n) { return String(n).padStart(2, '0'); }
  function escape(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
})();
