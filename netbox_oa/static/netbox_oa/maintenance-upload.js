(() => {
  const form = document.querySelector('[data-upload-form]');
  const loading = document.querySelector('[data-upload-loading]');
  if (form && loading) {
    const submit = form.querySelector('button[type="submit"]');
    let uploading = false;
    form.addEventListener('submit', event => {
      if (event.defaultPrevented) return;
      if (uploading) {
        event.preventDefault();
        return;
      }
      uploading = true;
      form.setAttribute('aria-busy', 'true');
      loading.hidden = false;
      if (submit) submit.disabled = true;
    });
    window.addEventListener('pageshow', () => {
      uploading = false;
      form.removeAttribute('aria-busy');
      loading.hidden = true;
      if (submit) submit.disabled = false;
    });
  }
  document.querySelector('[data-upload-cancel]')?.addEventListener('click', event => {
    if (window.parent === window) return;
    event.preventDefault();
    window.parent.postMessage({type: 'oa-upload-cancel'}, location.origin);
  });
  const drop = document.querySelector('.oa-upload-drop');
  if (!drop) return;
  const input = drop.querySelector('input[type=file]');
  const selected = document.querySelector('.oa-upload-selected');
  const source = document.querySelector('.oa-upload-source select');
  const mode = document.querySelector('.oa-upload-mode select');
  const replacement = document.querySelector('.oa-upload-replacement select');
  const showFiles = () => {
    input.setCustomValidity('');
    const updating = mode?.value === 'replace';
    const pending = source ? [...source.options].filter(option =>
      option.textContent.includes('待上传用印件')) : [];
    selected.replaceChildren(...Array.from(input.files, (file, index) => {
      const item = document.createElement('li');
      item.textContent = file.name;
      if (source) {
        const choice = source.cloneNode(true);
        choice.disabled = false;
        choice.id = `upload-source-${index}`;
        choice.setAttribute('aria-label', `${file.name}对应原件`);
        choice.value = pending[index]?.value || source.value;
        item.append(choice);
      }
      if (replacement && updating) {
        const choice = replacement.cloneNode(true);
        choice.disabled = false;
        choice.id = `upload-replacement-${index}`;
        choice.setAttribute('aria-label', `${file.name}更新哪份材料`);
        choice.value = replacement.options[index]?.value || replacement.value;
        item.append(choice);
      }
      return item;
    }));
    if (replacement) {
      replacement.disabled = !updating || input.files.length > 0;
      replacement.closest('.oa-upload-replacement').hidden = !updating || input.files.length > 0;
    }
    if (source) {
      source.disabled = input.files.length > 0;
      source.closest('.oa-upload-source').hidden = source.disabled;
    }
  };
  input.addEventListener('change', showFiles);
  mode?.addEventListener('change', showFiles);
  drop.addEventListener('dragover', event => {
    event.preventDefault();
    drop.classList.add('dragging');
  });
  drop.addEventListener('dragleave', () => drop.classList.remove('dragging'));
  drop.addEventListener('drop', event => {
    event.preventDefault();
    drop.classList.remove('dragging');
    const transfer = new DataTransfer();
    for (const file of event.dataTransfer.files) {
      transfer.items.add(file);
      if (!input.multiple) break;
    }
    if (!input.multiple && event.dataTransfer.files.length > 1) {
      input.setCustomValidity('此类材料每次只能上传一个文件。');
      window.oaNotify?.('此类材料每次只能上传一个文件。');
      return;
    }
    input.files = transfer.files;
    showFiles();
  });
  showFiles();
  const page = document.querySelector('[data-upload-heading]');
  if (page && window.parent !== window) {
    const updateLayout = () => window.parent.postMessage({
      type: 'oa-upload-layout', title: page.dataset.uploadHeading,
      height: Math.ceil(page.getBoundingClientRect().height),
    }, location.origin);
    new ResizeObserver(updateLayout).observe(page);
    window.addEventListener('load', updateLayout);
    updateLayout();
  }
})();
