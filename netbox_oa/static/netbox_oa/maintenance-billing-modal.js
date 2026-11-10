/* Bootstrap Modal is exported by NetBox, providing its native backdrop and focus trap. */
(() => {
  if (window.oaOpenBilling) return;
  let root, header, body, loading, frame, modal, opener, amountField, summaryField, contentHeight;
  let active = false;
  const setLoading = busy => {
    loading.hidden = !busy;
    body.setAttribute('aria-busy', String(busy));
    frame.style.visibility = busy ? 'hidden' : '';
  };
  const resize = () => {
    if (!frame || !contentHeight) return;
    const available = Math.max(0, window.innerHeight * .9 - header.offsetHeight - 2);
    frame.style.height = `${Math.min(contentHeight, available)}px`;
  };
  window.oaOpenBilling = (href, trigger) => {
    const url = new URL(href, location.href);
    if (url.origin !== location.origin || !window.Modal) return false;
    if (!root) {
      root = document.createElement('div');
      root.className = 'modal fade oa-billing-modal';
      root.tabIndex = -1;
      root.setAttribute('aria-label', '维护计费明细');
      root.setAttribute('aria-hidden', 'true');
      const dialog = document.createElement('div');
      dialog.className = 'modal-dialog modal-dialog-centered';
      const content = document.createElement('div');
      content.className = 'modal-content';
      header = document.createElement('div');
      header.className = 'modal-header';
      const title = document.createElement('h2');
      title.className = 'modal-title';
      title.textContent = '维护计费明细';
      const close = document.createElement('button');
      close.type = 'button'; close.className = 'btn-close';
      close.setAttribute('data-bs-dismiss', 'modal');
      close.setAttribute('aria-label', '取消维护计费明细');
      header.append(title, close);
      body = document.createElement('div');
      body.className = 'modal-body';
      loading = document.createElement('div');
      loading.className = 'oa-upload-loading oa-billing-loading';
      loading.setAttribute('role', 'status');
      loading.setAttribute('aria-live', 'polite');
      loading.setAttribute('aria-atomic', 'true');
      const spinner = document.createElement('i');
      spinner.className = 'mdi mdi-loading mdi-spin text-primary';
      spinner.setAttribute('aria-hidden', 'true');
      const loadingText = document.createElement('span');
      loadingText.textContent = '加载中，请稍候…';
      loading.append(spinner, loadingText);
      frame = document.createElement('iframe');
      frame.title = '维护计费明细';
      frame.addEventListener('load', () => {
        if (active && frame.contentDocument?.URL && frame.contentDocument.URL !== 'about:blank') {
          setLoading(false);
        }
      });
      body.append(frame, loading);
      content.append(header, body); dialog.append(content); root.append(dialog);
      document.body.append(root);
      modal = new window.Modal(root, {backdrop: true, focus: true, keyboard: true});
      root.addEventListener('hidden.bs.modal', () => {
        active = false;
        setLoading(false);
        frame.src = 'about:blank';
        contentHeight = null;
        if (opener?.isConnected) opener.focus();
      });
      root.addEventListener('shown.bs.modal', resize);
    }
    opener = trigger;
    amountField = trigger?.closest('form')?.querySelector('[name="settlement_amount"]') || null;
    summaryField = trigger?.closest('form')?.querySelector('[data-billing-summary]') || null;
    contentHeight = null;
    active = true;
    setLoading(true);
    frame.style.height = '400px';
    url.searchParams.set('embed', '1');
    frame.src = url.href;
    modal.show();
    return true;
  };
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-oa-billing]');
    if (!link || event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    if (window.oaOpenBilling(link.dataset.billingUrl || link.href, link)) event.preventDefault();
  });
  window.addEventListener('message', event => {
    if (!active || event.origin !== location.origin
        || event.source !== frame.contentWindow) return;
    if (event.data?.type === 'oa-billing-layout') {
      if (Number.isFinite(event.data.height) && event.data.height > 0) {
        contentHeight = Math.ceil(event.data.height); resize();
        setLoading(false);
      }
      return;
    }
    if (event.data?.type === 'oa-billing-cancel') {modal.hide(); return;}
    if (event.data?.type !== 'oa-billing-saved') return;
    const total = event.data.total;
    if (typeof total !== 'string' || !/^\d{1,12}\.\d{2}$/.test(total)) return;
    if (amountField) {
      amountField.value = total;
      amountField.dispatchEvent(new Event('change', {bubbles: true}));
    }
    if (summaryField && typeof event.data.summaryHtml === 'string') {
      summaryField.innerHTML = event.data.summaryHtml;
    }
    modal.hide();
    window.oaNotify?.('计费明细已保存。请保存验收单以更新验收金额。', 'success');
    if (!amountField && document.querySelector('[data-billing-overview]')) location.reload();
  });
  window.addEventListener('resize', resize);
})();
