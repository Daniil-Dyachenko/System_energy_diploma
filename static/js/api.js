// API helper + toast notifications.
(function () {
  'use strict';

  // CSRF
  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && meta.content) return meta.content;
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  // Error type
  class ApiError extends Error {
    constructor(status, payload, message) {
      super(message || `HTTP ${status}`);
      this.status = status;
      this.payload = payload;
    }
    userMessage() {
      const p = this.payload;
      if (!p) return this.message;
      if (typeof p === 'string') return p;
      if (p.detail) return p.detail;
      if (typeof p === 'object') {
        const firstKey = Object.keys(p)[0];
        if (firstKey) {
          const val = p[firstKey];
          if (Array.isArray(val)) return `${firstKey}: ${val[0]}`;
          if (typeof val === 'string') return `${firstKey}: ${val}`;
        }
      }
      return this.message;
    }
  }
  window.ApiError = ApiError;

  // Fetch wrapper
  async function apiFetch(url, options = {}) {
    const opts = Object.assign({ toastOnError: true }, options);
    const method = (opts.method || 'GET').toUpperCase();
    const headers = Object.assign({ 'Accept': 'application/json' }, opts.headers || {});
    let body = opts.body;

    if (body !== undefined && body !== null && !(body instanceof FormData)) {
      if (typeof body === 'object') {
        body = JSON.stringify(body);
        headers['Content-Type'] = 'application/json';
      }
    }
    if (method !== 'GET' && method !== 'HEAD') {
      headers['X-CSRFToken'] = csrfToken();
    }

    let response;
    try {
      response = await fetch(url, {
        method,
        headers,
        body,
        credentials: 'same-origin',
        cache: 'no-store',
      });
    } catch (err) {
      if (opts.toastOnError) showToast('Сервер недоступний', 'error');
      throw new ApiError(0, null, err.message || 'network error');
    }

    if (response.status === 204) return null;

    const contentType = response.headers.get('content-type') || '';
    let payload = null;
    if (contentType.includes('application/json')) {
      payload = await response.json().catch(() => null);
    } else {
      payload = await response.text().catch(() => null);
    }

    if (!response.ok) {
      const error = new ApiError(response.status, payload);
      if (opts.toastOnError) {
        showToast(error.userMessage() || `Помилка ${response.status}`, 'error');
      }
      throw error;
    }
    return payload;
  }

  window.apiFetch = apiFetch;

  // Toast notifications
  function showToast(message, type) {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = 'toast toast-' + (type || 'info');
    toast.textContent = message;
    container.appendChild(toast);

    const TTL = type === 'error' ? 5000 : 2800;
    setTimeout(() => dismiss(toast), TTL);
    toast.addEventListener('click', () => dismiss(toast));
  }

  function dismiss(toast) {
    if (!toast.isConnected || toast.dataset.leaving) return;
    toast.dataset.leaving = '1';
    toast.classList.add('is-leaving');
    const remove = () => toast.remove();
    toast.addEventListener('animationend', remove, { once: true });
    setTimeout(remove, 1000);
  }
  window.showToast = showToast;

  // Tiny helpers exported globally
  /** Format wattage */
  function fmtWatts(value, unit = 'Вт') {
    if (value == null || isNaN(value)) return '— ' + unit;
    const v = Number(value);
    if (Math.abs(v) >= 1000) {
      return (v / 1000).toLocaleString('uk-UA', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' к' + unit;
    }
    return Math.round(v).toLocaleString('uk-UA') + ' ' + unit;
  }
  /** Format a relative-time delta. Accepts ISO strings or Date. */
  function fmtAgo(when) {
    if (!when) return '—';
    const ts = (when instanceof Date) ? when.getTime() : Date.parse(when);
    if (isNaN(ts)) return '—';
    const secs = Math.max(0, Math.floor((Date.now() - ts) / 1000));
    if (secs < 5) return 'щойно';
    if (secs < 60) return secs + ' с тому';
    const mins = Math.floor(secs / 60);
    if (mins < 60) return mins + ' хв тому';
    const hours = Math.floor(mins / 60);
    if (hours < 24) return hours + ' год тому';
    return Math.floor(hours / 24) + ' дн тому';
  }
  window.fmtWatts = fmtWatts;
  window.fmtAgo = fmtAgo;

  // System status pill
  let statusPillTimer = null;
  let statusPillStarted = false;

  function setSystemStatus(state, text) {
    const pill = document.getElementById('global-status');
    if (!pill) return;
    pill.dataset.status = state;
    const textEl = pill.querySelector('.status-text');
    if (textEl) textEl.textContent = text;
  }

  function applySystemStatus(data) {
    const total = data.total_power_watts || 0;
    const limit = data.power_limit_watts || 0;
    if (!data.is_active) {
      setSystemStatus('paused', `Алгоритм на паузі · ${fmtWatts(total)} / ${fmtWatts(limit)}`);
    } else if (data.is_overloaded) {
      setSystemStatus('overload', `Перевантаження · ${fmtWatts(total)} / ${fmtWatts(limit)}`);
    } else {
      setSystemStatus('ok', `OK · ${fmtWatts(total)} / ${fmtWatts(limit)}`);
    }
  }

  /**
   * Keep the global topbar status pill live by polling /api/current-load/.
   */
  function initSystemStatusPill(intervalMs = 5000) {
    if (statusPillStarted) return;
    statusPillStarted = true;

    async function poll() {
      try {
        const data = await apiFetch('/api/current-load/', { toastOnError: false });
        applySystemStatus(data);
      } catch (_) {
        setSystemStatus('error', "Зв'язок з сервером втрачено");
      }
    }
    function start() { poll(); statusPillTimer = setInterval(poll, intervalMs); }
    function stop() { clearInterval(statusPillTimer); statusPillTimer = null; }

    document.addEventListener('visibilitychange', function () {
      if (document.hidden) stop();
      else if (!statusPillTimer) start();
    });
    start();
  }
  window.initSystemStatusPill = initSystemStatusPill;
})();