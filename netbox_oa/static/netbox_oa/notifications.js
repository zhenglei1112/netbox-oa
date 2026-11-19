/* Use NetBox's Bootstrap Toast for every OA notification, including embedded pages. */
(() => {
  if (window.oaNotify) return;
  const script = document.currentScript || document.querySelector('script[data-netbox-js]');
  const seen = new WeakMap();
  const recent = new Map();
  const pending = [];
  let container;
  let loading;
  function host() {
    try {
      if (window.parent !== window && window.parent.location.origin === location.origin
          && window.parent.oaNotify) return window.parent;
    } catch (_) { /* Cross-origin parents cannot receive private workflow messages. */ }
    return window;
  }
  function show(message, level, confirm) {
    const target = host();
    if (target !== window) {
      if (confirm) return target.oaConfirm(message).then(confirm);
      return target.oaNotify(message, level);
    }
    if (!window.Toast) {
      pending.push([message, level, confirm]);
      if (!loading) {
        loading = document.createElement('script');
        loading.src = script.dataset.netboxJs;
        loading.onload = () => {
          if (window.Toast) for (const args of pending.splice(0)) show(...args);
        };
        document.head.append(loading);
      }
      return;
    }
    if (!container) {
      container = document.createElement('div');
      container.className = 'toast-container position-fixed bottom-0 end-0 p-3';
      container.setAttribute('data-oa-toasts', '');
      // A native top-layer container keeps NetBox toasts above modal upload/signing dialogs.
      if (typeof container.showPopover === 'function') {
        container.setAttribute('popover', 'manual');
        container.style.cssText = 'inset:auto 0 0 auto;margin:0;border:0;background:transparent;overflow:visible';
      }
      document.body.append(container);
    }
    const toast = document.createElement('div');
    toast.className = 'toast toast-dark border-0 shadow-sm';
    toast.setAttribute('role', 'alert');
    toast.setAttribute('aria-live', level === 'danger' ? 'assertive' : 'polite');
    toast.setAttribute('aria-atomic', 'true');
    toast.dataset.bsDelay = '10000';
    const header = document.createElement('div');
    header.className = `toast-header text-bg-${level}`;
    const icon = document.createElement('i');
    icon.className = `mdi mdi-${level === 'success' ? 'check-circle' : level === 'info' ? 'information' : 'alert'} me-1`;
    icon.setAttribute('aria-hidden', 'true');
    const title = document.createElement('span');
    title.textContent = {danger: '错误', warning: '提醒', success: '成功', info: '提示'}[level];
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close me-0 m-auto';
    close.dataset.bsDismiss = 'toast';
    close.setAttribute('aria-label', '关闭提示');
    const body = document.createElement('div');
    body.className = 'toast-body';
    body.textContent = message;
    if (confirm) {
      const actions = document.createElement('div');
      actions.className = 'd-flex justify-content-end gap-2 mt-3';
      for (const [text, accepted, color] of [['取消', false, 'ghost-secondary'], ['确认删除', true, 'danger']]) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `btn btn-${color}`;
        button.textContent = text;
        button.addEventListener('click', () => {confirm(accepted); instance.hide();});
        actions.append(button);
      }
      body.append(actions);
    }
    header.append(icon, title, close);
    toast.append(header, body);
    container.append(toast);
    if (container.hasAttribute('popover')) {
      if (container.matches(':popover-open')) container.hidePopover();
      container.showPopover();
    }
    const instance = new window.Toast(toast, confirm ? {autohide: false} : undefined);
    toast.addEventListener('hidden.bs.toast', () => {
      if (confirm) confirm(false);
      instance.dispose();
      toast.remove();
    });
    instance.show();
  }
  window.oaConfirm = message => new Promise(resolve => show(String(message), 'warning', resolve));
  window.oaNotify = (message, level = 'danger') => {
    message = String(message || '').trim();
    if (!message) return;
    if (!['danger', 'warning', 'success', 'info'].includes(level)) level = 'info';
    const key = `${level}:${message}`;
    const now = Date.now();
    if (now - (recent.get(key) || 0) < 1000) return;
    for (const [key, time] of recent) if (now - time > 10000) recent.delete(key);
    recent.set(key, now);
    show(message, level);
  };
  window.oaNotifyPage = (page = document) => {
    for (const element of page.querySelectorAll(
      '#django-messages .toast, .oa-feedback p, .errorlist, .invalid-feedback, .alert[role="alert"], [data-oa-notice]'
    )) {
      if (element.closest('[data-oa-toasts], [data-oa-inline-errors]')) continue;
      const message = (element.querySelector('.toast-body') || element).textContent.trim();
      element.hidden = true;
      element.style.display = 'none';
      if (!message || seen.get(element) === message) continue;
      seen.set(element, message);
      const summary = element.closest('form')?.querySelector('.alert[role="alert"]');
      if (summary && element !== summary) continue;
      let level = element.dataset.oaNotice || 'danger';
      if (element.matches('.toast')) {
        const header = element.querySelector('.toast-header');
        level = ['danger', 'warning', 'success', 'info'].find(value =>
          header?.classList.contains(`text-bg-${value}`)) || 'info';
      } else if (element.closest('.oa-feedback')) {
        // Django already rendered the same messages with their native severity.
        if (page.querySelector('#django-messages .toast')) continue;
        level = 'success';
      }
      window.oaNotify(message, level);
    }
  };
  document.addEventListener('invalid', event => {
    event.preventDefault();
    const field = event.target;
    window.oaNotify(field.validity?.valueMissing && field.dataset?.requiredMessage
      ? field.dataset.requiredMessage : field.validationMessage);
  }, true);
  const start = () => {
    window.oaNotifyPage();
    new MutationObserver(() => window.oaNotifyPage()).observe(document.body, {
      childList: true, subtree: true, characterData: true,
    });
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
