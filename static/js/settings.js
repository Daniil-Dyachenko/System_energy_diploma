// Settings page — load + save the singleton SystemSettings row.
(function () {
  'use strict';

  // DOM
  const form = document.getElementById('settings-form');
  const inputLimit = document.getElementById('set-limit');
  const inputActive = document.getElementById('set-active');
  const inputCooldown = document.getElementById('set-cooldown');
  const cooldownField = document.getElementById('cooldown-field');
  const modeRadios = form.querySelectorAll('input[name="restore_mode"]');
  const modeExplain = document.getElementById('mode-explain');
  const btnSave = document.getElementById('btn-save');
  const btnReset = document.getElementById('btn-reset');

  const sumUpdated = document.getElementById('sum-updated');
  const sumLimit = document.getElementById('sum-limit');
  const sumMode = document.getElementById('sum-mode');
  const sumCooldown = document.getElementById('sum-cooldown');
  const sumActive = document.getElementById('sum-active');

  let savedState = null;

  // Mode-aware hints
  const MODE_EXPLAIN = {
    AUTO: `<strong>AUTO</strong> — алгоритм автоматично поверне прилад у мережу, щойно
           навантаження впаде нижче ліміту і пройде cooldown. Зручно для звичайних
           домашніх умов.`,
    MANUAL: `<strong>MANUAL</strong> — прилад залишається OFF, доки оператор не
             натисне toggle вручну. Поведінка latching-relay для систем з підвищеними
             вимогами до контролю.`,
  };

  function refreshModeHint(mode) {
    modeExplain.innerHTML = MODE_EXPLAIN[mode] || '';
    const isAuto = mode === 'AUTO';
    cooldownField.style.opacity = isAuto ? '1' : '0.5';
    inputCooldown.disabled = !isAuto;
  }

  // Render
  function applyToForm(data) {
    inputLimit.value = data.power_limit_watts ?? 3000;
    inputActive.checked = !!data.is_active;
    inputCooldown.value = data.restore_cooldown_seconds ?? 30;
    const mode = data.restore_mode === 'MANUAL' ? 'MANUAL' : 'AUTO';
    modeRadios.forEach(r => r.checked = (r.value === mode));
    refreshModeHint(mode);
  }

  function applyToSummary(data) {
    sumUpdated.textContent = data.updated_at ? fmtAgo(data.updated_at) : 'щойно';
    sumLimit.textContent = fmtWatts(data.power_limit_watts);
    sumMode.textContent = data.restore_mode || '—';
    sumCooldown.textContent = (data.restore_cooldown_seconds ?? 0) + ' с';
    sumActive.textContent = data.is_active ? 'активний' : 'на паузі';
    sumActive.style.color = data.is_active
      ? 'var(--color-success)'
      : 'var(--color-warning)';
  }

  // Load + save
  async function load() {
    try {
      const data = await apiFetch('/api/settings/');
      savedState = data;
      applyToForm(data);
      applyToSummary(data);
    } catch (_) {
    }
  }

  async function save(event) {
    event.preventDefault();
    const selectedMode = [...modeRadios].find(r => r.checked);
    const payload = {
      power_limit_watts: parseInt(inputLimit.value, 10) || 0,
      is_active: inputActive.checked,
      restore_mode: selectedMode ? selectedMode.value : 'AUTO',
      restore_cooldown_seconds: parseInt(inputCooldown.value, 10) || 0,
    };
    if (payload.power_limit_watts <= 0) {
      showToast('Ліміт має бути більший за 0', 'error');
      return;
    }
    btnSave.disabled = true;
    try {
      const data = await apiFetch('/api/settings/', { method: 'POST', body: payload });
      savedState = data;
      applyToForm(data);
      applyToSummary(data);
      showToast('Налаштування збережено', 'success');
    } catch (_) {
    } finally {
      btnSave.disabled = false;
    }
  }

  // Event wiring
  form.addEventListener('submit', save);
  btnReset.addEventListener('click', function () {
    if (savedState) applyToForm(savedState);
  });
  modeRadios.forEach(r => r.addEventListener('change', () => refreshModeHint(r.value)));

  // Live-update the summary as the user edits (without saving)
  form.addEventListener('input', function () {
    const selectedMode = [...modeRadios].find(r => r.checked);
    applyToSummary({
      updated_at: savedState ? savedState.updated_at : null,
      power_limit_watts: parseInt(inputLimit.value, 10) || 0,
      restore_mode: selectedMode ? selectedMode.value : 'AUTO',
      restore_cooldown_seconds: parseInt(inputCooldown.value, 10) || 0,
      is_active: inputActive.checked,
    });
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', load);
  } else {
    load();
  }
})();