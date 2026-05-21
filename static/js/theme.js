// Theme toggle: persists the user's choice to localStorage.
(function () {
  'use strict';

  const STORAGE_KEY = 'theme';
  const root = document.documentElement;

  function currentTheme() {
    return root.getAttribute('data-theme') || 'dark';
  }

  function setTheme(theme) {
    root.setAttribute('data-theme', theme);
    try { localStorage.setItem(STORAGE_KEY, theme); } catch (_) { }
    document.dispatchEvent(new CustomEvent('themechange', { detail: { theme } }));
  }

  function toggle() {
    setTheme(currentTheme() === 'dark' ? 'light' : 'dark');
  }

  document.addEventListener('click', function (event) {
    const trigger = event.target.closest('[data-theme-toggle]');
    if (trigger) {
      event.preventDefault();
      toggle();
    }
  });

  window.AppTheme = { current: currentTheme, set: setTheme, toggle };
})();