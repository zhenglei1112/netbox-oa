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
    const paymentCard = paymentHeader?.closest('.card');
    if (paymentCard) {
      const results = document.createElement('div');
      [...paymentCard.children].filter(child => child !== paymentHeader)
        .forEach(child => results.append(child));
      paymentCard.append(results);
      const original = document.createDocumentFragment();
      let activePaymentHeader = paymentHeader;
      let originalFaults;
      function restoreFaults() {
        const current = document.getElementById('contract_otn_faults');
        if (current && originalFaults) {
          current.replaceWith(originalFaults);
          window.htmx?.process(originalFaults);
          originalFaults = null;
        }
      }
      let pending;
      let busyTrigger;
      function setBusy(trigger, busy) {
        if (!trigger) return;
        if (busy) {
          trigger.setAttribute('disabled', '');
          trigger.setAttribute('aria-disabled', 'true');
          trigger.setAttribute('aria-busy', 'true');
        } else {
          trigger.removeAttribute('disabled');
          trigger.removeAttribute('aria-disabled');
          trigger.removeAttribute('aria-busy');
        }
        trigger.querySelector('.oa-payment-filter-indicator')?.classList.toggle('htmx-request', busy);
      }
      let filterUrl;
      async function filterPayments(event) {
        const clear = event.target.closest('[data-oa-payment-clear]');
        const link = event.target.closest('[data-oa-payment-filter]') ||
          (filterUrl && event.target.closest('a[href^="?"]'));
        if (!clear && !link) return;
        if (link?.getAttribute('aria-disabled') === 'true') return;
        event.preventDefault();
        pending?.abort();
        setBusy(busyTrigger, false);
        if (clear) {
          results.replaceChildren(original);
          restoreFaults();
          activePaymentHeader.replaceWith(paymentHeader);
          activePaymentHeader = paymentHeader;
          filterUrl = null;
          results.removeAttribute('aria-busy');
          window.htmx?.process(results);
          return;
        }
        const url = new URL(link.getAttribute('href') || link.getAttribute('data-oa-payment-url'),
          filterUrl || window.location.href);
        const controller = new AbortController();
        pending = controller;
        results.setAttribute('aria-busy', 'true');
        busyTrigger = link;
        setBusy(busyTrigger, true);
        try {
          const response = await fetch(url, {signal: controller.signal});
          if (!response.ok) throw new Error('Payment filter failed');
          const html = await response.text();
          if (controller.signal.aborted) return;
          const parsed = new DOMParser().parseFromString(html, 'text/html');
          if (!parsed.querySelector('[data-oa-payment-results]')) throw new Error('Invalid result');
          const faultTemplate = parsed.querySelector('template[data-oa-linked-faults]');
          const faultCard = document.getElementById('contract_otn_faults');
          const newFaults = faultTemplate?.content.querySelector('#contract_otn_faults');
          if (faultCard && newFaults) {
            if (!originalFaults) originalFaults = faultCard;
            faultCard.replaceWith(newFaults);
            window.htmx?.process(newFaults);
          }
          const headerTemplate = parsed.querySelector('template[data-oa-payment-header]');
          const newHeader = headerTemplate?.content.querySelector('.card-header');
          if (newHeader) {
            const actions = paymentHeader.querySelector('.card-actions');
            const placeholder = newHeader.querySelector('[data-oa-payment-actions]');
            if (actions) placeholder?.replaceWith(actions.cloneNode(true));
            else placeholder?.remove();
            activePaymentHeader.replaceWith(newHeader);
            activePaymentHeader = newHeader;
          }
          headerTemplate?.remove();
          faultTemplate?.remove();
          if (!filterUrl) {
            // Keep the actual native nodes: serializing HTML discards their HTMX
            // event handlers, which may not be available on window to rebind.
            original.append(...results.childNodes);
          }
          results.innerHTML = parsed.body.innerHTML;
          filterUrl = url;
          window.htmx?.process(results);
        } catch (error) {
          if (error.name !== 'AbortError') window.alert('付款筛选失败或无权查看，请重试。');
        } finally {
          if (pending === controller) {
            results.removeAttribute('aria-busy');
            setBusy(busyTrigger, false);
            busyTrigger = null;
          }
        }
      }
      section.addEventListener('click', filterPayments);
      paymentCard.addEventListener('click', filterPayments);
      document.addEventListener('click', event => {
        if (filterUrl && event.target.closest('#contract_otn_faults')) filterPayments(event);
      });
    }

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
