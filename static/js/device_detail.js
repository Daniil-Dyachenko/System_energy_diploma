// Device-detail page: live numbers + chart + timeline + inline edit.

(function () {
  'use strict';

  const POLL_INTERVAL = 5000;
  const ACTION_LABELS = {
    SHED: { verb: 'Відключено алгоритмом', icon: '↓', cls: 'event-shed' },
    RESTORE: { verb: 'Повернуто алгоритмом', icon: '↑', cls: 'event-restore' },
    MANUAL_ON: { verb: 'Увімкнено вручну', icon: '⏻', cls: 'event-manual-on' },
    MANUAL_OFF: { verb: 'Вимкнено вручну', icon: '⊘', cls: 'event-manual-off' },
  };

  // DOM refs
  const root = document.querySelector('.device-detail');
  if (!root) return;
  const deviceId = parseInt(root.dataset.deviceId, 10);

  const statusEl = document.getElementById('device-status');
  const toggleEl = document.getElementById('device-toggle');
  const currentEl = document.getElementById('device-current');
  const lastSeenEl = document.getElementById('device-last-seen');
  const shedLineEl = document.getElementById('device-shed-line');
  const shedAtEl = document.getElementById('device-shed-at');
  const metricAvgEl = document.getElementById('metric-avg');
  const metricPeakEl = document.getElementById('metric-peak');
  const metricOnTimeEl = document.getElementById('metric-ontime');
  const metricEnergyEl = document.getElementById('metric-energy');
  const chartCanvas = document.getElementById('device-chart');
  const chartEmpty = document.getElementById('chart-empty');
  const chartToolbar = document.getElementById('chart-toolbar');
  const timelineList = document.getElementById('timeline-list');
  const timelineSubtitle = document.getElementById('timeline-subtitle');
  const editForm = document.getElementById('edit-form');
  const editPriority = document.getElementById('edit-priority');
  const editDescription = document.getElementById('edit-description');
  const editReset = document.getElementById('edit-reset');
  const editSave = document.getElementById('edit-save');
  const globalStatus = document.getElementById('global-status');

  // State
  let windowKey = '1h';
  let chart = null;
  let pollTimer = null;
  let lastDevice = null;
  let savedState = null;
  let formDirty = false;
  let onSessionStartedAtMs = null;

  // Utilities
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  }
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function setStatus(state, text) {
    if (!globalStatus) return;
    globalStatus.dataset.status = state;
    const t = globalStatus.querySelector('.status-text');
    if (t) t.textContent = text;
  }
  function fmtDuration(seconds) {
    const s = Math.max(0, Math.round(seconds || 0));
    if (s < 60) return s + ' с';
    const m = Math.round(s / 60);
    if (m < 60) return m + ' хв';
    const h = Math.floor(m / 60);
    const rem = m - h * 60;
    return rem > 0 ? `${h} год ${rem} хв` : `${h} год`;
  }
  function fmtEnergy(kwh) {
    if (kwh == null || isNaN(kwh)) return '— кВт·год';
    if (kwh < 0.001) return '0 кВт·год';
    if (kwh < 1) return kwh.toFixed(3).replace('.', ',') + ' кВт·год';
    return kwh.toFixed(2).replace('.', ',') + ' кВт·год';
  }
  function fmtChartLabel(ts) {
    const d = new Date(ts);
    if (windowKey === '7d') {
      const day = d.toLocaleDateString('uk-UA', { day: '2-digit', month: '2-digit' });
      const hh = d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit', hour12: false });
      return `${day} ${hh}`;
    }
    return d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit', hour12: false });
  }
  function fmtEventTime(ts) {
    const d = new Date(ts);
    const date = d.toLocaleDateString('uk-UA', { day: '2-digit', month: '2-digit' });
    const hh = d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit', hour12: false });
    return `${date} ${hh}`;
  }

  // Status badge / toggle
  function renderDeviceState(d) {
    lastDevice = d;
    currentEl.textContent = Math.round(d.last_power_watts || 0).toLocaleString('uk-UA');
    lastSeenEl.textContent = d.last_seen_at ? fmtAgo(d.last_seen_at) : 'немає даних';

    statusEl.className = 'badge';
    let label, mod;
    if (d.is_on) { label = 'ON'; mod = 'badge-success'; }
    else if (d.shed_at) { label = 'SHED'; mod = 'badge-warning'; }
    else { label = 'OFF'; mod = 'badge-muted'; }
    statusEl.classList.add(mod);
    statusEl.innerHTML = `<span class="dot"></span>${label}`;

    if (d.shed_at && !d.is_on) {
      shedLineEl.hidden = false;
      shedAtEl.textContent = fmtAgo(d.shed_at);
    } else {
      shedLineEl.hidden = true;
    }

    if (toggleEl && document.activeElement !== toggleEl) {
      toggleEl.checked = !!d.is_on;
    }

    setStatus(d.is_on ? 'ok' : 'paused',
      `${d.name} · ${fmtWatts(d.last_power_watts || 0)}` + (d.is_on ? '' : ' (вимкнено)'));
  }

  // Metrics
  function renderMetrics(m, windowSeconds, device) {
    if (!m) return;
    metricAvgEl.textContent = fmtWatts(m.average_watts);
    metricPeakEl.textContent = fmtWatts(m.peak_watts);
    metricEnergyEl.textContent = fmtEnergy(m.energy_kwh);

    if (device && !device.is_on) {
      onSessionStartedAtMs = null;
      metricOnTimeEl.textContent = 'вимкнено';
    } else {
      onSessionStartedAtMs = Date.now() - (m.on_time_seconds || 0) * 1000;
      metricOnTimeEl.textContent = fmtDuration(m.on_time_seconds);
    }
  }

  // Chart
  function chartTheme() {
    return {
      accent: cssVar('--color-accent'),
      accentSoft: cssVar('--color-accent-soft'),
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
        datasets: [{
          label: 'Споживання',
          data: [],
          borderColor: t.accent,
          backgroundColor: t.accentSoft,
          borderWidth: 2,
          fill: true,
          tension: 0.35,
          pointRadius: 0,
          pointHoverRadius: 4,
        }],
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
              callback: v => v >= 1000 ? (v / 1000).toFixed(1) + ' кВт' : v + ' Вт',
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
            callbacks: { label: ctx => 'Споживання: ' + fmtWatts(ctx.parsed.y) },
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
  function renderChart(points) {
    ensureChart();
    if (!chart) return;
    const labels = points.map(p => fmtChartLabel(p.timestamp));
    const values = points.map(p => Math.round((p.power_watts || 0) * 10) / 10);
    chart.data.labels = labels;
    chart.data.datasets[0].data = values;
    chart.update();
    chartEmpty.classList.toggle('hidden', points.length > 0);
  }

  // Timeline
  function renderTimeline(events) {
    if (!events || !events.length) {
      timelineList.innerHTML = '<div class="empty-text">Подій за цей період не було.</div>';
      timelineSubtitle.textContent = 'Подій немає';
      return;
    }
    timelineSubtitle.textContent = `Записів: ${events.length}`;
    const html = events.map(ev => {
      const meta = ACTION_LABELS[ev.action] || { verb: ev.action, icon: '•', cls: '' };
      const power = (ev.power_watts || 0) > 0 ? ` · ${fmtWatts(ev.power_watts)}` : '';
      return `
        <div class="event-row ${meta.cls}" data-event-id="${ev.id}">
          <span class="event-icon-wrap" aria-hidden="true">${meta.icon}</span>
          <div class="event-body">
            <div class="event-line">${escapeHtml(meta.verb)}</div>
            <div class="event-meta">${escapeHtml(fmtEventTime(ev.occurred_at))}${power}</div>
          </div>
          <time class="event-time" datetime="${escapeHtml(ev.occurred_at)}">${fmtAgo(ev.occurred_at)}</time>
        </div>
      `;
    }).join('');
    timelineList.innerHTML = html;
  }

  // Edit form
  function applyServerStateToForm(d) {
    savedState = { priority: d.priority, description: d.description || '' };
    if (!formDirty) {
      editPriority.value = String(d.priority);
      editDescription.value = d.description || '';
    }
  }
  editForm.addEventListener('input', () => { formDirty = true; });
  editReset.addEventListener('click', () => {
    if (!savedState) return;
    editPriority.value = String(savedState.priority);
    editDescription.value = savedState.description || '';
    formDirty = false;
  });
  editForm.addEventListener('submit', async function (e) {
    e.preventDefault();
    editSave.disabled = true;
    try {
      const payload = {
        priority: parseInt(editPriority.value, 10),
        description: editDescription.value,
      };
      const updated = await apiFetch(`/api/devices/${deviceId}/`, {
        method: 'PATCH',
        body: payload,
      });
      savedState = { priority: updated.priority, description: updated.description || '' };
      formDirty = false;
      showToast('Параметри збережено', 'success');
    } catch (_) {
    } finally {
      editSave.disabled = false;
    }
  });

  // Manual toggle
  toggleEl.addEventListener('change', async function () {
    const original = !toggleEl.checked;
    toggleEl.disabled = true;
    try {
      const updated = await apiFetch(`/api/devices/${deviceId}/toggle/`, { method: 'POST' });
      renderDeviceState(updated);
      showToast(`${updated.name} ${updated.is_on ? 'увімкнено' : 'вимкнено'}`, 'success');
      poll();
    } catch (_) {
      toggleEl.checked = original;
    } finally {
      toggleEl.disabled = false;
    }
  });

  // Range switcher
  chartToolbar.addEventListener('click', function (e) {
    const btn = e.target.closest('.chart-range');
    if (!btn) return;
    chartToolbar.querySelectorAll('.chart-range').forEach(b => {
      const active = b === btn;
      b.classList.toggle('is-active', active);
      if (active) b.setAttribute('aria-selected', 'true'); else b.removeAttribute('aria-selected');
    });
    windowKey = btn.dataset.window;
    poll();
  });

  // Polling
  async function poll() {
    try {
      const data = await apiFetch(`/api/devices/${deviceId}/history/?window=${windowKey}`, { toastOnError: false });
      renderDeviceState(data.device);
      applyServerStateToForm(data.device);
      renderMetrics(data.metrics, data.window_seconds, data.device);
      renderChart(data.chart_data || []);
      renderTimeline(data.events || []);
    } catch (e) {
      setStatus('error', "Зв'язок з сервером втрачено");
    }
  }

  function tickRelTimes() {
    if (lastDevice && lastDevice.last_seen_at) {
      lastSeenEl.textContent = fmtAgo(lastDevice.last_seen_at);
    }
    if (lastDevice && lastDevice.shed_at && !lastDevice.is_on) {
      shedAtEl.textContent = fmtAgo(lastDevice.shed_at);
    }
    if (onSessionStartedAtMs !== null) {
      const secs = (Date.now() - onSessionStartedAtMs) / 1000;
      metricOnTimeEl.textContent = fmtDuration(secs);
    }
    timelineList.querySelectorAll('.event-time').forEach(el => {
      const ts = el.getAttribute('datetime');
      if (ts) el.textContent = fmtAgo(ts);
    });
  }

  document.addEventListener('themechange', function () {
    applyChartTheme();
    if (chart) chart.update();
  });

  function start() {
    poll();
    pollTimer = setInterval(poll, POLL_INTERVAL);
    setInterval(tickRelTimes, 1000);
  }
  function stop() {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stop();
    else if (!pollTimer) start();
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();