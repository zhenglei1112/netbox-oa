(() => {
  if (window.parent === window) return;
  const saved = document.querySelector('[data-billing-saved]');
  if (saved) {
    const summary = document.querySelector('#oa-billing-summary-result');
    window.parent.postMessage({type: 'oa-billing-saved', total: saved.dataset.total,
      summaryHtml: summary ? JSON.parse(summary.textContent) : undefined}, location.origin);
    return;
  }
  document.querySelector('[data-billing-cancel]')?.addEventListener('click', event => {
    event.preventDefault();
    window.parent.postMessage({type: 'oa-billing-cancel'}, location.origin);
  });
  const page = document.querySelector('[data-billing-editor]');
  if (!page) return;
  const update = () => window.parent.postMessage({
    type: 'oa-billing-layout', height: Math.ceil(page.getBoundingClientRect().height),
  }, location.origin);
  new ResizeObserver(update).observe(page);
  window.addEventListener('load', update);
  update();
})();
