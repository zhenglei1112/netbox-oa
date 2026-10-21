// Standalone pages share NetBox's theme when embedded; public pages use the OS theme.
(() => {
  const root = document.documentElement;
  const preferred = window.matchMedia('(prefers-color-scheme: dark)');
  let parentRoot;
  try {
    if (window.parent !== window) parentRoot = window.parent.document.documentElement;
  } catch (_) {
    // A cross-origin host cannot supply its theme.
  }
  const initial = root.getAttribute('data-bs-theme');
  const sync = () => {
    const inherited = parentRoot?.getAttribute('data-bs-theme');
    const theme = inherited || initial;
    root.setAttribute('data-bs-theme', ['light', 'dark'].includes(theme)
      ? theme : preferred.matches ? 'dark' : 'light');
  };
  sync();
  preferred.addEventListener('change', sync);
  if (parentRoot) {
    const observer = new MutationObserver(sync);
    observer.observe(parentRoot, {attributes: true, attributeFilter: ['data-bs-theme']});
    window.addEventListener('pagehide', () => observer.disconnect(), {once: true});
  }
})();
