const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
function element(tag='div'){
 return {tag,value:'',files:[],disabled:false,checked:false,dataset:{},children:[],handlers:{},
  addEventListener(event,fn){this.handlers[event]=fn;},setAttribute(){},append(...items){this.children.push(...items);},replaceChildren(){this.children=[];},
  querySelectorAll(){const all=[];function walk(e){for(const child of e.children){if(child.tag==='input'||child.tag==='button')all.push(child);walk(child);}}walk(this);return all;}};
}
async function setup(){
 const ids=['chat','message','send','status','history','clear','documents','document-list','document-status','document-file','document-upload','document-refresh'];
 const elements=Object.fromEntries(ids.map(id=>[id,element()]));
 const calls=[],docs=[],deletions=[],prompts=[];let fail=false;
 const context=vm.createContext({document:{querySelector:id=>elements[id.slice(1)],createElement:tag=>element(tag)},TextEncoder,AbortController,setTimeout,clearTimeout,
  confirm:message=>{prompts.push(message);return true;},fetch:async(url,options={})=>{
   calls.push({url,options});let data={};let status=200;
   if(url==='/api/session')data={csrf:'dummy'};
   else if(url==='/api/documents')data={documents:docs.map(d=>({...d})),deletions:deletions.map(d=>({...d}))};
   else if(url.startsWith('/api/documents?')){
    assert.equal(options.headers['X-CSRF-Token'],'dummy');
    if(fail){status=502;data={detail:'network'};}
    else{data={id:'file-a',filename:'<img onerror=bad>.txt',status:'in_progress',selectable:false,can_delete_original:true};docs.push(data);}
   }else if(url.endsWith('/delete')){assert.equal(options.headers['X-CSRF-Token'],'dummy');docs.splice(0);data={detached:true,original_deleted:true};}
   else if(url==='/api/chat')data={response:'answer',sources:[{file_id:'file-a',filename:'<script>bad</script>.txt'}]};
   return {ok:status===200,status,json:async()=>data};
  }});
 for(const file of ['documents.js','ui.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../local_web/static',file),'utf8'),context);
 await new Promise(resolve=>setImmediate(resolve));
 return {elements,calls,docs,deletions,prompts,fail:()=>{fail=true;},click:async(id)=>elements[id].handlers.click(),send:async(text)=>{elements.message.value=text;await elements.chat.handlers.submit({preventDefault(){}});}};
}
test('UI upload, processing, selection, filtered chat, safe sources and deletion',async()=>{
 const ui=await setup();ui.elements['document-file'].files=[{name:'report.txt',size:100}];
 await ui.click('document-upload');
 let row=ui.elements['document-list'].children[0];assert.equal(row.children[0].disabled,true);
 ui.docs[0].status='completed';ui.docs[0].selectable=true;await ui.click('document-refresh');
 row=ui.elements['document-list'].children[0];assert.equal(row.children[1].textContent,'<img onerror=bad>.txt — completed');
 row.children[0].checked=true;row.children[0].handlers.change();await ui.send('question');
 const sent=ui.calls.filter(c=>c.url==='/api/chat');assert.deepEqual(JSON.parse(sent[0].options.body).document_ids,['file-a']);
 assert.equal(ui.elements.history.children.at(-1).textContent,'出典: <script>bad</script>.txt');
 assert.equal(ui.elements.history.children.at(-1).children.length,0);
 await row.children[3].handlers.click();assert.equal(ui.elements['document-list'].children.length,0);
 await ui.send('normal');const payload=JSON.parse(ui.calls.filter(c=>c.url==='/api/chat').at(-1).options.body);
 assert.equal(payload.document_ids,undefined);assert.deepEqual(payload.history,[]);
 assert.equal(ui.prompts.length,2);
});
test('UI file rejection and upload failure are visible',async()=>{
 const ui=await setup();ui.elements['document-file'].files=[{name:'bad.exe',size:1}];await ui.click('document-upload');
 assert.equal(ui.calls.some(c=>c.url.startsWith('/api/documents?')),false);
 ui.elements['document-file'].files=[{name:'a.txt',size:100}];ui.fail();await ui.click('document-upload');
 assert.match(ui.elements['document-status'].textContent,/502/);
});
test('refresh removes no-longer-ready selection and clears old context',async()=>{
 const ui=await setup();ui.docs.push({id:'file-a',filename:'a.txt',status:'completed',selectable:true});await ui.click('document-refresh');
 const check=ui.elements['document-list'].children[0].children[0];check.checked=true;check.handlers.change();await ui.send('q');
 ui.docs[0].status='failed';ui.docs[0].selectable=false;await ui.click('document-refresh');await ui.send('next');
 const payload=JSON.parse(ui.calls.filter(c=>c.url==='/api/chat').at(-1).options.body);
 assert.equal(payload.document_ids,undefined);assert.deepEqual(payload.history,[]);
});

test('persisted unfinished deletion is displayed safely and resumes only on click',async()=>{
 const ui=await setup();
 ui.deletions.push({id:'file-a',name:'<script>unsafe</script>.txt',mode:'detach',state:'unconfirmed'});
 await ui.click('document-refresh');
 const row=ui.elements['document-list'].children[0];
 assert.equal(row.children[0].textContent,'<script>unsafe</script>.txt — 削除状態: unconfirmed');
 assert.equal(ui.calls.some(c=>c.url.endsWith('/delete')),false);
 await row.children[1].handlers.click();
 const request=ui.calls.find(c=>c.url.endsWith('/delete'));
 assert.deepEqual(JSON.parse(request.options.body),{mode:'detach',confirmation:'<script>unsafe</script>.txt'});
});

test('CSV is accepted and conversion is explained before upload',async()=>{
 const ui=await setup();ui.elements['document-file'].files=[{name:'inventory.CSV',size:120}];
 await ui.click('document-upload');
 assert.match(ui.prompts[0],/CSVはサーバーで検証/);
 assert.match(ui.prompts[0],/元名.csv.txt/);
 const call=ui.calls.find(c=>c.url.startsWith('/api/documents?'));
 assert.equal(call.url,'/api/documents?filename=inventory.CSV');
 assert.equal(call.options.headers['Content-Type'],'application/octet-stream');
 assert.equal(call.options.headers['X-CSRF-Token'],'dummy');
});

test('oversized and empty CSVs are rejected before upload',async()=>{
 const ui=await setup();
 for(const size of [0,5*1024*1024+1]){
  ui.elements['document-file'].files=[{name:'inventory.csv',size}];await ui.click('document-upload');
 }
 assert.equal(ui.calls.some(c=>c.url.startsWith('/api/documents?')),false);
 assert.equal(ui.prompts.length,0);
});
