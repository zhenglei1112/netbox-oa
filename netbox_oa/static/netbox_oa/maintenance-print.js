(() => {
  const button = document.getElementById('print-document');
  let codes = [];
  let feedback;
  const clear = () => {
    for (const code of codes) code.remove();
    codes = [];
  };
  window.addEventListener('afterprint', clear);
  button.addEventListener('click', async () => {
    clear();
    feedback?.remove();
    button.disabled = true;
    try {
      for (const template of document.querySelectorAll('.print-code-template')) {
        const code = template.content.firstElementChild.cloneNode(true);
        template.before(code);
        codes.push(code);
      }
      await Promise.all(codes.flatMap(code => [...code.querySelectorAll('img')])
        .map(image => image.decode()));
      window.print();
    } catch {
      clear();
      feedback = document.createElement('span');
      feedback.setAttribute('role', 'alert');
      feedback.textContent = '打印码加载失败，请重新点击打印。';
      button.after(feedback);
    } finally {
      button.disabled = false;
    }
  });
})();
