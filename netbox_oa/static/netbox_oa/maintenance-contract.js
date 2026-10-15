(() => {
  const options = document.getElementById('oa-contract-options');
  const select = document.getElementById('id_contract_id');
  if (!options || !select) return;
  const contracts = JSON.parse(options.textContent);
  const update = (contractChanged = false) => {
    const contract = contracts[select.value] || {};
    const supplier = document.getElementById('id_supplier');
    if (supplier) supplier.value = contract.supplier || '';
    const region = document.getElementById('id_service_region');
    if (region && (contractChanged || (!region.selectedOptions.length && region.dataset?.oaPreserveEmpty !== '1'))) {
      const ids = (contract.service_region_ids || []).map(String);
      for (const option of region.options) option.selected = ids.includes(option.value);
      // Keep NetBox's enhanced select display in sync with the native selection.
      if (region.tomselect) region.tomselect.setValue(ids);
      else region.dispatchEvent(new Event('change', {bubbles: true}));
    }
  };
  select.addEventListener('change', () => update(true));
  update();
})();
