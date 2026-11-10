(function () {
  if (window.oaTodoListenerInstalled) return;
  window.oaTodoListenerInstalled = true;
  document.addEventListener('change', async function (event) {
    const checkbox = event.target.closest('[data-todo-complete]');
    if (!checkbox || !checkbox.checked || checkbox.disabled) return;
    const widget = checkbox.closest('.oa-todo-list');
    const error = widget.querySelector('[data-todo-error]');
    const form = checkbox.form;
    checkbox.disabled = true;
    error.classList.add('d-none');
    try {
      const response = await fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        credentials: 'same-origin',
        headers: {Accept: 'application/json'},
      });
      const result = await response.json();
      if (!response.ok || !result.success) {
        throw new Error(result.error || '办理失败，请刷新页面后重试。');
      }
      // Refresh all widgets and include any next task assigned to the same account.
      window.location.reload();
    } catch (exception) {
      checkbox.checked = false;
      checkbox.disabled = false;
      window.oaNotify?.(exception.message || '办理失败，请刷新页面后重试。');
    }
  });
})();
