(() => {
  const diagram = document.querySelector('.oa-flow');
  if (!diagram) return;
  const svg = diagram.querySelector('svg');
  const width = svg.viewBox.baseVal.width;
  const height = svg.viewBox.baseVal.height;
  let scale = 1;
  diagram.querySelector('.oa-flow-controls').hidden = false;
  diagram.querySelector('[data-flow-returns]').addEventListener('change', (event) => {
    diagram.classList.toggle('hide-returns', !event.target.checked);
  });
  diagram.querySelectorAll('[data-flow-zoom]').forEach((button) => {
    button.addEventListener('click', () => {
      const action = button.dataset.flowZoom;
      scale = action === 'reset' ? 1 : Math.min(1.5, Math.max(0.5, scale + (action === 'in' ? 0.1 : -0.1)));
      svg.setAttribute('width', width * scale);
      svg.setAttribute('height', height * scale);
      diagram.querySelector('[data-flow-scale]').value = `${Math.round(scale * 100)}%`;
    });
  });
})();
