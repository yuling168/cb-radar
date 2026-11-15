(function() {
  function getTheme() {
    return localStorage.getItem('cb_theme') || localStorage.getItem('stock_cycle_theme') || 'light';
  }
  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    try {
      localStorage.setItem('cb_theme', theme);
      localStorage.setItem('stock_cycle_theme', theme);
    } catch (e) {}
    const btn = document.getElementById('themeToggleBtn');
    if (btn) {
      btn.innerHTML = (theme === 'dark') ? '☀️ 淺色' : '🌙 深色';
    }
    if (typeof window !== 'undefined' && window.dispatchEvent) {
      window.dispatchEvent(new CustomEvent('themechange', { detail: { theme: theme } }));
    }
  }
  window.toggleTheme = function() {
    const current = document.documentElement.getAttribute('data-theme') || 'light';
    applyTheme(current === 'dark' ? 'light' : 'dark');
  };
  window.setTheme = applyTheme;
  applyTheme(getTheme());
  document.addEventListener('DOMContentLoaded', function() {
    applyTheme(getTheme());
  });
})();
