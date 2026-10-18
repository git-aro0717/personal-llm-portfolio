// No browser packages or network: execute the actual UI with a fake DOM/fetch.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
async function setup() {
  const elements = {};
  for (const id of ['chat','message','send','status','history','clear']) {
    elements[id] = {value:'',disabled:false,children:[],handlers:{},addEventListener(event,fn){this.handlers[event]=fn;},append(item){this.children.push(item);},replaceChildren(){this.children=[];}};
  }
  const calls = [];
  let code = 200, reply = 'こんにちは';
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../local_web/static/ui.js'),'utf8'), {
    document:{querySelector:selector=>elements[selector.slice(1)],createElement:()=>({})},
    TextEncoder,AbortController,setTimeout,clearTimeout,
    fetch:async (url,options)=>{
      if(url==='/api/session') return {ok:true,json:async()=>({csrf:'dummy'})};
      calls.push(JSON.parse(options.body));
      assert.equal(options.headers['X-CSRF-Token'],'dummy');
      return {ok:code===200,status:code,json:async()=>({response:reply})};
    }
  });
  await new Promise(resolve=>setImmediate(resolve));
  return {elements,calls,fail:()=>{code=502;},reply:value=>{reply=value;},
    send:async value=>{elements.message.value=value;await elements.chat.handlers.submit({preventDefault(){}});}};
}
test('successful turns are sent in order and clear removes model context', async()=>{
  const ui=await setup();await ui.send('私は花子');await ui.send('私の名前は？');
  assert.deepEqual(ui.calls[0].history,[]);
  assert.deepEqual(ui.calls[1].history,[{role:'user',content:'私は花子'},{role:'assistant',content:'こんにちは'}]);
  assert.equal(ui.elements.history.children[3].textContent,'こんにちは');
  ui.elements.clear.handlers.click();await ui.send('new');assert.deepEqual(ui.calls[2].history,[]);
});
test('failed turns never enter context and local invalid input is not sent', async()=>{
  const ui=await setup();await ui.send('a');ui.fail();await ui.send('failed');await ui.send('retry');
  assert.equal(ui.calls[2].history.length,2);await ui.send('x'.repeat(4001));assert.equal(ui.calls.length,3);
});
test('history and encoded requests stay bounded by trimming complete turns', async()=>{
  const ui=await setup();for(let i=0;i<13;i++)await ui.send('q'+i);
  assert.equal(ui.calls[12].history.length,20);
  assert.equal(ui.calls[12].history[0].role,'user');
  const large=await setup();large.reply('あ'.repeat(3000));await large.send('問'.repeat(3000));await large.send('次');
  assert.equal(large.calls[1].history.length,0);
  for(const body of large.calls)assert.ok(Buffer.byteLength(JSON.stringify(body))<=16384);
});
test('oversized assistant reply is displayed but resets future context', async()=>{
  const ui=await setup();ui.reply('a'.repeat(4001));await ui.send('q');await ui.send('next');
  assert.equal(ui.elements.history.children[1].textContent.length,4001);
  assert.deepEqual(ui.calls[1].history,[]);
});
