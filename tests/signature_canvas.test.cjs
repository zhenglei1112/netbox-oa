const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('mobile canvas captures strokes, clears ink and submits a PNG only after drawing', () => {
  const handlers = {}, strokes = [];
  let rect = {left:10,top:20,width:500,height:200,right:510};
  let available = {width:360,height:550}, resized;
  const area = {getBoundingClientRect:()=>available};
  const context = {beginPath(){}, moveTo(...p){strokes.push(p);},
    lineTo(...p){strokes.push(p);}, stroke(){}, clearRect(){}};
  const canvas = {width:1000, height:400, style:{}, getContext:()=>context,
    getBoundingClientRect:()=>rect,
    setPointerCapture(){}, toDataURL:()=> 'data:image/png;base64,fixture',
    addEventListener:(name, fn)=>handlers[name]=fn};
  const clear = {}, error = {}, submit = {};
  clear.addEventListener = (_, fn)=>clear.click=fn;
  const form = {elements:{image_data:{}}, querySelector: selector => ({
    canvas, '.signature-canvas-area':area, '[data-signature-clear]':clear, '[data-canvas-error]':error,
    'button[type=submit]':submit,
  })[selector], addEventListener:(_,fn)=>form.submit=fn};
  const document = {querySelector: selector => selector === '[data-signature-canvas-form]' ? form : null};
  vm.runInNewContext(fs.readFileSync('netbox_oa/static/netbox_oa/maintenance-signature.js','utf8'), {
    document, ResizeObserver:class{constructor(callback){resized=callback;} observe(){}}
  });
  assert.equal(canvas.style.width,'360px');
  assert.equal(canvas.style.height,'202.5px');
  let prevented = false;
  form.submit({preventDefault(){prevented=true;}});
  assert.equal(prevented,true);
  handlers.pointerdown({pointerId:1, clientX:60,clientY:70, preventDefault(){}});
  handlers.pointermove({pointerId:1, clientX:110,clientY:120, preventDefault(){}});
  handlers.pointerup({pointerId:1});
  assert.deepEqual(strokes[0], [100,100]);
  assert.deepEqual(strokes.at(-1), [200,200]);
  // Device rotation changes the display size without clearing the bitmap.
  available = {width:800,height:300};
  resized();
  assert.equal(parseFloat(canvas.style.height),300);
  assert.ok(Math.abs(parseFloat(canvas.style.width)/parseFloat(canvas.style.height)-16/9)<0.00001);
  assert.equal(canvas.width,1000);
  assert.equal(canvas.height,400);
  rect = {left:10,top:20,width:800,height:300,right:810};
  handlers.pointerdown({pointerId:2, clientX:170,clientY:170, preventDefault(){}});
  assert.deepEqual(strokes.at(-2), [200,200]);
  handlers.pointerup({pointerId:2});
  form.submit({preventDefault(){throw Error('unexpected prevention');}});
  assert.equal(form.elements.image_data.value,'data:image/png;base64,fixture');
  assert.equal(submit.disabled,true);
  clear.click();
  prevented=false;
  form.submit({preventDefault(){prevented=true;}});
  assert.equal(prevented,true);
});
