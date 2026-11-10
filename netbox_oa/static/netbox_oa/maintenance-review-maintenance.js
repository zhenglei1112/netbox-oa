(() => {
  const closeMenus = () => {
    document.querySelectorAll('.material-maintenance[open]').forEach(menu => {
      menu.open = false;
    });
  };
  document.addEventListener('toggle', event => {
    const details = event.target;
    if (!details.matches?.('.material-maintenance') || !details.open) return;
    document.querySelectorAll('.material-maintenance[open]').forEach(other => {
      if (other !== details) other.open = false;
    });
    const menu = details.querySelector('.material-maintenance-menu');
    const trigger = details.querySelector('summary').getBoundingClientRect();
    const bounds = menu.getBoundingClientRect();
    const gap = 8;
    const width = document.documentElement.clientWidth;
    const height = document.documentElement.clientHeight;
    menu.style.left = `${Math.max(gap, Math.min(trigger.right - bounds.width, width - bounds.width - gap))}px`;
    const top = trigger.bottom + gap + bounds.height <= height
      ? trigger.bottom + gap : trigger.top - bounds.height - gap;
    menu.style.top = `${Math.max(gap, Math.min(top, height - bounds.height - gap))}px`;
  }, true);
  document.addEventListener('click', event => {
    if (!event.target.closest('.material-maintenance')) closeMenus();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeMenus();
  });
  document.addEventListener('scroll', event => {
    if (!event.target.closest?.('.material-maintenance-menu')) closeMenus();
  }, true);
  window.addEventListener('resize', closeMenus);
  document.addEventListener('submit', async event => {
    const form = event.target.closest('[data-material-remove]');
    if (!form) return;
    event.preventDefault();
    const button = form.querySelector('button');
    if (button.disabled) return;
    button.disabled = true;
    const confirmed = await window.oaConfirm(`确定删除${form.dataset.materialName}？删除后不再计入当前材料，原文件和历史记录将保留。`);
    if (!confirmed) {button.disabled = false; return;}
    try {
      const response = await fetch(form.action, {
        method: 'POST', credentials: 'same-origin', body: new FormData(form),
      });
      if (!response.ok) {
        const result = await response.json().catch(() => ({}));
        throw new Error(result.error || '删除失败，请确认当前节点及材料删除权限。');
      }
      if (window.parent !== window && window.parent.document.querySelector('.oa-inbox')) {
        window.parent.document.dispatchEvent(new window.parent.CustomEvent('oa-upload-complete'));
      }
      const url = new URL(location.href);
      url.searchParams.delete('document');
      location.replace(url.href);
    } catch (error) {
      window.oaNotify?.(error.message);
      button.disabled = false;
    }
  });
})();
