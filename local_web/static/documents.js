"use strict";
(() => {
  const rows=document.querySelector('#document-list');
  const note=document.querySelector('#document-status');
  const picker=document.querySelector('#document-file');
  const upload=document.querySelector('#document-upload');
  const refresh=document.querySelector('#document-refresh');
  let selected=new Set(), busy=false;
  globalThis.personalDocuments={selection:()=>Array.from(selected),setBusy:value=>{
    busy=value; upload.disabled=value;refresh.disabled=value;
    for(const control of rows.querySelectorAll('input,button'))control.disabled=value||control.dataset.unavailable==='true';
  }};
  function reset(){if(globalThis.onDocumentsChanged)globalThis.onDocumentsChanged();}
  async function call(url,options={}){
    const session=await fetch('/api/session',{cache:'no-store'});
    if(!session.ok)throw new Error('ローカル接続に失敗しました');
    const token=(await session.json()).csrf;
    const response=await fetch(url,{...options,headers:{...options.headers,'X-CSRF-Token':token}});
    const data=await response.json();
    if(!response.ok)throw new Error(`文書操作に失敗 (${response.status})：${data.detail||'通信エラー'}${data.file_id?' 元ファイルID: '+data.file_id:''}`);
    return data;
  }
  async function listing(){
    const data=await call('/api/documents');
    const ready=new Set(data.documents.filter(item=>item.selectable).map(item=>item.id));
    const kept=new Set([...selected].filter(id=>ready.has(id)));
    if(kept.size!==selected.size){selected=kept;reset();}
    rows.replaceChildren();
    for(const item of data.documents){
      const row=document.createElement('div'),check=document.createElement('input'),label=document.createElement('span');
      check.type='checkbox';check.checked=selected.has(item.id);check.disabled=!item.selectable;check.dataset.unavailable=String(!item.selectable);
      check.setAttribute('aria-label',`検索対象 ${item.filename}`);
      check.addEventListener('change',()=>{if(check.checked){if(selected.size>=20){check.checked=false;note.textContent='選択は20件までです';return;}selected.add(item.id);}else selected.delete(item.id);reset();});
      label.textContent=`${item.filename} — ${item.status}`;row.append(check,label);
      for(const mode of (item.can_delete_original?['detach','delete_original']:['detach'])){
        const button=document.createElement('button');button.type='button';button.textContent=mode==='detach'?'関連付け解除':'元ファイルも削除';
        button.addEventListener('click',async()=>{
          if(busy)return;
          const text=mode==='detach'?'Vector Storeから外します。元ファイルは残ります。':'OpenAIの元ファイルも削除します。他の利用先に影響する場合があります。';
          if(!confirm(`${item.filename}\n${text}`))return;
          globalThis.personalDocuments.setBusy(true);
          // Drop selected context before deletion, even when original deletion fails.
          selected.delete(item.id);reset();
          try{const result=await call(`/api/documents/${encodeURIComponent(item.id)}/delete`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,confirmation:item.filename})});await listing();note.textContent=result.state==='unconfirmed'?'削除状態が未確認です。検索対象から除外しました。':'削除処理が完了しました';}
          catch(error){note.textContent=error.message;}
          finally{globalThis.personalDocuments.setBusy(false);}
        });row.append(button);
      }
      rows.append(row);
    }
    for(const item of data.deletions||[]){
      const row=document.createElement('div'),label=document.createElement('span');
      label.textContent=`${item.name} — 削除状態: ${item.state}`;row.append(label);
      if(item.state!=='complete'){
        const resume=document.createElement('button');resume.type='button';resume.textContent='削除状態を照合して再開';
        resume.addEventListener('click',async()=>{
          if(busy||!confirm(`${item.name}\n削除状態を照合して処理を再開します。`))return;
          globalThis.personalDocuments.setBusy(true);
          try{await call(`/api/documents/${encodeURIComponent(item.id)}/delete`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:item.mode,confirmation:item.name})});await listing();}
          catch(error){note.textContent=error.message;}
          finally{globalThis.personalDocuments.setBusy(false);}
        });row.append(resume);
      }
      rows.append(row);
    }
  }
  refresh.addEventListener('click',async()=>{if(busy)return;globalThis.personalDocuments.setBusy(true);try{await listing();note.textContent='更新しました。処理中の文書は再度更新してください';}catch(error){note.textContent=error.message;}finally{globalThis.personalDocuments.setBusy(false);}});
  upload.addEventListener('click',async()=>{
    if(busy)return;const file=picker.files[0];if(!file){note.textContent='ファイルを選択してください';return;}
    if(!/\.(pdf|txt|md|csv)$/i.test(file.name)||file.size>5*1024*1024||!file.size){note.textContent='PDF/TXT/Markdown/CSV、空でない5 MiB以下のファイルを指定してください';return;}
    const conversion=/\.csv$/i.test(file.name)?'CSVはサーバーで検証し、行・列情報を含むTXTへ変換します。登録名と出典は「元名.csv.txt」です。\n':'';
    if(!confirm(`${conversion}${file.name}をOpenAIへ送信し、既存Vector Storeに保存します。料金・保存期間に注意してください。`))return;
    globalThis.personalDocuments.setBusy(true);
    try{const data=await call(`/api/documents?filename=${encodeURIComponent(file.name)}`,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});await listing();note.textContent=`登録状態: ${data.status}。完了後に選択できます`;picker.value='';}
    catch(error){note.textContent=error.message;}
    finally{globalThis.personalDocuments.setBusy(false);}
  });
})();
