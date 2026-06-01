// Cabinet page — period selector, tariff editor, per-device consumption table.
(function () {
  'use strict';

  // DOM
  const periodForm = document.getElementById('period-form');
  const sinceInput = document.getElementById('period-since');
  const untilInput = document.getElementById('period-until');
  const periodSummary = document.getElementById('period-summary');
  const presets = periodForm.querySelectorAll('.chip');

  const totalsPeriodKwh = document.getElementById('totals-period-kwh');
  const totalsPeriodUah = document.getElementById('totals-period-uah');
  const totalsLifetimeKwh = document.getElementById('totals-lifetime-kwh');
  const totalsLifetimeUah = document.getElementById('totals-lifetime-uah');

  const tariffForm = document.getElementById('tariff-form');
  const tariffInput = document.getElementById('tariff-input');
  const btnTariffSave = document.getElementById('btn-tariff-save');
  const btnTariffReset = document.getElementById('btn-tariff-reset');

  const rowsEl = document.getElementById('account-rows');
  const cardsEl = document.getElementById('account-cards');
  const btnExportCsv = document.getElementById('btn-export-csv');

  let lastSummary = null;

  const DEFAULT_TARIFF = '4.32';

  //  Helpers
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  }
  function fmtNumber(value, fractionDigits) {
    if (value == null || isNaN(value)) return '—';
    return Number(value).toLocaleString('uk-UA', {
      minimumFractionDigits: fractionDigits,
      maximumFractionDigits: fractionDigits,
    });
  }
  function fmtKwh(value) { return fmtNumber(value, 2); }
  function fmtUah(value) { return fmtNumber(value, 2); }
  function todayLocal() {
    const d = new Date();
    return new Date(d.getFullYear(), d.getMonth(), d.getDate());
  }
  function toIsoDate(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }
  function setActivePreset(name) {
    presets.forEach(btn => {
      const isActive = btn.dataset.preset === name;
      btn.classList.toggle('is-active', isActive);
      if (isActive) btn.setAttribute('aria-pressed', 'true');
      else btn.removeAttribute('aria-pressed');
    });
  }
  function clearActivePreset() {
    presets.forEach(btn => {
      btn.classList.remove('is-active');
      btn.removeAttribute('aria-pressed');
    });
  }

  // Period defaults
  function applyPreset(name) {
    const today = todayLocal();
    let since, until = today;
    switch (name) {
      case '7':
        since = new Date(today); since.setDate(since.getDate() - 6); break;
      case '30':
        since = new Date(today); since.setDate(since.getDate() - 29); break;
      case '90':
        since = new Date(today); since.setDate(since.getDate() - 89); break;
      case 'month':
        since = new Date(today.getFullYear(), today.getMonth(), 1); break;
      case 'prev-month': {
        const firstThis = new Date(today.getFullYear(), today.getMonth(), 1);
        until = new Date(firstThis); until.setDate(until.getDate() - 1);
        since = new Date(until.getFullYear(), until.getMonth(), 1);
        break;
      }
      case 'year':
        since = new Date(today); since.setDate(since.getDate() - 364); break;
      default:
        return;
    }
    sinceInput.value = toIsoDate(since);
    untilInput.value = toIsoDate(until);
    setActivePreset(name);
    fetchSummary();
  }

  function seedDefaultPeriod() {
    const today = todayLocal();
    const since = new Date(today); since.setDate(since.getDate() - 29);
    sinceInput.value = toIsoDate(since);
    untilInput.value = toIsoDate(today);
    untilInput.max = toIsoDate(today);
    sinceInput.max = toIsoDate(today);
    setActivePreset('30');
  }

  // Render
  function applySummary(data) {
    lastSummary = data;

    const days = (data.period && data.period.days) || 0;
    if (data.period) {
      periodSummary.textContent =
        `${data.period.since} → ${data.period.until} (${days} ` +
        (days === 1 ? 'день' : (days < 5 ? 'дні' : 'днів')) + ')';
    }

    totalsPeriodKwh.textContent = fmtKwh(data.period_kwh);
    totalsPeriodUah.textContent = fmtUah(data.period_uah);
    totalsLifetimeKwh.textContent = fmtKwh(data.lifetime_kwh);
    totalsLifetimeUah.textContent = fmtUah(data.lifetime_uah);

    if (document.activeElement !== tariffInput) {
      tariffInput.value = Number(data.tariff_uah_per_kwh).toFixed(2);
    }

    renderDeviceRows(data.devices || []);
  }

  function renderDeviceRows(devices) {
    if (!devices.length) {
      const msg = '<td colspan="3" class="empty-text">Немає зареєстрованих приладів. Додайте через <a href="/devices/">сторінку приладів</a>.</td>';
      rowsEl.innerHTML = `<tr>${msg}</tr>`;
      cardsEl.innerHTML = '<div class="empty-text">Немає зареєстрованих приладів.</div>';
      return;
    }

    const sorted = devices.slice().sort((a, b) => {
      const diff = (b.lifetime_kwh || 0) - (a.lifetime_kwh || 0);
      return diff !== 0 ? diff : String(a.name).localeCompare(String(b.name), 'uk');
    });

    const stateBadge = d => d.is_on
      ? '<span class="badge badge-success"><span class="dot"></span>ON</span>'
      : '<span class="badge badge-muted"><span class="dot"></span>OFF</span>';

    const metricCell = (kwh, uah, extraClass = '') => `
      <td class="metric-cell ${extraClass}">
        <div class="metric-primary mono">${fmtKwh(kwh)}<span class="metric-unit"> кВт·год</span></div>
        <div class="metric-secondary mono">${fmtUah(uah)}<span class="metric-unit"> грн</span></div>
      </td>
    `;

    rowsEl.innerHTML = sorted.map(d => `
      <tr>
        <td class="device-cell-wrap">
          <div class="device-cell">
            <a href="/devices/${d.id}/" class="device-name">${escapeHtml(d.name)}</a>
            ${stateBadge(d)}
          </div>
          <div class="device-cell-meta">
            <span class="device-cell-id">${escapeHtml(d.device_id)}</span>
            <span class="device-cell-prio">Пріоритет ${d.priority}</span>
          </div>
        </td>
        ${metricCell(d.period_kwh, d.period_uah)}
        ${metricCell(d.lifetime_kwh, d.lifetime_uah, 'col-lifetime')}
      </tr>
    `).join('');

    cardsEl.innerHTML = sorted.map(d => `
      <article class="account-mini">
        <div class="account-mini-head">
          <div class="account-mini-title">
            <a href="/devices/${d.id}/" class="device-name">${escapeHtml(d.name)}</a>
            <div class="device-cell-meta">
              <span class="device-cell-id">${escapeHtml(d.device_id)}</span>
              <span class="device-cell-prio">Пріоритет ${d.priority}</span>
            </div>
          </div>
          ${stateBadge(d)}
        </div>
        <div class="account-mini-grid">
          <div class="account-mini-metric">
            <div class="metric-label">За період</div>
            <div class="metric-primary mono">${fmtKwh(d.period_kwh)}<span class="metric-unit"> кВт·год</span></div>
            <div class="metric-secondary mono">${fmtUah(d.period_uah)}<span class="metric-unit"> грн</span></div>
          </div>
          <div class="account-mini-metric col-lifetime">
            <div class="metric-label">За весь час</div>
            <div class="metric-primary mono">${fmtKwh(d.lifetime_kwh)}<span class="metric-unit"> кВт·год</span></div>
            <div class="metric-secondary mono">${fmtUah(d.lifetime_uah)}<span class="metric-unit"> грн</span></div>
          </div>
        </div>
      </article>
    `).join('');
  }

  // Fetch + save
  async function fetchSummary() {
    const since = sinceInput.value;
    const until = untilInput.value;
    if (!since || !until) return;
    if (since > until) {
      showToast('Дата "З" не може бути пізнішою за "По"', 'error');
      return;
    }
    btnExportCsv.href =
      `/api/account/export/?since=${encodeURIComponent(since)}&until=${encodeURIComponent(until)}`;
    try {
      const data = await apiFetch(
        `/api/account/summary/?since=${encodeURIComponent(since)}&until=${encodeURIComponent(until)}`,
      );
      applySummary(data);
    } catch (_) {
    }
  }

  async function persistTariff(value, button, successMsg) {
    button.disabled = true;
    try {
      await apiFetch('/api/settings/', {
        method: 'POST',
        body: { tariff_uah_per_kwh: value },
      });
      showToast(successMsg, 'success');
      await fetchSummary();
    } catch (_) {
    } finally {
      button.disabled = false;
    }
  }

  async function saveTariff(event) {
    event.preventDefault();
    const value = parseFloat(tariffInput.value);
    if (!isFinite(value) || value < 0) {
      showToast('Введіть коректне додатне число', 'error');
      return;
    }
    await persistTariff(value.toFixed(2), btnTariffSave, 'Тариф оновлено');
  }

  async function resetTariff() {
    tariffInput.value = Number(DEFAULT_TARIFF).toFixed(2);
    await persistTariff(DEFAULT_TARIFF, btnTariffReset, 'Тариф скинуто до дефолтного (4,32 грн)');
  }

  // Event wiring
  periodForm.addEventListener('submit', function (e) {
    e.preventDefault();
    clearActivePreset();
    fetchSummary();
  });

  presets.forEach(btn => {
    btn.addEventListener('click', () => applyPreset(btn.dataset.preset));
  });

  [sinceInput, untilInput].forEach(input => {
    input.addEventListener('input', clearActivePreset);
  });

  tariffForm.addEventListener('submit', saveTariff);
  btnTariffReset.addEventListener('click', resetTariff);

  // Boot
  function boot() {
    seedDefaultPeriod();
    fetchSummary();
    initSystemStatusPill();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();