// Devices page: CRUD + manual toggle + inline priority edit.
(function () {
  'use strict';

  const POLL_INTERVAL = 7000;

  // DOM refs
  const tbody = document.getElementById('devices-tbody');
  const cardList = document.getElementById('devices-cards');
  const btnAdd = document.getElementById('btn-add-device');
  const addModal = document.getElementById('add-modal');
  const addForm = document.getElementById('add-form');
  const addPriority = document.getElementById('add-priority');
  const deleteModal = document.getElementById('delete-modal');
  const deleteNameEl = document.getElementById('delete-name');
  const confirmDeleteBtn = document.getElementById('confirm-delete');
  const globalStatus = document.getElementById('global-status');
  const priorityOptionsTpl = document.getElementById('tpl-priority-options').innerHTML;

  // State
  let devices = [];
  let pollTimer = null;
  let pendingDeleteId = null;

  // Utilities
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  }
  function prioritySelectHtml(current) {
    let html = '';
    for (let i = 1; i <= 10; i++) {
      const sel = i === current ? ' selected' : '';
      html += `<option value="${i}"${sel}>${i}</option>`;
    }
    return html;
  }
  function statusBadge(d) {
    if (d.is_on) return '<span class="badge badge-success"><span class="dot"></span>ON</span>';
    if (d.shed_at) return '<span class="badge badge-warning"><span class="dot"></span>SHED</span>';
    return '<span class="badge badge-muted"><span class="dot"></span>OFF</span>';
  }
  function setGlobalStatus(state, text) {
    if (!globalStatus) return;
    globalStatus.dataset.status = state;
    const t = globalStatus.querySelector('.status-text');
    if (t) t.textContent = text;
  }

  // Rendering
  function renderTable() {
    if (!devices.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty-text">Поки немає приладів. Натисніть «Додати прилад», щоб створити першого.</td></tr>';
      return;
    }
    tbody.innerHTML = devices.map(d => `
      <tr data-id="${d.id}">
        <td><a href="/devices/${d.id}/" class="device-name-link" title="Відкрити сторінку приладу"><strong>${escapeHtml(d.name)}</strong></a></td>
        <td class="device-id-cell">${escapeHtml(d.device_id)}</td>
        <td>
          <select class="select priority-input" data-action="priority" aria-label="Пріоритет">
            ${prioritySelectHtml(d.priority)}
          </select>
        </td>
        <td class="device-power-cell">${fmtWatts(d.last_power_watts || 0)}</td>
        <td class="text-muted">${d.last_seen_at ? fmtAgo(d.last_seen_at) : 'немає'}</td>
        <td>
          <label class="toggle" title="Перемкнути реле">
            <input type="checkbox" data-action="toggle" ${d.is_on ? 'checked' : ''}>
            <span class="slider"></span>
            <span class="hidden">toggle</span>
          </label>
          ${d.shed_at && !d.is_on ? `<div class="text-subtle mono" style="font-size:11px; margin-top:4px;">shed ${fmtAgo(d.shed_at)}</div>` : ''}
        </td>
        <td class="actions-cell">
          <button class="btn btn-ghost btn-sm" data-action="delete" aria-label="Видалити">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg>
          </button>
        </td>
      </tr>
    `).join('');
  }

  function renderCards() {
    if (!devices.length) {
      cardList.innerHTML = '<div class="empty-text">Поки немає приладів.</div>';
      return;
    }
    cardList.innerHTML = devices.map(d => `
      <article class="device-card" data-id="${d.id}">
        <div class="device-card-head">
          <div>
            <a href="/devices/${d.id}/" class="device-name-link"><span class="device-card-name">${escapeHtml(d.name)}</span></a>
            <span class="device-card-id">${escapeHtml(d.device_id)}</span>
          </div>
          ${statusBadge(d)}
        </div>
        <div class="device-card-metrics">
          <div class="device-card-metric">
            <span class="label">Потужність</span>
            <span class="value">${fmtWatts(d.last_power_watts || 0)}</span>
          </div>
          <div class="device-card-metric">
            <span class="label">Останній сигнал</span>
            <span class="value">${d.last_seen_at ? fmtAgo(d.last_seen_at) : 'немає'}</span>
          </div>
        </div>
        ${d.shed_at && !d.is_on ? `<div class="badge badge-warning" style="align-self: flex-start;"><span class="dot"></span>Shed · ${fmtAgo(d.shed_at)}</div>` : ''}
        <div class="device-card-controls">
          <label class="toggle">
            <input type="checkbox" data-action="toggle" ${d.is_on ? 'checked' : ''}>
            <span class="slider"></span>
            <span>Реле</span>
          </label>
          <div class="priority-control">
            <label for="prio-${d.id}" class="text-muted" style="font-size: 13px;">Пріоритет</label>
            <select id="prio-${d.id}" class="select priority-input" data-action="priority">
              ${prioritySelectHtml(d.priority)}
            </select>
          </div>
          <button class="btn btn-ghost btn-sm" data-action="delete" style="margin-left:auto;" aria-label="Видалити">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg>
          </button>
        </div>
      </article>
    `).join('');
  }

  function render() {
    renderTable();
    renderCards();
    updateGlobalStatusFromDevices();
  }

  function updateGlobalStatusFromDevices() {
    const total = devices.reduce((s, d) => s + (d.is_on ? (d.last_power_watts || 0) : 0), 0);
    setGlobalStatus(total > 0 ? 'ok' : 'paused', `${devices.length} приладів · ${fmtWatts(total)} ON`);
  }

  // Loading
  async function load() {
    try {
      const data = await apiFetch('/api/devices/?page_size=100', { toastOnError: false });
      devices = Array.isArray(data) ? data : (data.results || []);
      render();
    } catch (e) {
      setGlobalStatus('error', 'Не вдалось завантажити прилади');
    }
  }

  // Per-row actions
  async function handleToggle(deviceId, checkbox) {
    const original = !checkbox.checked;
    checkbox.disabled = true;
    try {
      const updated = await apiFetch(`/api/devices/${deviceId}/toggle/`, { method: 'POST' });
      const idx = devices.findIndex(x => x.id === deviceId);
      if (idx !== -1) devices[idx] = updated;
      render();
      showToast(`${updated.name} ${updated.is_on ? 'увімкнено' : 'вимкнено'}`, 'success');
    } catch (e) {
      checkbox.checked = original;
    } finally {
      checkbox.disabled = false;
    }
  }

  async function handlePriorityChange(deviceId, select) {
    const newPriority = parseInt(select.value, 10);
    const idx = devices.findIndex(x => x.id === deviceId);
    if (idx === -1) return;
    const oldPriority = devices[idx].priority;
    select.disabled = true;
    try {
      const updated = await apiFetch(`/api/devices/${deviceId}/`, {
        method: 'PATCH',
        body: { priority: newPriority },
      });
      devices[idx] = updated;
      showToast(`Пріоритет для «${updated.name}» → ${updated.priority}`, 'success');
    } catch (e) {
      select.value = oldPriority;
    } finally {
      select.disabled = false;
    }
  }

  function askDelete(deviceId) {
    const d = devices.find(x => x.id === deviceId);
    if (!d) return;
    pendingDeleteId = deviceId;
    deleteNameEl.textContent = `${d.name} (${d.device_id})`;
    openModal(deleteModal);
  }

  async function performDelete() {
    if (!pendingDeleteId) return;
    confirmDeleteBtn.disabled = true;
    try {
      await apiFetch(`/api/devices/${pendingDeleteId}/`, { method: 'DELETE' });
      devices = devices.filter(x => x.id !== pendingDeleteId);
      render();
      showToast('Прилад видалено', 'success');
      closeModal(deleteModal);
    } finally {
      confirmDeleteBtn.disabled = false;
      pendingDeleteId = null;
    }
  }

  function handleRowEvent(event) {
    const root = event.target.closest('tr[data-id], article.device-card[data-id]');
    if (!root) return;
    const deviceId = parseInt(root.dataset.id, 10);
    const trigger = event.target.closest('[data-action]');
    if (!trigger) return;
    const action = trigger.dataset.action;

    if (action === 'toggle' && event.type === 'change') {
      handleToggle(deviceId, trigger);
    } else if (action === 'priority' && event.type === 'change') {
      handlePriorityChange(deviceId, trigger);
    } else if (action === 'delete' && event.type === 'click') {
      event.preventDefault();
      askDelete(deviceId);
    }
  }
  document.addEventListener('change', handleRowEvent);
  document.addEventListener('click', handleRowEvent);

  // Modal plumbing
  function openModal(modal) {
    modal.hidden = false;
    requestAnimationFrame(() => modal.classList.add('is-open'));
    const focusable = modal.querySelector('input, select, textarea, button');
    if (focusable) setTimeout(() => focusable.focus(), 60);
  }
  function closeModal(modal) {
    modal.classList.remove('is-open');
    setTimeout(() => { modal.hidden = true; }, 200);
  }
  document.addEventListener('click', function (e) {
    if (e.target.matches('[data-modal-close]') || e.target.matches('.modal-backdrop')) {
      const backdrop = e.target.closest('.modal-backdrop');
      if (backdrop) closeModal(backdrop);
    }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      document.querySelectorAll('.modal-backdrop.is-open').forEach(closeModal);
    }
  });

  // Add modal
  addPriority.innerHTML = priorityOptionsTpl;
  addPriority.value = '5';

  btnAdd.addEventListener('click', function () {
    addForm.reset();
    addPriority.value = '5';
    openModal(addModal);
  });

  addForm.addEventListener('submit', async function (e) {
    e.preventDefault();
    const fd = new FormData(addForm);
    const payload = {
      name: (fd.get('name') || '').toString().trim(),
      device_id: (fd.get('device_id') || '').toString().trim(),
      priority: parseInt(fd.get('priority'), 10) || 5,
      description: (fd.get('description') || '').toString(),
      is_on: !!fd.get('is_on'),
    };
    if (!payload.name || !payload.device_id) return;
    const submit = addForm.querySelector('button[type=submit]') || document.querySelector('button[form="add-form"]');
    if (submit) submit.disabled = true;
    try {
      const created = await apiFetch('/api/devices/', { method: 'POST', body: payload });
      devices.push(created);
      devices.sort((a, b) => a.priority - b.priority || a.name.localeCompare(b.name));
      render();
      showToast(`Прилад «${created.name}» додано`, 'success');
      closeModal(addModal);
    } catch (_) {
    } finally {
      if (submit) submit.disabled = false;
    }
  });

  // Delete confirm
  confirmDeleteBtn.addEventListener('click', performDelete);

  // Polling + boot
  function start() {
    load();
    pollTimer = setInterval(load, POLL_INTERVAL);
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