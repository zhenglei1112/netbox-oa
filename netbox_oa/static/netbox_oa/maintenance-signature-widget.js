/* Delegation keeps signing available after workbench fragment navigation. */
(() => {
  const states = new WeakMap();
  const write = async (widget, url, operation) => {
    const csrf = widget.closest('form').querySelector('[name=csrfmiddlewaretoken]').value;
    const saving = widget.dataset.saveForm === 'true' && !operation;
    const form = widget.closest('form');
    if (saving && !form.reportValidity()) throw new Error('请先完整填写工单资料。');
    const body = saving ? new FormData(form) : new FormData();
    if (saving) body.set('_signature', '1');
    body.set('csrfmiddlewaretoken', csrf);
    if (operation) body.set('operation', operation);
    const response = await fetch(url, {method:'POST', credentials:'same-origin',
      headers:{Accept:'application/json'}, body});
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || '操作失败，请刷新页面后重试。');
    }
    const data = await response.json();
    if (saving && data.form_action) {
      form.action = data.form_action;
      widget.dataset.createUrl = data.form_action;
      form.querySelector('[name=expected_document_id]').value = data.document_id;
      const policy = form.querySelector('[name=policy]');
      if (policy) policy.disabled = true;
      history.replaceState(history.state, '', data.form_action);
    }
    return data;
  };
  const setSigned = (widget, signed, imageUrl) => {
    widget.querySelector('[data-signature-add]').hidden = signed;
    widget.querySelector('[data-signature-value]').hidden = !signed;
    widget.dataset.signed = signed ? '1' : '0';
    const image = widget.querySelector('[data-signature-image]');
    if (signed) image.src = imageUrl;
    else image.removeAttribute('src');
  };
  const stop = widget => {
    const state = states.get(widget);
    if (state) {state.stopped = true; clearTimeout(state.timer);}
  };
  const poll = async (widget, state) => {
    if (state.stopped || !widget.isConnected) return;
    try {
      const response = await fetch(state.status_url, {credentials:'same-origin',cache:'no-store'});
      if (!response.ok) throw new Error('连接中断，正在重试……');
      const {state: status} = await response.json();
      if (state.stopped || !widget.isConnected) return;
      if (status === 'uploaded' || status === 'confirmed') {
        stop(widget); state.signed = true;
        setSigned(widget, true, state.image_url);
        widget.querySelector('[data-signature-dialog]').close();
        return;
      }
      if (status === 'invalid') {
        stop(widget);
        widget.querySelector('[data-signature-hint]').textContent = '二维码已失效，请关闭后重新添加签名。';
        return;
      }
      widget.querySelector('[data-signature-hint]').textContent = '扫描二维码，添加签名';
    } catch (error) {
      if (!state.stopped) widget.querySelector('[data-signature-hint]').textContent = error.message;
    }
    if (!state.stopped) state.timer = setTimeout(() => poll(widget, state), 2000);
  };
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-signature-add], [data-signature-delete], [data-signature-close], [data-signature-reuse]');
    if (!button) return;
    const widget = button.closest('[data-signature-widget]');
    if (button.disabled) return;
    button.disabled = true;
    const feedback = widget.querySelector('[data-signature-feedback]');
    feedback.textContent = '';
    try {
      if (button.hasAttribute('data-signature-add')) {
        stop(widget);
        const state = await write(widget, widget.dataset.createUrl);
        states.set(widget, state); widget.dataset.actionUrl = state.action_url;
        widget.querySelector('[data-signature-qr]').src = state.qr;
        widget.querySelector('[data-signature-reuse]').hidden = !state.has_saved_signature;
        widget.querySelector('[data-signature-hint]').textContent = '扫描二维码，添加签名';
        const dialog = widget.querySelector('[data-signature-dialog]');
        dialog.showModal();
        dialog.oncancel = event => {event.preventDefault(); widget.querySelector('[data-signature-close]').click();};
        poll(widget, state);
      } else if (button.hasAttribute('data-signature-reuse')) {
        const state = states.get(widget);
        await write(widget, widget.dataset.actionUrl, 'reuse');
        stop(widget);
        setSigned(widget, true, state.image_url);
        widget.querySelector('[data-signature-dialog]').close();
      } else {
        stop(widget);
        await write(widget, widget.dataset.actionUrl, 'cancel');
        setSigned(widget, false);
        widget.querySelector('[data-signature-dialog]').close();
        delete widget.dataset.actionUrl;
      }
    } catch (error) {feedback.textContent = error.message;}
    finally {button.disabled = false;}
  });
  document.addEventListener('input', event => {
    const form = event.target.closest('form');
    const widget = form?.querySelector('[data-save-form="true"]');
    if (!widget || event.target.closest('[data-signature-widget]')) return;
    stop(widget);
    setSigned(widget, false);
    widget.querySelector('[data-signature-dialog]').close();
    widget.querySelector('[data-signature-feedback]').textContent = '';
  });
  document.addEventListener('change', event => {
    if (event.target.matches('select')) event.target.dispatchEvent(new Event('input', {bubbles: true}));
  });
  document.addEventListener('submit', event => {
    const widget = event.target.querySelector('[data-save-form="true"]');
    if (widget && event.submitter?.name === '_submit' && widget.dataset.signed !== '1') {
      event.preventDefault();
      widget.querySelector('[data-signature-add]').click();
    }
  });
})();
