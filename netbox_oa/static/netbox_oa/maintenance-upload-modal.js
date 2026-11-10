(() => {
  if (window.oaOpenUpload) return;
  let dialog;
  let frame;
  let opener;
  let header;
  let title;
  let contentHeight;
  const resize = () => {
    if (!dialog?.open || !contentHeight) return;
    const available = Math.max(0, window.innerHeight * .9 - header.offsetHeight - 2);
    const height = Math.min(contentHeight, available);
    frame.style.height = `${height}px`;
    dialog.style.height = `${height + header.offsetHeight + 2}px`;
  };
  window.oaOpenUpload = (href, trigger) => {
    const url = new URL(href, location.href);
    if (url.origin !== location.origin) return;
    if (window.parent !== window && typeof window.parent.oaOpenUpload === 'function') {
      window.parent.oaOpenUpload(url.href, trigger);
      return;
    }
    if (!dialog) {
      dialog = document.createElement('dialog');
      dialog.className = 'oa-upload-modal';
      dialog.setAttribute('aria-label', '上传文件');
      header = document.createElement('header');
      header.className = 'oa-upload-modal-header';
      title = document.createElement('strong');
      title.textContent = '上传文件';
      header.append(title);
      frame = document.createElement('iframe');
      frame.title = '上传材料';
      dialog.append(header, frame);
      document.body.append(dialog);
      dialog.addEventListener('close', () => {
        frame.src = 'about:blank';
        document.documentElement.classList.remove('oa-upload-modal-open');
        if (opener?.isConnected) opener.focus();
      });
    }
    opener = trigger;
    title.textContent = '上传文件';
    contentHeight = null;
    dialog.style.height = 'fit-content';
    frame.style.height = '400px';
    url.searchParams.set('embed', '1');
    frame.src = url.href;
    if (!dialog.open) dialog.showModal();
    document.documentElement.classList.add('oa-upload-modal-open');
  };
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-oa-upload]');
    if (!link || event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    window.oaOpenUpload(link.href, link);
  });
  window.addEventListener('message', event => {
    if (event.origin !== location.origin || !dialog?.open || event.source !== frame.contentWindow) return;
    if (event.data?.type === 'oa-upload-layout') {
      if (typeof event.data.title === 'string') {
        title.textContent = event.data.title;
        dialog.setAttribute('aria-label', event.data.title);
      }
      if (Number.isFinite(event.data.height) && event.data.height > 0) {
        contentHeight = Math.ceil(event.data.height);
        resize();
      }
      return;
    }
    if (!['oa-upload-cancel', 'oa-upload-complete'].includes(event.data?.type)) return;
    dialog.close();
    if (event.data.type === 'oa-upload-complete') {
      window.oaNotify?.('材料上传成功。', 'success');
      if (document.querySelector('.oa-inbox')) {
        document.dispatchEvent(new CustomEvent('oa-upload-complete'));
      } else {
        location.reload();
      }
    }
  });
  window.addEventListener('resize', resize);
})();
