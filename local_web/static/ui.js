"use strict";
const form=document.querySelector('#chat'), input=document.querySelector('#message'), send=document.querySelector('#send'), status=document.querySelector('#status'), history=document.querySelector('#history'), clear=document.querySelector('#clear');
let csrf='', conversation=[];
globalThis.onDocumentsChanged=()=>{conversation=[];history.replaceChildren();status.textContent='検索対象を変更したため会話履歴を消去しました';};
const errors={403:'認証エラー：AWS認証またはローカル接続設定を確認してください。',422:'入力エラー：本文・履歴は各4000文字以内で指定してください。',413:'入力サイズが上限を超えています。',502:'モデルサービスエラー：Lambda側の状態を確認してください。',503:'通信失敗：ネットワーク接続を確認してください。',504:'タイムアウト：応答が間に合いませんでした。'};
function addMessage(kind,text){const item=document.createElement('div');item.className=`message ${kind}`;item.textContent=text;history.append(item);history.scrollTop=history.scrollHeight;}
async function prepare(){try{const response=await fetch('/api/session',{cache:'no-store'});if(!response.ok)throw new Error();csrf=(await response.json()).csrf;send.disabled=false;status.textContent='接続準備完了（履歴は画面のメモリ内のみ）';}catch{status.textContent='ローカル中継サーバーに接続できません。画面を再読み込みしてください。';}}
form.addEventListener('submit',async event=>{
 event.preventDefault();if(send.disabled)return;
 const message=input.value;if(!message.trim()||Array.from(message).length>4000){status.textContent=errors[422];return;}
 const prior=conversation.slice(-20);const ids=globalThis.personalDocuments?.selection()||[];const payload=()=>ids.length?{message,history:prior,document_ids:ids}:{message,history:prior};let body=JSON.stringify(payload());
 while(prior.length&&new TextEncoder().encode(body).length>16384){prior.splice(0,2);body=JSON.stringify(payload());}
 if(new TextEncoder().encode(body).length>16384){status.textContent=errors[413];return;}
 const trimmed=prior.length<conversation.length;
 send.disabled=true;clear.disabled=true;globalThis.personalDocuments?.setBusy(true);addMessage('user',message);status.textContent='送信中…';
 const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),160000);
 try{
  const response=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body,signal:controller.signal});
  if(!response.ok){status.textContent=errors[response.status]||`接続エラー (${response.status})`;return;}
  const result=await response.json();if(typeof result.response!=='string'||!result.response.trim())throw new Error();
  addMessage('assistant',result.response);for(const source of (result.sources||[])){if(typeof source.filename==='string')addMessage('source','出典: '+source.filename);}input.value='';
  if(Array.from(result.response).length<=4000){conversation=[...prior,{role:'user',content:message},{role:'assistant',content:result.response}].slice(-20);status.textContent=trimmed?'受信しました（古い履歴を上限に合わせて除外しました）':'受信しました';}
  else{conversation=[];status.textContent='受信しました（回答が履歴上限を超えたため次回は新しい会話になります）';}
 }catch(error){status.textContent=error.name==='AbortError'?errors[504]:'通信失敗：ローカル中継サーバーとの接続を確認してください。';}
 finally{clearTimeout(timeout);send.disabled=false;clear.disabled=false;globalThis.personalDocuments?.setBusy(false);}
});
clear.addEventListener('click',()=>{if(send.disabled)return;conversation=[];history.replaceChildren();status.textContent='履歴を消去しました';});
prepare();