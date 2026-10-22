(() => {
  const navigation = document.querySelector('aside nav');
  const stream = document.querySelector('.document-view');
  if (!navigation || !stream) return;
  if (window.parent !== window) {
    window.parent.postMessage({type: 'oa-review-ready'}, location.origin);
  }
  const articles = [...stream.querySelectorAll('.review-document')];
  const files = [...navigation.querySelectorAll('a.material-file')];
  const titles = [...navigation.querySelectorAll('a.material-title')];
  const rows = [...navigation.querySelectorAll('a.material-row-link')];
  const states = new Map();
  let active;
  let jumpSequence = 0;
  let requestedArticle;
  const feedback = document.createElement('p');
  feedback.className = 'review-feedback';
  feedback.setAttribute('role', 'status');
  feedback.hidden = true;
  stream.parentElement.append(feedback);

  function select(article) {
    if (!article || active === article) return;
    active = article;
    const documentId = article.dataset.document;
    const material = article.dataset.material;
    for (const link of files) {
      const selected = new URL(link.href, location.href).searchParams.get('document') === documentId;
      link.classList.toggle('selected', selected);
      link.classList.toggle('btn-primary', selected);
      link.classList.toggle('btn-ghost-primary', !selected);
      if (selected) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    for (const link of titles) {
      const selected = new URL(link.href, location.href).searchParams.get('material') === material;
      link.classList.toggle('selected', selected);
      if (selected) link.setAttribute('aria-current', 'true');
      else link.removeAttribute('aria-current');
    }
    for (const link of rows) {
      const selected = Boolean(link.closest('.material-index-row')?.querySelector('.material-file.selected'));
      link.classList.toggle('selected', selected);
      if (selected) link.setAttribute('aria-current', 'true');
      else link.removeAttribute('aria-current');
    }
    const selectedLink = files.find(link => link.hasAttribute('aria-current'));
    if (selectedLink) {
      const bounds = navigation.getBoundingClientRect();
      const rect = selectedLink.getBoundingClientRect();
      if (rect.top < bounds.top || rect.bottom > bounds.bottom) {
        navigation.scrollTop += rect.top - bounds.top - navigation.clientHeight / 2;
      }
    }
    const url = new URL(location.href);
    url.searchParams.set('material', material);
    url.searchParams.set('document', documentId);
    history.replaceState(history.state, '', url);
    document.title = `审阅 · ${article.dataset.title} · ${stream.dataset.orderNumber}`;
  }

  function message(content, text) {
    const paragraph = document.createElement('p');
    paragraph.className = 'review-loading';
    paragraph.setAttribute('role', 'status');
    paragraph.textContent = text;
    content.replaceChildren(paragraph);
  }
  function waitFor(resource, source, state) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => finish(new Error('文件加载超时')), 30000);
      function finish(error) {
        clearTimeout(timer);
        resource.onload = resource.onerror = null;
        state.cancelResource = undefined;
        if (error) reject(error);
        else resolve();
      }
      resource.onload = () => finish();
      resource.onerror = () => finish(new Error('文件加载失败'));
      state.cancelResource = () => finish(new Error('已取消加载'));
      resource.src = source;
    });
  }
  async function load(article) {
    if (states.has(article)) return states.get(article).loading;
    const state = {};
    states.set(article, state);
    const content = article.querySelector('.review-document-content');
    content.setAttribute('aria-busy', 'true');
    message(content, '正在加载材料…');
    state.loading = (async () => {
      try {
        if (article.dataset.type === 'pdf' || article.dataset.type === 'form') {
          const iframe = document.createElement('iframe');
          iframe.title = article.dataset.title;
          iframe.className = 'review-native-preview';
          // Let the browser render the original PDF or HTML, including its own controls.
          const ready = waitFor(iframe, article.dataset.source, state);
          content.replaceChildren(iframe);
          await ready;
        } else if (article.dataset.type === 'image') {
          const image = document.createElement('img');
          image.alt = article.dataset.title;
          image.className = 'review-image';
          const ready = waitFor(image, article.dataset.source, state);
          content.replaceChildren(image);
          await ready;
        } else {
          message(content, '此文件格式暂不支持在线展示，请下载原文件查看。');
        }
        if (state.disposed) return false;
        article.classList.add('review-loaded');
        return true;
      } catch (error) {
        if (state.disposed) return false;
        states.delete(article);
        message(content, '材料加载失败，请重试或下载原文件查看。');
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'btn btn-ghost-primary';
        retry.textContent = '重新加载';
        retry.addEventListener('click', () => jump(article, true));
        content.append(retry);
        return false;
      } finally {
        if (!state.disposed) content.removeAttribute('aria-busy');
      }
    })();
    return state.loading;
  }

  function unload(article) {
    const state = states.get(article);
    if (!state) return;
    state.disposed = true;
    state.cancelResource?.();
    states.delete(article);
    article.classList.remove('review-loaded');
    const content = article.querySelector('.review-document-content');
    content.removeAttribute('aria-busy');
    message(content, '等待加载材料…');
  }
  async function jump(article, focus = false) {
    const request = ++jumpSequence;
    const abandoned = requestedArticle;
    requestedArticle = article;
    if (abandoned && abandoned !== article && abandoned !== active) unload(abandoned);
    feedback.hidden = false;
    feedback.textContent = '正在加载材料…';
    // Give the native preview its full viewport while it loads.
    for (const item of articles) item.hidden = item !== article;
    const loaded = await load(article);
    if (request !== jumpSequence) {
      if (active !== article && requestedArticle !== article) unload(article);
      return;
    }
    if (!loaded && active && active !== article) {
      for (const item of articles) item.hidden = item !== active;
      feedback.textContent = '材料加载失败，当前文档已保留。';
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'btn btn-ghost-primary';
      retry.textContent = '重试';
      retry.addEventListener('click', () => jump(article, focus));
      feedback.append(retry);
      return;
    }
    const previous = active;
    for (const item of articles) item.hidden = item !== article;
    stream.scrollTop = 0;
    select(article);
    if (previous && previous !== article) unload(previous);
    if (focus) stream.focus({preventScroll: true});
    feedback.hidden = true;
  }
  navigation.addEventListener('click', event => {
    const link = event.target?.closest?.('a.material-title, a.material-row-link, a.material-file');
    if (!link || event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    if (link.hasAttribute('data-review-upload') || link.hasAttribute('data-oa-upload')) return;
    event.preventDefault();
    const url = new URL(link.href, location.href);
    const id = url.searchParams.get('document');
    const material = url.searchParams.get('material');
    const article = articles.find(item => id ? item.dataset.document === id : item.dataset.material === material);
    if (article) return jump(article, true);
  });
  const initial = articles.find(item => item.dataset.document === stream.dataset.selected)
    || articles.find(item => item.dataset.material === stream.dataset.material) || articles[0];
  if (initial) jump(initial);
  window.addEventListener('pagehide', event => {
    if (event.persisted) return;
    for (const article of states.keys()) unload(article);
  });
})();
