const $ = id => document.getElementById(id);
let activePath = null;
let history = [];
let pendingChanges = [];
let apiSettings = {provider:'openai', api_key:'', model:'', base_url:''};
// Remember non-secret preferences for this browser tab; never persist the API key.
try {
  const saved = JSON.parse(sessionStorage.getItem('codepilot-api-settings') || '{}');
  apiSettings = {...apiSettings, ...saved, api_key:''};
} catch (_) {}


async function api(url, options={}) {
  const res = await fetch(url, {headers: {'Content-Type':'application/json'}, ...options});
  const data = await res.json().catch(()=>({error:'Некорректный ответ сервера'}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function loadTree(){
  const tree = await api('/api/tree');
  $('tree').innerHTML = '';
  renderTree(tree, $('tree'), 0);
}
function renderTree(items, root, depth){
  for(const item of items){
    const row=document.createElement('div'); row.className='tree-item';
    row.innerHTML=`<span class="${item.type==='dir'?'folder-icon':'file-icon'}">${item.type==='dir'?'▸':'◈'}</span><span class="fname">${escapeHtml(item.name)}</span>`;
    row.style.paddingLeft=(8+depth*8)+'px';
    root.appendChild(row);
    if(item.type==='file') row.onclick=()=>openFile(item.path);
    else {
      const children=document.createElement('div'); children.className='tree-children'; root.appendChild(children);
      renderTree(item.children||[],children,depth+1);
      row.onclick=()=>{children.classList.toggle('hidden'); row.querySelector('span').textContent=children.classList.contains('hidden')?'▸':'▾';};
    }
  }
}
async function openFile(path){
  try{
    const data=await api('/api/file?path='+encodeURIComponent(path));
    activePath=data.path; $('editor').value=data.content; $('currentPath').textContent=path;
    $('editorName').textContent=path; $('saveStatus').textContent='Загружен';
  }catch(e){alert(e.message)}
}
$('refreshTree').onclick=loadTree;
$('modelSettings').onclick=()=>$('settingsModal').classList.remove('hidden');
$('cancelSettings').onclick=()=>$('settingsModal').classList.add('hidden');
$('provider').onchange=()=>{
  if($('provider').value==='anthropic' && !$('model').value) $('model').value='claude-sonnet-4-20250514';
  if($('provider').value==='openai' && $('model').value==='claude-sonnet-4-20250514') $('model').value='';
  $('baseUrl').placeholder=$('provider').value==='anthropic'?'Необязательно: https://api.anthropic.com':'Например, https://openrouter.ai/api/v1';
};
// Restore provider/model/URL, but intentionally never restore the API key.
$('provider').value=apiSettings.provider;
$('model').value=apiSettings.model;
$('baseUrl').value=apiSettings.base_url;
$('baseUrl').placeholder='Например, https://openrouter.ai/api/v1';
$('saveSettings').onclick=()=>{
  apiSettings={provider:$('provider').value,api_key:$('apiKey').value.trim(),model:$('model').value.trim(),base_url:$('baseUrl').value.trim()};
  try { sessionStorage.setItem('codepilot-api-settings', JSON.stringify({provider:apiSettings.provider,model:apiSettings.model,base_url:apiSettings.base_url})); } catch (_) {}
  $('settingsModal').classList.add('hidden');
  addMessage('assistant',`Настройки выбраны: ${apiSettings.provider==='anthropic'?'Claude / Anthropic':'свой OpenAI-совместимый API'}, модель ${apiSettings.model||'не выбрана'}. API-ключ хранится только в памяти вкладки.`);
};
$('saveFile').onclick=async()=>{
  if(!activePath){alert('Сначала открой или создай файл.');return}
  try{await api('/api/file',{method:'POST',body:JSON.stringify({path:activePath,content:$('editor').value})});$('saveStatus').textContent='Сохранено';loadTree();}
  catch(e){alert(e.message)}
};
$('newFile').onclick=()=>{$('modal').classList.remove('hidden');$('newPath').focus()};
$('cancelNew').onclick=()=>$('modal').classList.add('hidden');
$('createNew').onclick=async()=>{
  const path=$('newPath').value.trim(); if(!path)return;
  try{await api('/api/file',{method:'POST',body:JSON.stringify({path,content:''})});$('modal').classList.add('hidden');$('newPath').value='';await loadTree();openFile(path);}
  catch(e){alert(e.message)}
};
document.querySelectorAll('.tab').forEach(btn=>btn.onclick=()=>{
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));btn.classList.add('active');
  $('chatPanel').classList.toggle('hidden',btn.dataset.tab!=='chat');$('terminalPanel').classList.toggle('hidden',btn.dataset.tab!=='terminal');
});
function addMessage(role, content){
  const welcome=document.querySelector('.welcome');if(welcome)welcome.remove();
  const div=document.createElement('div');div.className='message '+role;
  div.innerHTML=`<div class="role">${role==='user'?'Ты':'CodePilot'}</div><div>${escapeHtml(content)}</div>`;
  $('messages').appendChild(div);$('messages').scrollTop=$('messages').scrollHeight;return div;
}
function parseAgentReply(raw){
  try{
    const start=raw.indexOf('{'), end=raw.lastIndexOf('}');
    if(start>=0&&end>start){const obj=JSON.parse(raw.slice(start,end+1));if(obj&&typeof obj==='object'&&('reply'in obj||'changes'in obj))return obj;}
  }catch(e){}
  return {reply:raw,changes:[]};
}
$('chatForm').onsubmit=async e=>{
  e.preventDefault();const message=$('prompt').value.trim();if(!message)return;
  $('prompt').value='';addMessage('user',message);
  const wait=addMessage('assistant','Думаю…');
  try{
    const data=await api('/api/chat',{method:'POST',body:JSON.stringify({message,history,...apiSettings})});
    wait.remove();
    const parsed=parseAgentReply(data.reply||'');
    addMessage('assistant',parsed.reply||'Готово.');
    history.push({role:'user',content:message},{role:'assistant',content:data.reply||''});
    if(parsed.changes?.length) showChanges(parsed.changes);
  }catch(err){wait.remove();addMessage('assistant','Ошибка: '+err.message)}
};
$('prompt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('chatForm').requestSubmit()}});
document.querySelectorAll('.suggestions button').forEach(b=>b.onclick=()=>{$('prompt').value=b.textContent;$('prompt').focus()});
function showChanges(changes){
  const container=document.createElement('div');container.className='message assistant';
  container.innerHTML='<div class="role">ПРЕДЛОЖЕННЫЕ ИЗМЕНЕНИЯ</div>';
  changes.forEach((change,i)=>{
    const card=document.createElement('div');card.className='change-card';
    card.innerHTML=`<b>${escapeHtml(change.path||'unknown')}</b><pre>${escapeHtml(change.content||'')}</pre>`;
    container.appendChild(card);
  });
  const actions=document.createElement('div');actions.style.marginTop='10px';
  const apply=document.createElement('button');apply.className='primary';apply.textContent='Применить все изменения';
  const dismiss=document.createElement('button');dismiss.textContent='Отклонить';
  actions.append(apply,dismiss);container.appendChild(actions);$('messages').appendChild(container);
  apply.onclick=async()=>{
    try{
      for(const change of changes) await api('/api/file',{method:'POST',body:JSON.stringify({path:change.path,content:change.content})});
      apply.disabled=true;apply.textContent='Изменения применены';await loadTree();
      if(activePath){const current=await api('/api/file?path='+encodeURIComponent(activePath)).catch(()=>null);if(current)$('editor').value=current.content;}
    }catch(e){alert('Не удалось применить изменения: '+e.message)}
  };
  dismiss.onclick=()=>container.remove();
}
$('commandForm').onsubmit=async e=>{
  e.preventDefault();const command=$('commandInput').value.trim();if(!command)return;
  $('commandInput').value='';$('terminalOutput').textContent+='\n$ '+command+'\n';
  try{const data=await api('/api/command',{method:'POST',body:JSON.stringify({command})});$('terminalOutput').textContent+=data.output||`Команда завершилась с кодом ${data.returncode}`;}
  catch(err){$('terminalOutput').textContent+='Ошибка: '+err.message}
  $('terminalOutput').scrollTop=$('terminalOutput').scrollHeight;
};
loadTree().catch(e=>{$('tree').textContent='Ошибка: '+e.message});
