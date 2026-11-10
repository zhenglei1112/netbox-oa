/* Progressive enhancement: the server remains the source of truth for every action. */
(() => {
  if (!document.querySelector('.oa-inbox')) return;
  let controller;
  let sequence = 0;
  let posting = false;
  let pendingPop;
  let searchTimer;
  let searchInFlight = false;
  window.addEventListener('message', event => {
    if (event.origin !== location.origin || event.data?.type !== 'oa-review-ready') return;
    for (const frame of root().querySelectorAll('[data-review-src]')) {
      if (event.source !== frame.contentWindow) continue;
      const loading = frame.closest('dialog').querySelector('[data-review-loading]');
      if (loading) loading.hidden = true;
    }
  });
  history.scrollRestoration = "manual";
  const root = () => document.querySelector('.oa-inbox');
  const showNavigationLoading = busy => {
    const loading = root().querySelector('[data-oa-navigation-loading]');
    if (loading) loading.hidden = !busy;
  };
  const list = () => root().querySelector('.oa-order-list');
  const detail = () => root().querySelector('.oa-detail');
  const audit = () => root().querySelector('.oa-audit');
  const auditScroll = () => audit().querySelector('.oa-audit-scroll');
  const historyScroll = () => audit().querySelector('dialog[open] .oa-audit-dialog-scroll') || auditScroll();
  const narrow = () => window.matchMedia('(max-width: 1000px)').matches || root().clientWidth <= 760;
  // Fill the available viewport below the heading, leaving a small bottom gutter.
  const fitWorkspace = () => {
    const columns = root().querySelector('.oa-columns');
    if (!columns) return;
    const offset = Math.ceil(columns.getBoundingClientRect().top + window.scrollY + 24);
    root().style.setProperty('--oa-workbench-offset', `${offset}px`);
  };
  const workspaceObserver = typeof ResizeObserver === 'function'
    ? new ResizeObserver(fitWorkspace) : null;
  window.addEventListener('resize', fitWorkspace);
  const savePosition = () => {
    history.replaceState({...history.state, oaList: list().scrollTop,
      oaDetail: detail().scrollTop, oaAudit: auditScroll().scrollTop,
      oaAuditDialog: audit().querySelector('.oa-audit-dialog-scroll')?.scrollTop || 0, oaWindow: window.scrollY}, '', location.href);
  };
  const feedback = (message) => {
    window.oaNotify?.(message);
  };
  const restore = (state = {}) => {
    state = state || {};
    list().scrollTop = state.oaList || 0;
    detail().scrollTop = state.oaDetail || 0;
    auditScroll().scrollTop = state.oaAudit || 0;
    const historyPanel = audit().querySelector('.oa-audit-dialog-scroll');
    if (historyPanel) historyPanel.scrollTop = state.oaAuditDialog || 0;
    window.scrollTo(0, state.oaWindow || 0);
  };
  function activateTab(link, focus = false) {
    const panel = link.closest('dialog');
    for (const tab of panel.querySelectorAll('[data-oa-tab]')) {
      const active = tab === link;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
    }
    for (const section of panel.querySelectorAll('[data-oa-tab-panel]')) {
      section.hidden = section.dataset.oaTabPanel !== link.dataset.oaTab;
    }
    if (focus) link.focus();
  }

  function enhance() {
    root().classList.add('oa-enhanced');
    fitWorkspace();
    workspaceObserver?.disconnect();
    workspaceObserver?.observe(root());
    for (const panel of root().querySelectorAll('dialog')) {
      const tabs = [...panel.querySelectorAll('[data-oa-tab]')];
      if (tabs.length) {
        panel.querySelector('.oa-panel-tabs').setAttribute('role', 'tablist');
        for (const tab of tabs) {
          const id = panel.id + '-' + tab.dataset.oaTab;
          tab.id = id + '-tab';
          tab.setAttribute('role', 'tab');
          tab.setAttribute('aria-controls', id);
          const section = panel.querySelector(`[data-oa-tab-panel="${tab.dataset.oaTab}"]`);
          section.id = id;
          section.setAttribute('role', 'tabpanel');
          section.setAttribute('aria-labelledby', tab.id);
        }
        activateTab(tabs.find(tab => tab.classList.contains('active')) || tabs[0]);
      }
      if (panel.open && !panel.matches(':modal')) {
        panel.close();
        panel.showModal();
      }
    }
    document.documentElement.classList.toggle('oa-panel-open', Boolean(root().querySelector('dialog[open]')));
  }
  document.addEventListener('close', (event) => {
    if (typeof event.target?.matches !== 'function') return;
    if (event.target.matches('.oa-panel') && !root().querySelector('dialog[open]')) {
      document.documentElement.classList.remove('oa-panel-open');
    }
  }, true);
  document.addEventListener('click', (event) => {
    if (typeof event.target?.closest !== 'function') return;
    const trigger = event.target.closest('.oa-inbox [data-oa-open], .oa-inbox [data-oa-close], .oa-inbox [data-oa-tab]');
    if (!trigger || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button) return;
    if (trigger.hasAttribute('data-oa-tab')) {
      event.preventDefault();
      activateTab(trigger);
    } else if (trigger.hasAttribute('data-oa-close')) {
      event.preventDefault();
      trigger.closest('dialog').close();
    } else if (!posting) {
      const panel = document.getElementById(trigger.dataset.oaOpen);
      if (!panel) return;
      event.preventDefault();
      for (const current of root().querySelectorAll('dialog[open]')) {
        if (current !== panel) current.close();
      }
      if (trigger.dataset.oaPane) {
        const tab = panel.querySelector(`[data-oa-tab="${trigger.dataset.oaPane}"]`);
        if (tab) activateTab(tab);
      }
      if (!panel.open) panel.showModal();
      if (panel.classList.contains('oa-review-panel')) {
        const frame = panel.querySelector('[data-review-src]');
        if (frame && !frame.getAttribute('src')) frame.src = frame.dataset.reviewSrc;
      }
      document.documentElement.classList.add('oa-panel-open');
      if (trigger.dataset.oaEvent) {
        const record = panel.querySelector(`[data-event-id="${trigger.dataset.oaEvent}"]`);
        if (record) {
          record.setAttribute('tabindex', '-1');
          record.focus({preventScroll: true});
          record.scrollIntoView({block: 'center'});
        }
      }
    }
  });
  document.addEventListener('keydown', (event) => {
    if (typeof event.target?.closest !== 'function') return;
    const link = event.target.closest('[data-oa-tab]');
    if (!link || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...link.closest('dialog').querySelectorAll('[data-oa-tab]')];
    const offset = event.key === 'ArrowLeft' ? -1 : 1;
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
      : (tabs.indexOf(link) + offset + tabs.length) % tabs.length;
    activateTab(tabs[index], true);
  });
  document.addEventListener('change', (event) => {
    const input = event.target;
    if (typeof input?.matches !== 'function') return;
    if (!input.matches('.oa-file-drop input[type=file]')) return;
    const label = input.closest('.oa-file-drop');
    label.querySelector('strong').textContent = input.files.length
      ? input.files[0].name : '选择文件，或拖放到此处';
  });
  function queueSearch(input, composing = false) {
    clearTimeout(searchTimer);
    if (posting) return;
    if (searchInFlight) {
      controller?.abort();
      sequence++;
      searchInFlight = false;
      root().removeAttribute('aria-busy');
    }
    if (composing) return;
    searchTimer = setTimeout(() => {
      if (input.isConnected && !posting) input.form.requestSubmit();
    }, 300);
  }
  document.addEventListener('input', event => {
    if (typeof event.target?.matches !== 'function') return;
    if (event.target.matches('.oa-inbox .oa-filters [name="q"]')) {
      queueSearch(event.target, event.isComposing);
    }
  });
  document.addEventListener('compositionstart', event => {
    if (typeof event.target?.matches !== 'function') return;
    if (event.target.matches('.oa-inbox .oa-filters [name="q"]')) queueSearch(event.target, true);
  });
  document.addEventListener('compositionend', event => {
    if (typeof event.target?.matches !== 'function') return;
    if (event.target.matches('.oa-inbox .oa-filters [name="q"]')) queueSearch(event.target);
  });
  async function navigate(url, {mode = 'all', pop = false, state, method = 'GET', body} = {}) {
    clearTimeout(searchTimer);
    searchInFlight = mode === 'search';
    if (controller) controller.abort();
    controller = new AbortController();
    const requestSequence = ++sequence;
    const position = {oaList: list().scrollTop, oaDetail: detail().scrollTop,
      oaAudit: auditScroll().scrollTop,
      oaAuditDialog: audit().querySelector('.oa-audit-dialog-scroll')?.scrollTop || 0,
      oaWindow: window.scrollY};
    const historyOpen = Boolean(audit().querySelector('dialog[open]'));
    const anchor = mode === 'history' ? historyScroll().querySelector('[data-event-id]') : null;
    const anchorId = anchor?.dataset.eventId;
    const anchorTop = anchor?.getBoundingClientRect().top;
    if (!pop) savePosition();
    root().setAttribute('aria-busy', 'true');
    showNavigationLoading(mode === 'all' || mode === 'detail');
    if (mode !== 'search') {
      detail().inert = true;
      audit().inert = true;
    }
    try {
      const response = await fetch(url, {method, body, signal: controller.signal,
        credentials: 'same-origin', headers: {'X-Requested-With': 'XMLHttpRequest', 'X-OA-Navigation': mode}});
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const next = page.querySelector('.oa-inbox');
      if (requestSequence !== sequence) return;
      if (!next || (!response.ok && ![400, 403].includes(response.status))) {
        throw new Error('page-unavailable');
      }
      window.oaNotifyPage?.(page);
      const taskCompleted = method === 'POST' && response.ok
        && Boolean(next.querySelector('[data-oa-task-completed]'));
      if (mode === 'search') {
        // Keep the input, caret, and any unfinished handling form in place.
        list().replaceWith(next.querySelector('.oa-order-list'));
        for (const selector of ['.oa-tabs', '.oa-list-footer']) {
          root().querySelector(selector).replaceWith(next.querySelector(selector));
        }
        root().dataset.listUrl = next.dataset.listUrl;
      } else if (mode === 'detail' || mode === 'history') {
        if (mode === 'history') {
          const form = detail().querySelector('form.oa-actions');
          const replacement = next.querySelector('form.oa-actions');
          if (form && replacement && form.action === replacement.action) {
            replacement.replaceWith(form);
          }
        }
        detail().replaceWith(next.querySelector('.oa-detail'));
        audit().replaceWith(next.querySelector('.oa-audit'));
        if (mode === 'history' && historyOpen) {
          const panel = audit().querySelector('#activity');
          if (panel.open) panel.close();
          panel.showModal();
        }
        for (const row of root().querySelectorAll('[data-order-id]')) {
          const selected = next.querySelector(`[data-order-id="${row.dataset.orderId}"].selected`);
          row.classList.toggle('selected', Boolean(selected));
          const link = row.querySelector('[data-oa-select]');
          if (selected) link.setAttribute('aria-current', 'true');
          else link.removeAttribute('aria-current');
        }
        root().dataset.detailUrl = next.dataset.detailUrl;
        root().classList.add('oa-show-detail');
      } else {
        root().replaceWith(next);
      }
      const targetUrl = method === 'POST' ? root().dataset.detailUrl : url;
      if (!pop) {
        if (method === 'POST' || mode === 'search') history.replaceState(position, '', targetUrl);
        else history.pushState({}, '', targetUrl);
      }
      restore(taskCompleted ? {oaList: 0, oaDetail: 0, oaAudit: 0, oaAuditDialog: 0, oaWindow: 0}
        : pop ? state : {...position, oaList: mode === 'search' ? 0 : position.oaList,
        oaDetail: mode === 'detail' ? 0 : position.oaDetail});
      if (mode === 'history' && anchorId) {
        const newAnchor = historyScroll().querySelector(`[data-event-id="${anchorId}"]`);
        if (newAnchor) {
          const delta = newAnchor.getBoundingClientRect().top - anchorTop;
          historyScroll().scrollTop += delta;
        }
      } else if (taskCompleted) {
        const notice = detail();
        notice.setAttribute('tabindex', '-1');
        notice.focus({preventScroll: true});
      } else if (method === 'POST') {
        const current = detail().querySelector('#oa-current');
        if (current) {
          current.setAttribute('tabindex', '-1');
          current.focus({preventScroll: true});
          current.scrollIntoView({block: 'nearest'});
        }
      } else if (mode === 'detail') {
        detail().focus({preventScroll: true});
        if (narrow()) detail().scrollIntoView({block: 'start'});
      }
      savePosition();
    } catch (error) {
      if (error.name !== 'AbortError') {
        feedback(method === 'POST'
          ? '未能确认办理结果。请刷新页面核对最新状态；填写内容已保留，请勿直接重复提交。'
          : '加载失败，请重试，或使用链接重新打开页面。');
      }
    } finally {
      if (requestSequence === sequence) {
        searchInFlight = false;
        root().removeAttribute('aria-busy');
        showNavigationLoading(false);
        detail().inert = false;
        audit().inert = false;
        enhance();
      }
    }
  }
  document.addEventListener('click', (event) => {
    if (typeof event.target?.closest !== 'function') return;
    const link = event.target.closest('.oa-inbox a[data-oa-select], .oa-inbox a[data-oa-nav], .oa-inbox a[data-oa-history], .oa-inbox a[data-oa-back]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button) return;
    event.preventDefault();
    if (posting) return;
    if (link.hasAttribute('data-oa-back')) {
      savePosition();
      // Invalidate an in-flight detail request before showing the list again.
      controller?.abort();
      sequence++;
      root().removeAttribute('aria-busy');
      showNavigationLoading(false);
      detail().inert = false;
      audit().inert = false;
      root().classList.remove('oa-show-detail');
      history.pushState({...history.state, oaDetail: 0}, '', link.href);
      list().focus({preventScroll: true});
      return;
    }
    navigate(link.href, {mode: link.hasAttribute('data-oa-select') ? 'detail'
      : link.hasAttribute('data-oa-history') ? 'history' : 'all'});
  });
  document.addEventListener('submit', async (event) => {
    const form = event.target;
    if (typeof form?.matches !== 'function') return;
      if (event.submitter?.hasAttribute('data-oa-native')) return;
    if (!form.matches('.oa-inbox form[data-oa-action], .oa-inbox .oa-filters')) return;
    event.preventDefault();
    if (posting) return;
    if (form.matches('.oa-filters')) {
      const url = new URL(form.action);
      url.search = new URLSearchParams(new FormData(form)).toString();
      const selected = new URL(root().dataset.detailUrl, location.href).searchParams.get('selected');
      if (selected) url.searchParams.set('selected', selected);
      await navigate(url.href, {mode: 'search'});
      return;
    }
    const url = event.submitter?.getAttribute('formaction') || form.action;
    const body = new FormData(form);
    const buttons = [...form.querySelectorAll('button')];
    posting = true;
    buttons.forEach(button => {button.disabled = true;});
    try { await navigate(url, {method: 'POST', body}); }
    finally {
      posting = false;
      buttons.forEach(button => {button.disabled = false;});
      if (pendingPop) {
        const pending = pendingPop;
        pendingPop = null;
        history.replaceState(pending.state, '', pending.url);
        await navigate(pending.url, {pop: true, state: pending.state});
      }
    }
  });
  window.addEventListener('popstate', (event) => {
    // A POST must finish before its result is replaced by history navigation.
    if (posting) {
      pendingPop = {url: location.href, state: event.state};
      return;
    }
    navigate(location.href, {pop: true, state: event.state});
  });
  document.addEventListener('oa-upload-complete', async () => {
    const panel = root().querySelector('dialog[open]');
    const panelId = panel?.id;
    const reviewSource = panel?.querySelector('[data-review-src]')?.src;
    const pane = panel?.querySelector('[data-oa-tab].active')?.dataset.oaTab;
    savePosition();
    await navigate(root().dataset.detailUrl, {mode: 'history', pop: true, state: history.state});
    const refreshed = panelId && document.getElementById(panelId);
    if (refreshed) {
      if (pane) {
        const tab = refreshed.querySelector(`[data-oa-tab="${pane}"]`);
        if (tab) activateTab(tab);
      }
      if (!refreshed.open) refreshed.showModal();
      const reviewFrame = refreshed.querySelector('[data-review-src]');
      if (reviewFrame) reviewFrame.src = reviewSource || reviewFrame.dataset.reviewSrc;
      document.documentElement.classList.add('oa-panel-open');
    }
  });
  window.addEventListener('pagehide', savePosition);
  // Persist scroll into the current history entry without waiting for another click.
  let frame;
  document.addEventListener('scroll', () => {
    if (!frame) frame = requestAnimationFrame(() => {frame = null; savePosition();});
  }, true);
  if (history.state?.oaList !== undefined) restore(history.state);
  else savePosition();
  enhance();
})();
