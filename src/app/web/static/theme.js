/* Shared by the workbench and coverage page. Light is the first-visit default. */
(() => {
  const key = 'youju.theme.v2';
  let value = 'light';
  try { if (localStorage.getItem(key) === 'dark') value = 'dark'; } catch {}
  const apply = (theme, persist = true) => {
    value = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.dataset.theme = value;
    if (persist) { try { localStorage.setItem(key, value); } catch {} }
    document.dispatchEvent(new CustomEvent('youju:theme', { detail: value }));
  };
  window.youjuTheme = { current: () => value, toggle: () => apply(value === 'light' ? 'dark' : 'light') };
  window.addEventListener('storage', event => { if (event.key === key) apply(event.newValue, false); });
  apply(value, false);
})();
