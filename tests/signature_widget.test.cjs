const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('add opens QR, uploaded signature closes QR, and delete restores add', async () => {
  let click;
  const calls = [];
  const add = {hidden:false}, value = {hidden:true};
  const image = {removeAttribute(name){delete this[name];}};
  const qr = {}, feedback = {}, hint = {};
  const dialog = {showModal(){this.open=true;}, close(){this.open=false;}};
  const widget = {isConnected:true, dataset:{createUrl:'/create/'},
    closest:()=>({querySelector:()=>({value:'csrf'})}),
    querySelector: selector => ({'[data-signature-add]':add,
      '[data-signature-value]':value,'[data-signature-image]':image,
      '[data-signature-feedback]':feedback,'[data-signature-qr]':qr,
      '[data-signature-hint]':hint,'[data-signature-dialog]':dialog})[selector]};
  const button = attr => ({closest:()=>widget, hasAttribute:name=>name===attr});
  const payload = {qr:'data:image/svg+xml;base64,qr', status_url:'/status/',
    action_url:'/action/',image_url:'/image/'};
  const results = [payload,{state:'uploaded'},{state:'empty'}];
  vm.runInNewContext(fs.readFileSync('netbox_oa/static/netbox_oa/maintenance-signature-widget.js','utf8'), {
    document:{addEventListener:(_,fn)=>click=fn}, FormData, clearTimeout, setTimeout,
    fetch:async(url,options)=>{calls.push([url,options]); return {ok:true,json:async()=>results.shift()};}
  });
  await click({target:{closest:()=>button('data-signature-add')}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(qr.src,payload.qr);
  assert.equal(dialog.open,false);
  assert.equal(add.hidden,true);
  assert.equal(value.hidden,false);
  assert.equal(image.src,'/image/');
  await click({target:{closest:()=>button('data-signature-delete')}});
  assert.equal(add.hidden,false);
  assert.equal(value.hidden,true);
  assert.equal(image.src,undefined);
  assert.equal(calls[0][1].body.get('csrfmiddlewaretoken'),'csrf');
  assert.equal(calls[2][1].body.get('operation'),'cancel');
});
