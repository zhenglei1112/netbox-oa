/* Native pages use ordinary form submissions; only material removal needs AJAX. */
(() => {
  document.addEventListener('submit', event => {
    const text = event.target.dataset.v3Confirm;
    if (text && !window.confirm(text)) event.preventDefault();
  });
  document.addEventListener('click', async event => {
    const terminate = event.target.closest('a[href="#v3-terminate"]');
    if (terminate) document.getElementById('v3-terminate').open = true;
    const button = event.target.closest('[data-v3-remove]');
    if (!button || button.disabled || !window.confirm('确认删除此材料？历史版本与操作记录将保留。')) return;
    button.disabled = true;
    try {
      const response = await fetch(button.dataset.v3Remove, {
        method: 'POST', credentials: 'same-origin',
        headers: {'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value},
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || '删除失败，请刷新页面核对权限和当前节点。');
      }
      location.reload();
    } catch (error) {
      const alert = document.createElement('div');
      alert.className = 'alert alert-danger';
      alert.setAttribute('role', 'alert');
      alert.textContent = error.message;
      button.closest('.card').prepend(alert);
      button.disabled = false;
    }
  });
})();
