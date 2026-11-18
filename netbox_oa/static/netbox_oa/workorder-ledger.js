(() => {
  let controller;
  let version = 0;
  let timer;

  function cancel() {
    version++;
    controller?.abort();
    document.getElementById('oa-ledger-totals')?.removeAttribute('aria-busy');
  }

  async function refresh() {
    const totals = document.getElementById('oa-ledger-totals');
    const list = document.getElementById('object_list');
    if (!totals || !list) return;
    cancel();
    const current = version;
    controller = new AbortController();
    const url = new URL(location.href);
    url.searchParams.set('_ledger_totals', '1');
    url.searchParams.delete('_selected');
    url.searchParams.delete('_ledger_all');
    const quicksearch = document.getElementById('quicksearch');
    if (quicksearch) {
      if (quicksearch.value) url.searchParams.set('q', quicksearch.value);
      else url.searchParams.delete('q');
    }
    const form = list.closest('form');
    const all = form?.querySelector('input[name="_all"]:checked');
    if (all) url.searchParams.set('_ledger_all', '1');
    else list.querySelectorAll('input[name="pk"]:checked').forEach(input => {
      url.searchParams.append('_selected', input.value);
    });
    totals.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(url, {signal: controller.signal, credentials: 'same-origin'});
      if (!response.ok) throw new Error('Totals request failed');
      const html = await response.text();
      if (current !== version || !totals.isConnected) return;
      const fresh = new DOMParser().parseFromString(html, 'text/html')
        .getElementById('oa-ledger-totals');
      const values = fresh?.querySelectorAll('.btn');
      const blocks = totals.querySelectorAll('.btn');
      if (!values || values.length !== blocks.length) throw new Error('Invalid totals response');
      blocks.forEach((block, index) => { block.textContent = values[index].textContent; });
      totals.setAttribute('aria-label', '查询结果合计');
      totals.removeAttribute('title');
    } catch (error) {
      if (error.name !== 'AbortError' && current === version && totals.isConnected) {
        const message = '统计更新失败，请重新勾选或刷新重试';
        totals.setAttribute('aria-label', message);
        totals.setAttribute('title', message);
      }
    } finally {
      if (current === version) totals.removeAttribute('aria-busy');
    }
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(refresh, 0);
  }

  document.addEventListener('change', event => {
    if (event.target.matches('input[type="checkbox"][name="pk"], input.toggle, input[name="_all"]')) {
      schedule();
    }
  });
  document.addEventListener('htmx:beforeRequest', cancel);
  document.addEventListener('htmx:afterSettle', schedule);
})();
