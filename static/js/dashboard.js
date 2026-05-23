// Dashboard wiring — polls /api/current-load/ and /api/chart-data/ on intervals
// and updates the digital readout, progress bar, device mini-cards, and chart.
(function () {
  'use strict';

  const POLL_INTERVAL = 5000;
  const CHART_POLL_INTERVAL = 10000;
  const COOLDOWN_TICK = 1000;
  const EVENTS_LIMIT = 20;

  // DOM refs
  const stateEl = document.getElementById('load-stat');
  const currentEl = document.getElementById('load-current');
  const limitEl = document.getElementById('load-limit');
  const cooldownDisplay = document.getElementById('cooldown-display');
  const progressEl = document.getElementById('load-progress');
  const modeBadge = document.getElementById('mode-badge');
  const globalStatus = document.getElementById('global-status');
  const deviceGrid = document.getElementById('device-grid');
  const chartCanvas = document.getElementById('power-chart');
  const chartEmpty = document.getElementById('chart-empty');
  const chartToolbar = document.getElementById('chart-toolbar');
  const eventsList = document.getElementById('events-list');
  const eventsSubtitle = document.getElementById('events-subtitle');

  // Local state
  let chart = null;
  let chartMinutes = 30;
  let lastSnapshot = null;
  let loadTimer = null;
  let chartTimer = null;
  let cooldownTimer = null;
  let lastSeenEventId = null;

  // Utilities
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  }
  function setStatus(state, text) {
    if (!globalStatus) return;
    globalStatus.dataset.status = state;
    const textEl = globalStatus.querySelector('.status-text');
    if (textEl) textEl.textContent = text;
  }

  // Load + devices
  function applyLoad(data) {
    const total = data.total_power_watts || 0;
    const limit = data.power_limit_watts || 0;
    const pct = limit > 0 ? (total / limit) * 100 : 0;

    currentEl.textContent = Math.round(total).toLocaleString('uk-UA');
    limitEl.textContent = fmtWatts(limit);
    cooldownDisplay.textContent = (data.restore_cooldown_seconds || 0) + ' с';

    progressEl.style.width = Math.min(100, pct) + '%';
    progressEl.classList.toggle('is-warn', pct >= 75 && pct < 95 && !data.is_overloaded);
    progressEl.classList.toggle('is-danger', pct >= 95 || data.is_overloaded);
    stateEl.classList.toggle('is-overloaded', !!data.is_overloaded);

    if (modeBadge) {
      const mode = data.restore_mode === 'MANUAL' ? 'MANUAL' : 'AUTO';
      modeBadge.dataset.mode = mode;
      modeBadge.textContent = mode;
    }

    if (!data.is_active) {
      setStatus('paused', `Алгоритм на паузі · ${fmtWatts(total)} / ${fmtWatts(limit)}`);
    } else if (data.is_overloaded) {
      setStatus('overload', `Перевантаження · ${fmtWatts(total)} / ${fmtWatts(limit)}`);
    } else {
      setStatus('ok', `OK · ${fmtWatts(total)} / ${fmtWatts(limit)}`);
    }
  }

  function renderDevices(devices) {
    if (!devices || !devices.length) {
      deviceGrid.innerHTML = '<div class="empty-text">Немає зареєстрованих приладів. Додайте через сторінку <a href="/devices/">Прилади</a>.</div>';
      return;
    }
    const html = devices.map(d => {
      const isOn = d.is_on;
      const isShed = !!d.shed_at && !isOn;
      const cls = ['device-mini'];
      if (isOn) cls.push('is-on');
      else if (isShed) cls.push('is-shed');
      const badge = isOn
        ? '<span class="badge badge-success"><span class="dot"></span>ON</span>'
        : (isShed
            ? '<span class="badge badge-warning"><span class="dot"></span>SHED</span>'
            : '<span class="badge badge-muted"><span class="dot"></span>OFF</span>');
      const cooldown = isShed
        ? `<div class="device-mini-cooldown" data-shed-at="${d.shed_at}">…</div>`
        : '';
      const last = d.last_seen_at ? fmtAgo(d.last_seen_at) : 'немає даних';
      return `
        <div class="${cls.join(' ')}">
          <div class="device-mini-head">
            <span class="device-mini-name" title="${escapeHtml(d.device_id)}">${escapeHtml(d.name)}</span>
            ${badge}
          </div>
          <div class="device-mini-power">${fmtWatts(d.last_power_watts || 0)}</div>
          <div class="device-mini-meta">
            <span>Пріоритет ${d.priority}</span>
            <span>${last}</span>
          </div>
          ${cooldown}
        </div>
      `;
    }).join('');
    deviceGrid.innerHTML = html;
  }

  function tickCooldowns() {
    if (!lastSnapshot) return;
    const cooldownSec = lastSnapshot.restore_cooldown_seconds || 0;
    const mode = lastSnapshot.restore_mode;
    const now = Date.now();
    deviceGrid.querySelectorAll('.device-mini-cooldown').forEach(el => {
      const shedAt = Date.parse(el.dataset.shedAt);
      if (isNaN(shedAt)) { el.textContent = '—'; return; }
      if (mode === 'MANUAL') {
        el.textContent = 'Manual: чекає на toggle';
        return;
      }
      const elapsed = Math.floor((now - shedAt) / 1000);
      const remaining = cooldownSec - elapsed;
      el.textContent = remaining > 0
        ? `Cooldown · ${remaining} с до restore`
        : 'Готово до restore';
    });
    if (eventsList) {
      eventsList.querySelectorAll('.event-time').forEach(el => {
        const ts = el.getAttribute('datetime');
        if (ts) el.textContent = fmtAgo(ts);
      });
    }
    if (lastSnapshot.last_overload_at) {
      updateEventsSubtitle(lastSnapshot.last_overload_at);
    }
  }

  async function pollLoad() {
    try {
      const data = await apiFetch('/api/current-load/', { toastOnError: false });
      lastSnapshot = data;
      applyLoad(data);
      renderDevices(data.devices);
      updateEventsSubtitle(data.last_overload_at);
      tickCooldowns();
    } catch (e) {
      setStatus('error', 'Зв\'язок з сервером втрачено');
    }
  }

  // Balancing events
  function updateEventsSubtitle(lastOverloadAt) {
    if (!eventsSubtitle) return;
    eventsSubtitle.textContent = lastOverloadAt
      ? 'Останнє перевантаження: ' + fmtAgo(lastOverloadAt)
      : 'Останнє перевантаження: —';
  }

  function renderEvents(events) {
    if (!eventsList) return;
    if (!events || !events.length) {
      eventsList.innerHTML = '<div class="empty-text">Подій балансування ще не було.</div>';
      return;
    }
    const html = events.map(ev => {
      const isShed = ev.action === 'SHED';
      const cls = isShed ? 'event-row event-shed' : 'event-row event-restore';
      const iconChar = isShed ? '↓' : '↑';
      const verb = isShed ? 'Відключено' : 'Повернуто';
      const name = escapeHtml(ev.device_name || ev.device_public_id || ('#' + ev.device));
      const meta = `${fmtWatts(ev.device_power_watts || 0)} · ліміт ${fmtWatts(ev.power_limit_watts || 0)}`;
      return `
        <div class="${cls}" data-event-id="${ev.id}">
          <span class="event-icon-wrap" aria-hidden="true">${iconChar}</span>
          <div class="event-body">
            <div class="event-line">${verb} <strong>${name}</strong></div>
            <div class="event-meta">${meta}</div>
          </div>
          <time class="event-time" datetime="${escapeHtml(ev.occurred_at)}">${fmtAgo(ev.occurred_at)}</time>
        </div>
      `;
    }).join('');
    eventsList.innerHTML = html;
  }

  async function pollEvents() {
    try {
      const events = await apiFetch('/api/balancing-events/?limit=' + EVENTS_LIMIT, { toastOnError: false });
      if (!Array.isArray(events)) return;

      if (lastSeenEventId === null) {
        lastSeenEventId = events.length ? Math.max.apply(null, events.map(e => e.id)) : 0;
      } else {
        const fresh = events
          .filter(e => e.id > lastSeenEventId && e.action === 'SHED')
          .sort((a, b) => a.id - b.id);
        for (const ev of fresh) {
          const name = ev.device_name || ev.device_public_id || ('#' + ev.device);
          showToast(`Перевантаження! Відключено ${name}`, 'warning');
        }
        if (events.length) {
          lastSeenEventId = Math.max(lastSeenEventId, Math.max.apply(null, events.map(e => e.id)));
        }
      }
      renderEvents(events);
    } catch (e) {
    }
  }

  //  Chart
  function chartTheme() {
    return {
      accent: cssVar('--color-accent'),
      accentSoft: cssVar('--color-accent-soft'),
      danger: cssVar('--color-danger'),
      grid: cssVar('--color-border'),
      text: cssVar('--color-text-muted'),
      surface: cssVar('--color-surface-raised'),
      textStrong: cssVar('--color-text'),
    };
  }

  function ensureChart() {
    if (chart || !window.Chart) return;
    const t = chartTheme();
    chart = new Chart(chartCanvas.getContext('2d'), {
      type: 'line',
      data: {
        labels: [],
        datasets: [
          {
            label: 'Споживання',
            data: [],
            borderColor: t.accent,
            backgroundColor: t.accentSoft,
            borderWidth: 2,
            fill: true,
            tension: 0.35,
            pointRadius: 0,
            pointHoverRadius: 4,
          },
          {
            label: 'Ліміт',
            data: [],
            borderColor: t.danger,
            borderDash: [6, 6],
            borderWidth: 1.5,
            fill: false,
            tension: 0,
            pointRadius: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 220 },
        interaction: { mode: 'index', intersect: false },
        scales: {
          y: {
            beginAtZero: true,
            ticks: {
              color: t.text,
              callback: function (val) {
                return val >= 1000 ? (val / 1000).toFixed(1) + ' кВт' : val + ' Вт';
              },
            },
            grid: { color: t.grid },
            border: { display: false },
          },
          x: {
            ticks: { color: t.text, maxRotation: 0, autoSkipPadding: 18 },
            grid: { display: false },
            border: { color: t.grid },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: t.surface,
            titleColor: t.textStrong,
            bodyColor: t.textStrong,
            borderColor: cssVar('--color-border'),
            borderWidth: 1,
            displayColors: false,
            padding: 10,
            callbacks: {
              label: function (ctx) {
                return ctx.dataset.label + ': ' + fmtWatts(ctx.parsed.y);
              },
            },
          },
        },
      },
    });
  }

  function applyChartTheme() {
    if (!chart) return;
    const t = chartTheme();
    chart.data.datasets[0].borderColor = t.accent;
    chart.data.datasets[0].backgroundColor = t.accentSoft;
    chart.data.datasets[1].borderColor = t.danger;
    chart.options.scales.y.grid.color = t.grid;
    chart.options.scales.y.ticks.color = t.text;
    chart.options.scales.x.ticks.color = t.text;
    chart.options.scales.x.border.color = t.grid;
    const tip = chart.options.plugins.tooltip;
    tip.backgroundColor = t.surface;
    tip.titleColor = t.textStrong;
    tip.bodyColor = t.textStrong;
    tip.borderColor = cssVar('--color-border');
  }

  function formatLabel(ts) {
    const d = new Date(ts);
    return d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit', hour12: false });
  }

  async function pollChart() {
    ensureChart();
    if (!chart) return;
    try {
      const data = await apiFetch(`/api/chart-data/?minutes=${chartMinutes}`, { toastOnError: false });
      const labels = data.map(p => formatLabel(p.timestamp));
      const values = data.map(p => Math.round((p.total_power_watts || 0) * 10) / 10);
      const limit = lastSnapshot ? lastSnapshot.power_limit_watts : 0;
      chart.data.labels = labels;
      chart.data.datasets[0].data = values;
      chart.data.datasets[1].data = labels.map(() => limit);
      chart.update();
      chartEmpty.classList.toggle('hidden', data.length > 0);
    } catch (e) {
    }
  }

  // Range selector
  chartToolbar.addEventListener('click', function (e) {
    const btn = e.target.closest('.chart-range');
    if (!btn) return;
    chartToolbar.querySelectorAll('.chart-range').forEach(b => {
      const active = b === btn;
      b.classList.toggle('is-active', active);
      if (active) b.setAttribute('aria-selected', 'true'); else b.removeAttribute('aria-selected');
    });
    chartMinutes = parseInt(btn.dataset.minutes, 10) || 30;
    pollChart();
  });

  // Theme reactivity
  document.addEventListener('themechange', function () {
    applyChartTheme();
    if (chart) chart.update();
  });

  // Visibility-aware polling
  function start() {
    pollLoad();
    pollChart();
    pollEvents();
    loadTimer = setInterval(function () { pollLoad(); pollEvents(); }, POLL_INTERVAL);
    chartTimer = setInterval(pollChart, CHART_POLL_INTERVAL);
    cooldownTimer = setInterval(tickCooldowns, COOLDOWN_TICK);
  }
  function stop() {
    clearInterval(loadTimer); loadTimer = null;
    clearInterval(chartTimer); chartTimer = null;
    clearInterval(cooldownTimer); cooldownTimer = null;
  }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stop();
    else if (!loadTimer) start();
  });

  // Boot
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();