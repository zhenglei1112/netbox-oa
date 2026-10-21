const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('add opens QR, uploaded signature closes QR, and delete restores add', async () => {
  let click;
  const calls = [];
  const add = {hidden:false}, value = {hidden:true};
  const image = {removeAttribute(name){delete this[name];}};
  const qr = {}, feedback = {}, hint = {}, reuse = {};
  const dialog = {showModal(){this.open=true;}, close(){this.open=false;}};
  const widget = {isConnected:true, dataset:{createUrl:'/create/'},
    closest:()=>({querySelector:()=>({value:'csrf'})}),
    querySelector: selector => ({'[data-signature-add]':add,
      '[data-signature-value]':value,'[data-signature-image]':image,
      '[data-signature-feedback]':feedback,'[data-signature-qr]':qr,
      '[data-signature-hint]':hint,'[data-signature-dialog]':dialog,'[data-signature-reuse]':reuse})[selector]};
  const button = attr => ({closest:()=>widget, hasAttribute:name=>name===attr});
  const payload = {qr:'data:image/svg+xml;base64,qr', status_url:'/status/',
    action_url:'/action/',image_url:'/image/'};
  const results = [payload,{state:'uploaded'},{state:'empty'}];
  vm.runInNewContext(fs.readFileSync('netbox_oa/static/netbox_oa/maintenance-signature-widget.js','utf8'), {
    document:{addEventListener:(name,fn)=>{if(name==='click') click=fn;}}, FormData, clearTimeout, setTimeout:()=>0,
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
  results.push({...payload,has_saved_signature:true},{state:'waiting'},{state:'uploaded'});
  await click({target:{closest:()=>button('data-signature-add')}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(dialog.open,true);
  assert.equal(reuse.hidden,false);
  await click({target:{closest:()=>button('data-signature-reuse')}});
  assert.equal(calls.at(-1)[0],'/action/');
  assert.equal(calls.at(-1)[1].body.get('operation'),'reuse');
  assert.equal(widget.dataset.signed,'1');
  assert.equal(image.src,'/image/');
  assert.equal(dialog.open,false);
});


test('draft signing saves the whole form, changes to edit URL, and editing requires signing again', async () => {
  const listeners = {}, requests = [];
  let addClicks = 0, prevented = false;
  const add = {hidden:false, click(){addClicks++;}}, value = {hidden:true};
  const image = {removeAttribute(name){delete this[name];}}, feedback = {}, qr = {}, hint = {};
  const dialog = {showModal(){this.open=true;}, close(){this.open=false;}};
  const expected = {value:''}, policy = {}, another = {};
  const form = {action:'/add/', reportValidity:()=>true,
    querySelector:selector=>({'[name=csrfmiddlewaretoken]':{value:'csrf'},
      '[name=expected_document_id]':expected,'[name=policy]':policy,'[name=_addanother]':another,
      '[data-save-form="true"]':widget})[selector]};
  const widget = {isConnected:true, dataset:{createUrl:'/add/',saveForm:'true',signed:'0'},
    closest:()=>form,
    querySelector:selector=>({'[data-signature-add]':add,'[data-signature-value]':value,
      '[data-signature-image]':image,'[data-signature-feedback]':feedback,
      '[data-signature-qr]':qr,'[data-signature-hint]':hint,'[data-signature-dialog]':dialog,'[data-signature-reuse]':{}})[selector]};
  const results = [{qr:'qr',status_url:'/status/',action_url:'/signature/',image_url:'/image/',
    form_action:'/edit/7/',document_id:9},{state:'uploaded'}];
  class FormDataMock {
    constructor(source){this.source=source;this.fields={};}
    set(key,value){this.fields[key]=value;}
  }
  vm.runInNewContext(fs.readFileSync('netbox_oa/static/netbox_oa/maintenance-signature-widget.js','utf8'), {
    document:{addEventListener:(name,fn)=>listeners[name]=fn}, FormData:FormDataMock,
    clearTimeout,setTimeout,history:{replaceState(){},state:{}},
    fetch:async(url,options)=>{requests.push([url,options]);return {ok:true,json:async()=>results.shift()};}
  });
  const button = {closest:()=>widget,hasAttribute:name=>name==='data-signature-add'};
  await listeners.click({target:{closest:()=>button}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests[0][1].body.source,form);
  assert.equal(requests[0][1].body.fields._signature,'1');
  assert.equal(form.action,'/edit/7/');
  assert.equal(widget.dataset.createUrl,'/edit/7/');
  assert.equal(expected.value,9);
  assert.equal(policy.disabled,true);
  assert.notEqual(another.hidden,true);
  assert.equal(widget.dataset.signed,'1');
  listeners.input({target:{closest:selector=>selector==='form'?form:null}});
  assert.equal(widget.dataset.signed,'0');
  assert.equal(add.hidden,false);
  listeners.submit({target:form,submitter:{name:'_submit'},preventDefault(){prevented=true;}});
  assert.equal(prevented,true);
  assert.equal(addClicks,1);
});
