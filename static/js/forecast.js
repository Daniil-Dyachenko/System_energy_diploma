// Forecast page — projects total system consumption forward with three
// transparent methods and overlays them on the recent history.
(function () {
  'use strict';

  const REFRESH_INTERVAL = 60000;
  const HISTORY_DAYS = 7;

  // DOM refs
  const subtitleEl = document.getElementById('forecast-subtitle');
  const horizonToolbar = document.getElementById('horizon-toolbar');
  const bannerEl = document.getElementById('overload-banner');
  const bannerTextEl = document.getElementById('overload-text');
  const chartCanvas = document.getElementById('forecast-chart');
  const chartEmpty = document.getElementById('chart-empty');
  const methodCardsEl = document.getElementById('method-cards');
  const explainerEl = document.getElementById('method-explainer');

  // State
  let horizonHours = 24;
  let chart = null;
  let pollTimer = null;
  let lastData = null;

  // Utilities
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  }
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function round1(v) { return Math.round((v || 0) * 10) / 10; }
  function fmtNumber(value, digits) {
    if (value == null || isNaN(value)) return '—';
    return Number(value).toLocaleString('uk-UA', {
      minimumFractionDigits: digits, maximumFractionDigits: digits,
    });
  }
  function fmtKwh(v) { return fmtNumber(v, 2); }
  function fmtUah(v) { return fmtNumber(v, 2); }
  function fmtPct(v) { return v == null ? '—' : fmtNumber(v, 1) + '%'; }
  function fmtLabel(ts) {
    const d = new Date(ts);
    const day = d.toLocaleDateString('uk-UA', { day: '2-digit', month: '2-digit' });
    const hh = d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit', hour12: false });
    return `${day} ${hh}`;
  }

  function chartTheme() {
    return {
      accent: cssVar('--color-accent'),
      warning: cssVar('--color-warning'),
      success: cssVar('--color-success'),
      danger: cssVar('--color-danger'),
      grid: cssVar('--color-border'),
      text: cssVar('--color-text-muted'),
      textStrong: cssVar('--color-text'),
      surface: cssVar('--color-surface-raised'),
    };
  }
  function methodColor(key, t) {
    t = t || chartTheme();
    if (key === 'moving_average') return t.accent;
    if (key === 'linear_trend') return t.warning;
    if (key === 'hourly_profile') return t.success;
    return t.textStrong;
  }

  function buildView(data) {
    const methods = data.methods || [];
    const future = methods.length ? methods[0].points.map(p => p.timestamp) : [];
    const fullHist = data.history || [];
    const showN = Math.min(fullHist.length, Math.max(24, horizonHours * 2));
    const hist = fullHist.slice(fullHist.length - showN);
    const histLen = hist.length;

    const labels = hist.map(p => fmtLabel(p.timestamp)).concat(future.map(fmtLabel));
    const historyData = hist.map(p => round1(p.power_watts)).concat(future.map(() => null));

    const methodData = {};
    methods.forEach(m => {
      const arr = new Array(histLen).fill(null);
      if (histLen > 0) arr[histLen - 1] = round1(hist[histLen - 1].power_watts);
      m.points.forEach(p => arr.push(round1(p.power_watts)));
      methodData[m.key] = arr;
    });

    const limit = data.power_limit_watts || 0;
    const limitData = labels.map(() => limit);

    return { labels, historyData, methodData, limitData, hasHistory: histLen > 0 };
  }

  function buildDatasets(data, view) {
    const t = chartTheme();
    const ds = [{
      label: 'Факт (історія)',
      data: view.historyData,
      borderColor: t.textStrong,
      backgroundColor: 'transparent',
      borderWidth: 2,
      fill: false,
      tension: 0.3,
      pointRadius: 0,
      pointHoverRadius: 4,
      spanGaps: false,
    }];
    (data.methods || []).forEach(m => {
      ds.push({
        label: m.label,
        data: view.methodData[m.key],
        borderColor: methodColor(m.key, t),
        backgroundColor: 'transparent',
        borderWidth: m.key === data.recommended_method ? 2.6 : 1.8,
        borderDash: [6, 4],
        fill: false,
        tension: 0.3,
        pointRadius: 0,
        pointHoverRadius: 4,
        spanGaps: true,
      });
    });
    ds.push({
      label: 'Ліміт',
      data: view.limitData,
      borderColor: t.danger,
      borderDash: [2, 4],
      borderWidth: 1.5,
      fill: false,
      tension: 0,
      pointRadius: 0,
    });
    return ds;
  }

  function ensureChart() {
    if (chart || !window.Chart) return;
    const t = chartTheme();
    chart = new Chart(chartCanvas.getContext('2d'), {
      type: 'line',
      data: { labels: [], datasets: [] },
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
            ticks: { color: t.text, maxRotation: 0, autoSkipPadding: 24 },
            grid: { display: false },
            border: { color: t.grid },
          },
        },
        plugins: {
          legend: {
            display: true,
            position: 'top',
            labels: { color: t.textStrong, usePointStyle: true, boxWidth: 8, padding: 14 },
          },
          tooltip: {
            backgroundColor: t.surface,
            titleColor: t.textStrong,
            bodyColor: t.textStrong,
            borderColor: cssVar('--color-border'),
            borderWidth: 1,
            displayColors: true,
            padding: 10,
            filter: item => item.parsed.y !== null,
            callbacks: { label: ctx => ctx.dataset.label + ': ' + fmtWatts(ctx.parsed.y) },
          },
        },
      },
    });
  }

  function renderChart(data) {
    ensureChart();
    if (!chart) return;
    const view = buildView(data);
    if (!view.hasHistory) {
      chart.data.labels = [];
      chart.data.datasets = [];
      chart.update();
      chartEmpty.classList.remove('hidden');
      return;
    }
    chartEmpty.classList.add('hidden');
    chart.data.labels = view.labels;
    chart.data.datasets = buildDatasets(data, view);
    chart.update();
  }

  // Method cards + explainer
  function renderMethods(data) {
    const methods = data.methods || [];
    if (!methods.length) {
      methodCardsEl.innerHTML = '<div class="empty-text">Немає даних для прогнозу.</div>';
      return;
    }
    methodCardsEl.innerHTML = methods.map(m => {
      const isRec = m.key === data.recommended_method;
      const swatch = methodColor(m.key);
      const accuracy = (m.mae_watts == null)
        ? '<span class="method-acc-na">недостатньо історії для оцінки точності</span>'
        : `Похибка: <strong>${fmtWatts(m.mae_watts)}</strong>` +
          (m.mape_percent != null ? ` · ${fmtPct(m.mape_percent)}` : '');
      const overload = m.predicted_overload
        ? '<span class="badge badge-warning"><span class="dot"></span>можливе перевантаження</span>'
        : '';
      return `
        <article class="card method-card${isRec ? ' is-recommended' : ''}">
          <div class="method-card-head">
            <span class="method-swatch" style="background:${swatch}"></span>
            <span class="method-name">${escapeHtml(m.label)}</span>
            ${isRec ? '<span class="badge badge-success">Рекомендовано</span>' : ''}
          </div>
          <div class="method-value"><span>${fmtKwh(m.energy_kwh)}</span><span class="unit"> кВт·год</span></div>
          <div class="method-sub">≈ <strong>${fmtUah(m.energy_uah)}</strong> грн за ${data.horizon_hours} год</div>
          <div class="method-acc">${accuracy}</div>
          <div class="method-flags">${overload}</div>
        </article>
      `;
    }).join('');
  }

  function renderExplainer(data) {
    const methods = data.methods || [];
    explainerEl.innerHTML = methods.map(m => `
      <li class="forecast-explainer-item">
        <span class="method-swatch" style="background:${methodColor(m.key)}"></span>
        <div><strong>${escapeHtml(m.label)}.</strong> ${escapeHtml(m.description)}</div>
      </li>
    `).join('');
  }

  function renderSubtitle(data) {
    const rec = (data.methods || []).find(m => m.key === data.recommended_method);
    if (rec) {
      subtitleEl.textContent =
        `Горизонт ${data.horizon_hours} год · рекомендовано «${rec.label}» · ` +
        `≈ ${fmtKwh(rec.energy_kwh)} кВт·год / ${fmtUah(rec.energy_uah)} грн`;
    } else {
      subtitleEl.textContent = `Горизонт ${data.horizon_hours} год · недостатньо історії для оцінки`;
    }
  }

  function renderBanner(data) {
    const rec = (data.methods || []).find(m => m.key === data.recommended_method)
      || (data.methods || [])[0];
    if (rec && rec.predicted_overload) {
      const limit = data.power_limit_watts || 0;
      const hit = rec.points.find(p => p.power_watts > limit);
      const when = hit ? ' близько ' + fmtLabel(hit.timestamp) : '';
      bannerTextEl.textContent =
        `За методом «${rec.label}» прогнозується перевищення ліміту ` +
        `(${fmtWatts(limit)})${when}. Розгляньте зниження навантаження або підвищення ліміту.`;
      bannerEl.classList.remove('hidden');
    } else {
      bannerEl.classList.add('hidden');
    }
  }

  // Fetch + render
  function render(data) {
    lastData = data;
    renderSubtitle(data);
    renderBanner(data);
    renderChart(data);
    renderMethods(data);
    renderExplainer(data);
  }

  async function fetchForecast() {
    try {
      const data = await apiFetch(
        `/api/forecast/?hours=${horizonHours}&history_days=${HISTORY_DAYS}`,
        { toastOnError: false },
      );
      render(data);
    } catch (_) {
    }
  }

  // Horizon switcher
  horizonToolbar.addEventListener('click', function (e) {
    const btn = e.target.closest('.chart-range');
    if (!btn) return;
    horizonToolbar.querySelectorAll('.chart-range').forEach(b => {
      const active = b === btn;
      b.classList.toggle('is-active', active);
      if (active) b.setAttribute('aria-selected', 'true'); else b.removeAttribute('aria-selected');
    });
    horizonHours = parseInt(btn.dataset.hours, 10) || 24;
    fetchForecast();
  });

  // Theme reactivity
  document.addEventListener('themechange', function () {
    if (lastData) render(lastData);
  });

  // Visibility-aware polling
  function start() {
    fetchForecast();
    pollTimer = setInterval(fetchForecast, REFRESH_INTERVAL);
    initSystemStatusPill();
  }
  function stop() { clearInterval(pollTimer); pollTimer = null; }
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