(() => {
  const container = document.querySelector('[data-billing-rows]');
  if (!container) return;
  const rules = JSON.parse(document.getElementById('oa-billing-rules').textContent);
  const initial = JSON.parse(document.getElementById('oa-billing-initial').textContent);
  const form = container.closest('form');
  const payload = form.querySelector('[name="lines"]');
  const status = form.querySelector('[data-billing-status]');
  const total = form.querySelector('[data-billing-total]');
  const refresh = form.querySelector('[data-billing-refresh]');
  const save = form.querySelector('[data-billing-save]');
  const contractLink = form.querySelector('[data-billing-contract-link]');
  let sequence = 0;
  let timer;

  function values() {
    return [...container.children].map(row => Object.fromEntries(
      [...row.querySelectorAll('[data-field]')].map(field => [field.dataset.field,
        field.type === 'checkbox' ? field.checked : field.value])
    ));
  }
  function sync() {
    payload.value = JSON.stringify(values());
    sequence += 1;
    clearTimeout(timer);
    total.textContent = '—';
    for (const row of container.children) {
      row.querySelector('[data-billing-price]').textContent = '小计：—；结算价格：—';
    }
    status.textContent = '正在核算…';
    status.hidden = false;
    timer = setTimeout(preview, 300);
  }
  async function preview() {
    const current = sequence;
    const url = new URL(form.action || location.href);
    url.searchParams.set('preview', '1');
    try {
      const response = await fetch(url, {
        method: 'POST', body: new FormData(form), credentials: 'same-origin',
      });
      const data = await response.json();
      if (current !== sequence) return;
      if (!response.ok) throw new Error(data.error || '计费核算失败。');
      data.lines.forEach((line, index) => {
        const unitPrice = line.rule?.method === 'fixed' ? '' : `核算单价：${line.unit_price} 元；`;
        container.children[index].querySelector('[data-billing-price]').textContent =
          `${unitPrice}小计：${line.gross} 元；扣费：${line.deduction} 元；结算价格：${line.net} 元`;
      });
      total.textContent = data.total;
      status.textContent = '';
      status.hidden = true;
    } catch (error) {
      if (current !== sequence) return;
      status.textContent = error.message || '暂时无法核算，请检查明细后重试。';
      status.hidden = false;
    }
  }
  function add(value = {}) {
    if (container.children.length >= Math.max(1000, initial.length)) return;
    const row = document.createElement('div');
    row.className = 'card mb-3';
    const body = document.createElement('div');
    body.className = 'card-body';
    row.append(body);
    const fields = document.createElement('div');
    fields.className = 'row g-3';
    function field(key, label, type, defaultValue = '') {
      const wrapper = document.createElement('label');
      wrapper.className = 'col-md-4 form-label';
      const text = document.createElement('span');
      text.textContent = label;
      const input = document.createElement(type === 'select' ? 'select' : 'input');
      input.className = type === 'select' ? 'form-select mt-1' : 'form-control mt-1';
      if (type !== 'select') input.type = type;
      input.dataset.field = key;
      input.dataset.requiredMessage = type === 'select'
        ? (key === 'tier_index' ? '请选择规格值。' : '请选择合同计费条目。')
        : `请填写${label.split('（')[0]}。`;
      if (type === 'number') {input.min = '0'; input.step = key === 'deduction' ? '.01' : '.000001';}
      if (key !== 'tier_index' && type !== 'date') input.required = true;
      wrapper.append(text, input);
      fields.append(wrapper);
      input.value = value[key] ?? defaultValue;
      return input;
    }
    const select = field('rule_key', '合同计费条目', 'select');
    const blank = document.createElement('option');
    blank.value = ''; blank.textContent = '请选择计费条目'; select.append(blank);
    for (const rule of rules) {
      const option = document.createElement('option');
      option.value = rule.rule_key;
      const specification = typeof rule.specification === 'string' ? rule.specification.trim() : '';
      option.textContent = rule.group === 'materials'
        ? [rule.name, specification].filter(Boolean).join('/')
        : `${rule.code} · ${rule.name}`;
      select.append(option);
    }
    if (value.rule_key && !rules.some(rule => rule.rule_key === value.rule_key)) {
      const missing = document.createElement('option');
      missing.value = value.rule_key;
      missing.textContent = '原计费条目已失效，请重新选择';
      select.append(missing);
    }
    select.value = value.rule_key || '';
    for (const key of ['started_at', 'ended_at']) {
      field(key, key === 'started_at' ? '服务开始日期' : '服务结束日期', 'date').value =
        (value[key] || '').slice(0, 10);
    }
    const spec = field('tier_index', '规格值', 'select');
    const specWrapper = fields.children[fields.children.length - 1];
    const specCaption = specWrapper.children[0];
    const unitPrice = document.createElement('input');
    unitPrice.type = 'text';
    unitPrice.className = 'form-control mt-1';
    unitPrice.readOnly = true;
    unitPrice.disabled = true;
    unitPrice.hidden = true;
    specWrapper.append(unitPrice);
    field('quantity', '数量（次／班次／台／km／个）', 'number', '1');
    field('deduction', '扣费金额（元）', 'number', '0');
    body.append(fields);
    if (container.dataset.routineMaintenanceAllowed === '1') {
      const deduction = fields.children[fields.children.length - 1];
      const deductionGroup = document.createElement('div');
      deductionGroup.className = 'col-md-4 form-label d-flex align-items-end gap-3';
      deduction.className = 'form-label flex-grow-1 mb-0';
      deduction.remove();
      deductionGroup.append(deduction);
      fields.append(deductionGroup);
      const label = document.createElement('label');
      label.className = 'form-check mb-2 flex-shrink-0';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.className = 'form-check-input';
      checkbox.dataset.field = 'routine_maintenance';
      checkbox.checked = value.routine_maintenance === true;
      const caption = document.createElement('span');
      caption.className = 'form-check-label';
      caption.textContent = '日常代维';
      label.append(checkbox, caption);
      deductionGroup.append(label);
    }
    const description = document.createElement('p');
    description.className = 'text-muted mt-3'; body.append(description);
    function describe(restoring = false) {
      const rule = rules.find(item => item.rule_key === select.value);
      const fixed = rule?.method === 'fixed';
      specCaption.textContent = fixed ? '单价（元）' : '规格值';
      spec.hidden = fixed;
      unitPrice.hidden = !fixed;
      unitPrice.value = fixed ? String(rule.unit_price ?? '未配置') : '';
      spec.required = rule?.method === 'tiered';
      spec.disabled = !spec.required;
      spec.replaceChildren();
      const placeholder = document.createElement('option');
      placeholder.value = ''; placeholder.textContent = spec.required ? '请选择规格值' : '不适用';
      spec.append(placeholder);
      if (spec.required) {
        for (const [index, tier] of (rule.tiers || []).entries()) {
          const option = document.createElement('option');
          option.value = String(index);
          option.textContent = `${tier.label}（${tier.unit_price ?? '未配置'} 元）`;
          spec.append(option);
        }
      }
      spec.value = '';
      if (restoring && spec.required) {
        if (value.tier_index !== undefined) {
          spec.value = String(value.tier_index);
        } else if (value.specification !== undefined && value.specification !== '') {
          const actual = Number(value.specification);
          const matches = (rule.tiers || []).map((tier, index) => ({tier, index})).filter(({tier}) =>
            Number.isFinite(actual)
            && (actual > Number(tier.min) || (actual === Number(tier.min) && tier.min_inclusive))
            && (tier.max == null || actual < Number(tier.max)
              || (actual === Number(tier.max) && tier.max_inclusive)));
          if (matches.length === 1) spec.value = String(matches[0].index);
        }
      }
      description.textContent = !rule ? '请先选择合同计费条目。'
        : rule.method === 'fixed' ? `合同单价：${rule.unit_price ?? '未配置'} 元；服务时间仅记录，按数量计费。`
        : rule.method === 'duration' ? `首 ${rule.base_hours ?? '—'} 小时 ${rule.base_price ?? '—'} 元，超时 ${rule.overtime_price ?? '—'} 元/小时；${rule.overtime_rounding === 'ceil_hour' ? '超时按整小时向上取整' : rule.overtime_rounding === 'pro_rata' ? '超时按实际时长' : '超时规则待确认'}。`
        : `按规格值匹配合同档位：${(rule.tiers || []).map(tier => `${tier.label}：${tier.unit_price ?? '未配置'} 元`).join('；')}`;
    }
    select.addEventListener('change', () => describe());
    const result = document.createElement('p'); result.dataset.billingPrice = ''; body.append(result);
    const remove = document.createElement('button');
    remove.type = 'button'; remove.className = 'btn btn-outline-danger'; remove.textContent = '删除条目';
    remove.addEventListener('click', () => {row.remove(); sync();}); body.append(remove);
    container.append(row);
    describe(true);
  }
  for (const line of initial) add(line);
  if (!initial.length) add();
  form.querySelector('[data-billing-add]').addEventListener('click', () => {add(); sync();});
  container.addEventListener('input', sync);
  container.addEventListener('change', sync);
  refresh?.addEventListener('click', async () => {
    refresh.disabled = true;
    const wasDisabled = save?.disabled;
    if (save) save.disabled = true;
    sequence += 1;
    clearTimeout(timer);
    status.textContent = '正在刷新收费标准…';
    status.hidden = false;
    const url = new URL(form.action || location.href);
    url.searchParams.delete('preview');
    url.searchParams.set('refresh_pricing', '1');
    try {
      const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store'});
      const data = await response.json();
      if (!response.ok || !Array.isArray(data.rules)) {
        throw new Error(data.error || '刷新收费标准失败。');
      }
      const current = values();
      rules.splice(0, rules.length, ...data.rules);
      container.dataset.routineMaintenanceAllowed = data.routine_maintenance_allowed ? '1' : '0';
      container.replaceChildren();
      for (const line of current) add(line);
      if (contractLink) contractLink.hidden = rules.length > 0;
      if (save) save.disabled = !rules.length;
      sync();
    } catch (error) {
      if (save) save.disabled = wasDisabled;
      status.textContent = error.message || '刷新收费标准失败，请重试。';
      status.hidden = false;
    } finally {
      refresh.disabled = false;
    }
  });
  form.addEventListener('submit', () => {payload.value = JSON.stringify(values());});
  sync();
})();
