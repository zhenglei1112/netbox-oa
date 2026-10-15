(() => {
  const form = document.querySelector('[data-signature-canvas-form]');
  if (form) {
    const canvas = form.querySelector('canvas'), ctx = canvas.getContext('2d');
    const area = form.querySelector('.signature-canvas-area');
    if (area) {
      const fitCanvas = () => {
        const rect = area.getBoundingClientRect();
        const width = Math.min(rect.width, rect.height * 16 / 9);
        canvas.style.width = `${width}px`;
        canvas.style.height = `${width * 9 / 16}px`;
      };
      fitCanvas();
      // Change only display dimensions; the bitmap and existing ink stay intact.
      new ResizeObserver(fitCanvas).observe(area);
    }
    let pointer = null, ink = false;
    const point = event => {
      const rect = canvas.getBoundingClientRect();
      return [(event.clientX - rect.left) * canvas.width / rect.width,
        (event.clientY - rect.top) * canvas.height / rect.height];
    };
    ctx.lineWidth = 5; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    canvas.addEventListener('pointerdown', event => {
      if (pointer !== null) return;
      event.preventDefault(); pointer = event.pointerId; canvas.setPointerCapture(pointer);
      const [x,y] = point(event); ctx.beginPath(); ctx.moveTo(x,y); ctx.lineTo(x+0.1,y); ctx.stroke(); ink = true;
    });
    canvas.addEventListener('pointermove', event => {
      if (event.pointerId !== pointer) return;
      event.preventDefault(); ctx.lineTo(...point(event)); ctx.stroke();
    });
    for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
      canvas.addEventListener(type, event => {if (event.pointerId === pointer) pointer = null;});
    }
    form.querySelector('[data-signature-clear]').addEventListener('click', () => {
      ctx.clearRect(0,0,canvas.width,canvas.height); ink = false; pointer = null;
    });
    form.addEventListener('submit', event => {
      if (!ink) {event.preventDefault(); form.querySelector('[data-canvas-error]').textContent = '请先书写完整名字。'; return;}
      form.elements.image_data.value = canvas.toDataURL('image/png');
      form.querySelector('button[type=submit]').disabled = true;
    });
  }
  const panel = document.querySelector('[data-signature-status]');
  if (panel) {
    const poll = async () => {
      try {
        const response = await fetch(panel.dataset.signatureStatus, {credentials:'same-origin', cache:'no-store'});
        if (!response.ok) throw new Error('status');
        const {state} = await response.json();
        const uploaded = state === 'uploaded' || state === 'confirmed';
        panel.querySelector('[data-signature-confirm]').disabled = !uploaded;
        panel.querySelector('[data-signature-message]').textContent = {
          waiting:'等待手机提交签名……', uploaded:'签名已上传，请核对后确认。',
          confirmed:'已确认签名。', invalid:'签字链接已失效，请返回工单重新生成二维码。'
        }[state];
        if (uploaded) {
          const image = panel.querySelector('[data-signature-preview]');
          if (image.hidden) {image.src += '?preview=1'; image.hidden = false;}
        }
        if (state !== 'waiting') return;
      } catch (_) {panel.querySelector('[data-signature-message]').textContent = '连接中断，正在重试……';}
      setTimeout(poll, 3000);
    };
    poll();
  }
})();
