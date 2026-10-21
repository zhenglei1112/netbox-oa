(() => {
  const navigation = document.querySelector('aside nav');
  const main = document.querySelector('main');
  if (!navigation || !main) return;
  let pending;
  let staged;
  const feedback = document.createElement('p');
  feedback.className = 'review-feedback';
  feedback.setAttribute('role', 'status');
  feedback.hidden = true;
  main.append(feedback);

  const ready = (element, signal) => new Promise((resolve, reject) => {
    const finish = error => {
      clearTimeout(timer);
      element.removeEventListener('load', loaded);
      element.removeEventListener('error', failed);
      signal.removeEventListener('abort', aborted);
      if (error) reject(error);
      else resolve();
    };
    const loaded = () => finish();
    const failed = () => finish(new Error('文件加载失败，请重试。'));
    const aborted = () => finish(new DOMException('Aborted', 'AbortError'));
    const timer = setTimeout(() => finish(new Error('文件加载超时，请重试。')), 20000);
    element.addEventListener('load', loaded, {once: true});
    element.addEventListener('error', failed, {once: true});
    signal.addEventListener('abort', aborted, {once: true});
    if (signal.aborted) aborted();
    else if (element.tagName === 'IMG' && element.complete) {
      if (element.naturalWidth) loaded();
      else failed();
    }
  });

  navigation.addEventListener('click', async event => {
    const link = event.target.closest('a');
    if (!link || event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    if (link.hasAttribute('data-review-upload')) return;
    event.preventDefault();
    const url = new URL(link.href, location.href);
    const current = new URL(location.href);
    url.searchParams.sort();
    current.searchParams.sort();
    if (url.href === current.href && !pending) return;
    pending?.abort();
    staged?.remove();
    const controller = new AbortController();
    pending = controller;
    const previous = main.querySelector('.document-view');
    previous.setAttribute('aria-busy', 'true');
    feedback.hidden = false;
    feedback.textContent = '正在加载材料…';
    let nextView;
    try {
      const response = await fetch(url, {credentials: 'same-origin', signal: controller.signal});
      if (!response.ok) throw new Error('材料加载失败，请重试。');
      const next = new DOMParser().parseFromString(await response.text(), 'text/html');
      const content = next.querySelector('.document-view');
      if (!content) throw new Error('登录状态已变化，请刷新页面。');
      if (pending !== controller) return;
      nextView = document.importNode(content, true);
      nextView.classList.add('review-staged');
      const resource = nextView.querySelector('iframe, img');
      const loading = resource ? ready(resource, controller.signal) : Promise.resolve();
      staged = nextView;
      main.append(nextView);
      await loading;
      if (pending !== controller) return;
      nextView.classList.remove('review-staged');
      previous.remove();
      const active = new Map([...next.querySelectorAll('aside nav a')].map(item => [
        item.getAttribute('href'), item.getAttribute('aria-current'),
      ]));
      for (const item of navigation.querySelectorAll('a')) {
        const value = active.get(item.getAttribute('href'));
        item.classList.toggle('selected', Boolean(value));
        if (item.classList.contains('material-file')) {
          item.classList.toggle('btn-primary', Boolean(value));
          item.classList.toggle('btn-ghost-primary', !value);
        }
        if (value) item.setAttribute('aria-current', value);
        else item.removeAttribute('aria-current');
      }
      document.title = next.title;
      history.replaceState(history.state, '', url);
      feedback.hidden = true;
    } catch (error) {
      nextView?.remove();
      if (pending === controller && error.name !== 'AbortError') {
        feedback.textContent = error.message;
      }
    } finally {
      if (pending === controller) {
        previous.removeAttribute('aria-busy');
        pending = undefined;
        staged = undefined;
      }
    }
  });
})();
