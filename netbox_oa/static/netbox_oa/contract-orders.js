(() => {
  const section = document.getElementById('oa-contract-orders');
  if (section) {
    // The contract plugin exposes only a bottom slot. Move this card before payments,
    // identifying the native add-invoice link, or its heading for view-only users.
    const headers = [...document.querySelectorAll('.card-header')];
    const paymentHeader = headers.find(header => {
      const addLink = [...header.querySelectorAll('a[href]')].some(link =>
        section.dataset.invoiceAddUrl &&
        link.getAttribute('href').split('?')[0] === section.dataset.invoiceAddUrl);
      const heading = [...header.childNodes].filter(node => node.nodeType === 3)
        .map(node => node.textContent).join('').trim();
      return addLink || /^(付款|Invoices|Payments)$/.test(heading);
    });
    const paymentBlock = paymentHeader?.closest('.row') || paymentHeader?.closest('.card');
    paymentBlock?.before(section);
  }
  const dialog = document.getElementById('oa-contract-dialog');
  if (!dialog || dialog.dataset.initialized) return;
  dialog.dataset.initialized = 'true';
  const frame = dialog.querySelector('iframe');
  const status = dialog.querySelector('[data-oa-contract-status]');
  const title = dialog.querySelector('#oa-contract-dialog-title');
  let opener;
  let timer;
  let watchedDocuments = new WeakSet();
  let keyCleanup = [];
  function clearKeys() {
    keyCleanup.forEach(dispose => dispose());
    keyCleanup = [];
    watchedDocuments = new WeakSet();
  }
  function watchKeys(doc) {
    if (!doc || watchedDocuments.has(doc)) return;
    watchedDocuments.add(doc);
    const onKey = event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        dialog.close();
      }
    };
    const watchChild = child => {
      // Browser PDF viewers and cross-origin frames do not expose their documents.
      try { watchKeys(child.contentDocument); } catch { /* Keep the close button available. */ }
    };
    const onLoad = event => {
      if (event.target.tagName === 'IFRAME') watchChild(event.target);
    };
    doc.addEventListener('keydown', onKey);
    doc.addEventListener('load', onLoad, true);
    doc.querySelectorAll('iframe').forEach(watchChild);
    keyCleanup.push(() => {
      doc.removeEventListener('keydown', onKey);
      doc.removeEventListener('load', onLoad, true);
    });
  }
  function failed() {
    clearTimeout(timer);
    frame.hidden = true;
    status.hidden = false;
    status.textContent = '加载失败或无权查看，请关闭后重试。';
  }
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-oa-contract-open]');
    if (!link) return;
    event.preventDefault();
    opener = link;
    title.textContent = link.getAttribute('aria-label');
    frame.title = title.textContent;
    status.textContent = '正在加载…';
    status.hidden = false;
    frame.hidden = true;
    clearTimeout(timer);
    dialog.showModal();
    frame.src = link.href;
    timer = setTimeout(failed, 30000);
  });
  frame.addEventListener('load', () => {
    if (!dialog.open || !frame.getAttribute('src')) return;
    try {
      if (!frame.contentDocument?.querySelector('[data-oa-contract-ready]')) return failed();
      clearTimeout(timer);
      status.hidden = true;
      frame.hidden = false;
      clearKeys();
      watchKeys(frame.contentDocument);
    } catch { failed(); }
  });
  frame.addEventListener('error', failed);
  dialog.querySelector('[data-oa-contract-close]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    clearTimeout(timer);
    clearKeys();
    frame.removeAttribute('src');
    opener?.focus();
  });
})();
