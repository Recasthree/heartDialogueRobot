/* User identity is an HttpOnly server cookie; research preferences are a separate UI cookie.
   Neither this five-click reveal nor the preference cookie is an authorization boundary. */
(() => {
  let history = [], creating = null, collapsed = matchMedia('(max-width: 760px)').matches;
  let clickCount = 0, firstClick = 0, prefs = {};
  const identityChannel = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('hd-user-reset') : null;
  const researchCookie = 'hd_research';
  try { prefs = JSON.parse(decodeURIComponent(document.cookie.split('; ').find(x=>x.startsWith(researchCookie+'='))?.split('=').slice(1).join('=') || '{}')); } catch (_) {}
  function notice(text) { $('pageNotice').textContent=text; $('pageNotice').hidden=!text; }
  function savePrefs(values) {
    prefs = {...prefs,...values};
    // Preferences only, never identity tokens, patient facts or chat bodies.
    const safe = {view:['chat','cases','evaluations'].includes(prefs.view)?prefs.view:'chat',evaluation:String(prefs.evaluation || '').slice(0,100)};
    document.cookie=researchCookie+'='+encodeURIComponent(JSON.stringify(safe))+'; Path=/; Max-Age=7776000; SameSite=Strict'+(location.protocol==='https:'?'; Secure':'');
  }
  function sidebar(value) {
    collapsed=value; $('appShell').classList.toggle('history-collapsed',value);
    $('historySidebar').hidden=value;
    $('toggleHistory').setAttribute('aria-expanded',String(!value));
    $('toggleHistory').setAttribute('aria-label',value?'展开历史对话':'收起历史对话');
    $('historyBackdrop').hidden=value || !matchMedia('(max-width: 760px)').matches;
  }
  function research(value) {
    researchEnabled=value;
    document.body.classList.toggle('research-mode',value); $('researchDesk').hidden=!value;
    if (value) {
      const last = prefs.view;
      refreshStatus(); loadCases();
      $('evalRunId').value=prefs.evaluation || '';
      view(['chat','cases','evaluations'].includes(last)?last:'chat');
      $('closeResearch').focus();
    } else { view('chat'); $('supplement').checked=false; }
  }
  $('brandLogo').onclick=() => {
    const now=Date.now(); if (now-firstClick>3000) {clickCount=0; firstClick=now;}
    if (++clickCount===5) {clickCount=0;firstClick=0;research(true);}
  };
  document.addEventListener('pointerdown',event=>{if(!event.target.closest('#brandLogo')){clickCount=0;firstClick=0;}});
  $('closeResearch').onclick=()=>{research(false);$('brandLogo').focus();};
  $('clearResearchCookies').onclick=()=>{
    document.cookie=researchCookie+'=; Path=/; Max-Age=0; SameSite=Strict';prefs={};
    $('evalRunId').value='';research(false);
    notice('研究台 Cookie 已清除。访客身份和历史对话未改变。');
  };
  $('toggleHistory').onclick=()=>sidebar(!collapsed);
  $('historyBackdrop').onclick=()=>sidebar(true);
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&matchMedia('(max-width:760px)').matches)sidebar(true);});
  matchMedia('(max-width:760px)').addEventListener('change',e=>sidebar(e.matches));
  sidebar(collapsed);

  function timeline(visible) {
    $('timelineSidebar').hidden = !visible;
    $('chatLayout').classList.toggle('timeline-hidden', !visible);
    $('toggleTimeline').setAttribute('aria-expanded', String(visible));
    $('toggleTimeline').textContent = visible ? '隐藏时间轴' : '显示时间轴';
  }
  $('toggleTimeline').onclick = () => timeline($('timelineSidebar').hidden);
  $('hideTimeline').onclick = () => { timeline(false); $('toggleTimeline').focus(); };
  timeline(false);

  function renderHistory() {
    const target=$('historyList');target.replaceChildren();
    if(!history.length){target.append(node('p','还没有历史对话。发出第一个问题，就会留在这里。','history-empty'));return;}
    for(const item of history) {
      const button=node('button',undefined,'history-item'+(item.id===sessionId?' selected':''));
      button.setAttribute('aria-current',String(item.id===sessionId));
      button.append(node('span',item.title),node('time',new Date(item.updated*1000).toLocaleString('zh-CN',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})));
      button.onclick=()=>openSession(item.id); target.append(button);
    }
  }
  async function loadHistory() {
    const identity=browserIdentity?.user_id;
    try {const result=await api('/browser/sessions');if(identity!==browserIdentity?.user_id)return;history=result;renderHistory();}
    catch(error){notice(error.message);}
  }
  function renderTimeline() {
    const target=$('conversationEvents');target.replaceChildren();
    $('timelineEmpty').hidden=conversation.length>0;
    $('timelineCount').textContent=conversation.length+' 条消息';
    for(const m of conversation) {
      const row=node('li',undefined,m.kind==='user'?'event-user':'event-assistant');
      const time=node('time',new Date(m.at).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}));
      time.dateTime=new Date(m.at).toISOString();
      const button=node('button',undefined,'event-link');
      button.append(node('strong',m.label),node('span',m.text.length>42?m.text.slice(0,42)+'…':m.text));
      button.onclick=()=>{$(m.id)?.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'center'});const refs=$(m.id)?.querySelector('.inline-citations');if(refs)refs.open=true;if(m.response?.trace_id&&researchEnabled)showTrace(m.response.trace_id);};
      row.append(time,button);target.append(row);
    }
  }
  async function openSession(id) {
    resetSession(); const epoch=sessionEpoch;
    try {
      const detail=await api('/browser/sessions/'+encodeURIComponent(id));
      if(epoch!==sessionEpoch)return;
      sessionId=id;
      if(detail.case_id && ![...$('caseSelect').options].some(o=>o.value===detail.case_id))$('caseSelect').append(new Option('已关联研究病例',detail.case_id));
      $('caseSelect').value=detail.case_id || '';
      for(const turn of detail.turns) {
        addMessage('user','你',turn.question,true,{at:turn.created*1000});
        const r=turn.response;
        addMessage(r.is_emergency?'assistant emergency':'assistant',r.is_emergency?'急救提示':'心血管助手',r.answer,true,{at:turn.created*1000,response:r});
      }
      persist();renderHistory();view('chat');
      const last=detail.turns.at(-1)?.response;
      if(last){showEvidence(last);if(last.trace_id)await showTrace(last.trace_id);}
      if(matchMedia('(max-width:760px)').matches)sidebar(true);
    }catch(error){if(epoch===sessionEpoch)notice(error.message);}
  }
  async function ensureSession() {
    if(sessionId)return sessionId;
    if(!browserIdentity)throw new Error('访客身份尚未就绪，请刷新页面。');
    const epoch=sessionEpoch;
    if(!creating || creating.epoch!==epoch) {
      const promise=api('/browser/sessions',{method:'POST',body:JSON.stringify({case_id:$('caseSelect').value||null})});
      creating={epoch,promise};
    }
    const result=await creating.promise;
    if(epoch!==sessionEpoch)throw new Error('对话已切换，请在当前对话重新操作。');
    sessionId=result.id;creating=null;persist();await loadHistory();return sessionId;
  }
  async function bootstrap(restore=true) {
    $('sendChat').disabled=true;
    try {
      browserIdentity=await api('/browser');$('visitorLabel').textContent=browserIdentity.label;
      $('clearUserCookies').disabled=false; await loadHistory();
      if(restore && history.length)await openSession(history[0].id);
      $('sendChat').disabled=false;
    }catch(error){notice(error.message);$('visitorLabel').textContent='连接未完成';}
  }
  $('clearUserCookies').onclick=async()=>{
    if(!confirm('清除用户 Cookie 后将切换为新匿名身份，本页不再显示原历史。研究台 Cookie 和共享病例不受影响；这不等于删除服务器记录。继续吗？'))return;
    $('clearUserCookies').disabled=true;resetSession();$('caseSelect').value='';browserIdentity=null;history=[];renderHistory();
    try {
      await api('/browser',{method:'DELETE'});
      // Remove only legacy user-browser remnants, never research evaluation preferences.
      for(const key of ['cardio-session','cardio-conversation','cardio-user','cardio-case','cardio-last-answer','cardio-attachments'])sessionStorage.removeItem(key);
      identityChannel?.postMessage('reset');
      await bootstrap(false);view('chat',false);
      notice('旧用户 Cookie 已清除，已建立新的匿名身份。研究台设置保留。');
    }catch(error){notice('清除未完成：'+error.message);$('clearUserCookies').disabled=false;}
  };
  $('refreshHistory').onclick=loadHistory;
  if(identityChannel)identityChannel.onmessage=()=>{
    resetSession();browserIdentity=null;history=[];renderHistory();$('caseSelect').value='';
    $('visitorLabel').textContent='身份已更新';$('sendChat').disabled=true;
    notice('用户 Cookie 已在其他页面清除。本页旧对话已收起，请刷新后使用新的匿名身份。');
  };
  window.BrowserUI={ensureSession,loadHistory,renderHistory,renderTimeline};
  window.ResearchUI={save:savePrefs};
  document.addEventListener('DOMContentLoaded',()=>{if(location.protocol!=='file:')bootstrap();});
})();
