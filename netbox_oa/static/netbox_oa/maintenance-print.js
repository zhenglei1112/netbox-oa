(() => {
  document.getElementById('close-print-window')?.addEventListener('click', () => window.close());
  const button = document.getElementById('print-document');
  const settings = button?.dataset.auditUrl ? button.dataset : document.body.dataset;
  if (!settings.auditUrl) return;
  let codes = [];
  let printRequest;
  const clear = () => {
    for (const code of codes) code.remove();
    codes = [];
  };
  function report(text) {
    window.oaNotify?.(text);
  }
  function recordPrint() {
    if (!printRequest) {
      printRequest = fetch(settings.auditUrl, {
        method: 'POST', credentials: 'same-origin', keepalive: true,
        headers: {'X-CSRFToken': settings.csrfToken},
      }).then(response => {
        if (!response.ok) throw new Error('无法保存打印操作记录');
      }).catch(error => {printRequest = undefined; throw error;});
    }
    return printRequest;
  }
  window.addEventListener('afterprint', () => {clear(); printRequest = undefined;});
  // Covers the browser's print shortcut on this HTML page as well as the button.
  window.addEventListener('beforeprint', () => recordPrint().catch(() => {
    report('打印操作记录保存失败，请检查登录状态后重试。');
  }));
  button?.addEventListener('click', async () => {
    clear();
    button.disabled = true;
    let errorText = '印章、签名或打印码加载失败，请重新点击打印。';
    try {
      for (const template of document.querySelectorAll('.print-code-template')) {
        const code = template.content.firstElementChild.cloneNode(true);
        template.before(code);
        codes.push(code);
      }
      await Promise.all([
        ...codes.flatMap(code => [...code.querySelectorAll('img')]),
        ...document.querySelectorAll('.electronic-seal, .handwritten-signature'),
      ]
        .map(image => image.decode()));
      errorText = '打印操作记录保存失败，请检查登录状态后重试。';
      await recordPrint();
      errorText = '无法打开打印窗口，请重新点击打印。';
      window.print();
    } catch {
      clear();
      report(errorText);
    } finally {
      button.disabled = false;
    }
  });
})();
