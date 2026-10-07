const $ = id => document.getElementById(id);
let sessionId = null, conversation = [], browserIdentity = null, researchEnabled = false, sessionEpoch = 0;
let controller = null, cases = [], editingCase = null, evalTimer = null;
const defaultCase = {age: null, sex: null, chief_complaint: "", diagnoses: [], medications: [], lab_results: [], notes: ""};

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
async function api(path, options = {}) {
  const response = await fetch('/api/v1' + path, {...options, credentials:'same-origin', cache:'no-store',
    headers: {'Content-Type': 'application/json', 'X-Browser-Client':'1', ...options.headers}});
  const contentType = response.headers.get('content-type') || '';
  const raw = await response.text();
  let data = null;
  if (raw && contentType.toLowerCase().includes('application/json')) {
    try { data = JSON.parse(raw); } catch (_) { data = null; }
  }
  if (!response.ok) {
    if (response.status === 401) throw new Error('访客身份已失效或 Cookie 被禁用，请允许本地 Cookie 后刷新页面。');
    const detail = data && (data.detail || data.error || data.message);
    const trace = data?.trace_id ? `（trace_id: ${data.trace_id}）` : '';
    const fallback = raw && !raw.trim().startsWith('<') ? raw.trim().slice(0, 240) : '服务端没有返回结构化错误信息';
    throw new Error(`${typeof detail === 'string' ? detail : JSON.stringify(detail || fallback)}${trace}`);
  }
  if (!data) throw new Error(`服务端返回了无法识别的响应（${response.status}）`);
  return data;
}
function view(name, saveResearch = true) {
  if (!researchEnabled) name = 'chat';
  ['chat','cases','evaluations'].forEach(v => $(v+'View').hidden = v !== name);
  document.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === name));
  $('viewTitle').textContent = {chat:conversation.length ? '正在对话' : '新对话',cases:'病例档案',evaluations:'评测记录'}[name];
  $('toggleTimeline').hidden = name !== 'chat';
  if (researchEnabled && saveResearch) window.ResearchUI?.save({view:name});
}
document.querySelectorAll('[data-view]').forEach(b => b.onclick = () => view(b.dataset.view));
function persist() {
  $('sessionLabel').textContent = sessionId ? '当前对话 '+sessionId.slice(-6) : '尚未发送消息';
  $('chatIntro').hidden = conversation.length > 0;
  $('viewTitle').textContent = conversation.length ? '正在对话' : '新对话';
  window.BrowserUI?.renderTimeline();
}
function appendAnswerText(target, text, allowStrong = true) {
  // DOM-only formatting: model text and source snippets are never HTML.
  const tokens = String(text || '').split(/(\*\*[^\n]+?\*\*|`[^`\n]+`|\[\d+\](?:[ \t]*\[\d+\])*)/g);
  for (const part of tokens) {
    if (allowStrong && part.startsWith('**') && part.endsWith('**') && part.slice(2, -2).trim()) {
      const strong = node('strong');
      appendAnswerText(strong, part.slice(2, -2).trim(), false);
      target.append(strong);
    } else if (part.startsWith('`') && part.endsWith('`')) {
      target.append(node('code', part.slice(1, -1)));
    } else if (/^\[\d+\](?:[ \t]*\[\d+\])*$/.test(part)) {
      const numbers = [...part.matchAll(/\[(\d+)\]/g)].map(match => match[1]);
      const marker = node('sup', numbers.join('、'), 'citation-marker');
      marker.setAttribute('aria-label', '引用 '+numbers.join('、'));
      target.append(marker);
    } else if (part) target.append(document.createTextNode(part));
  }
}

function resizeMessageInput() {
  const input = $('messageInput');
  input.style.height = 'auto';
  input.style.height = Math.min(132, Math.max(38, input.scrollHeight))+'px';
}
$('messageInput').addEventListener('input', resizeMessageInput);
function citationCards(data) {
  const wrap = node('div', undefined, 'inline-citation-list');
  (data.citations || []).forEach(c => {
    const card = node('article', undefined, 'inline-citation');
    const sourceLabels = {huatuo_cardio:'问答资料',clinical_guideline:'指南资料',pubmed_abstract:'文献摘要',web_reference:'网页资料',session_attachment_reference:'本次上传的参考资料'};
    const sourceLabel = c.is_network ? '网络来源 · 需要甄别' : (sourceLabels[c.source_type] || '本地参考资料');
    card.append(node('small', '['+c.citation_id+'] '+sourceLabel+' / '+(c.medical_truth_verified ? '已完成医学核验' : '仅供参考，需核实')),
      node('strong', c.title || c.source_type || '资料片段'));
    const content = node('p');
    appendAnswerText(content, c.translated_text || c.snippet || c.original_text || '');
    card.append(content);
    if (c.is_network) card.append(node('small', '【信息来源于网络，需要甄别】'));
    if (c.translated_text) card.append(node('small','机器翻译，辅助阅读；原文仍保留'));
    if (c.original_text) {
      const original = node('details');
      original.append(node('summary','查看原文 · '+(c.original_language || '原文')), node('p',c.original_text));
      card.append(original);
    }
    const meta = [c.url ? '来源网址：'+c.url : '', c.page != null ? '页码：'+c.page : '', c.document_date ? '日期：'+c.document_date : ''].filter(Boolean).join('\n');
    if (meta) card.append(node('p',meta,'citation-meta'));
    if (c.url) {
      try {
        const url = new URL(c.url);
        if (['http:','https:'].includes(url.protocol)) { const link=node('a','打开来源网址'); link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';card.append(link); }
      } catch (_) {}
    }
    wrap.append(card);
  });
  return wrap;
}
function inlineCitations(data) {
  if (!data?.citations?.length) return null;
  const details = node('details', undefined, 'inline-citations');
  details.append(node('summary','查看 '+data.citations.length+' 条引用'), citationCards(data));
  return details;
}
function addMessage(kind, label, text, save = true, meta = {}) {
  const el = node('div', undefined, 'message '+kind);
  const at = meta.at || Date.now(), id = meta.id || 'msg-'+crypto.randomUUID();
  el.id = id;
  const header = node('div',undefined,'message-header'), time = node('time',new Date(at).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}));
  time.dateTime = new Date(at).toISOString();
  header.append(node('span',label,'message-label'),time);
  const answer = node('p'); appendAnswerText(answer, text); el.append(header, answer);
  const references = inlineCitations(meta.response);
  if (references) el.append(references);
  $('messages').append(el);
  $('messages').scrollTop = $('messages').scrollHeight;
  if (save) { conversation.push({kind, label, text, at, id, ...meta}); persist(); }
}
persist();

async function refreshStatus() {
  try {
    const response = await fetch('/api/v1/ready');
    const data = await response.json();
    if (![200,503].includes(response.status) || !data.llm_model) throw new Error('无法读取就绪状态');
    const modelStates = {mock:'离线 Mock',configured_not_probed:'已配置，尚未实测',last_request_succeeded:'最近调用成功',failed:'最近调用失败'};
    const lines = [data.llm_model, '模型连接：'+(modelStates[data.model_health] || data.model_health), 'Chroma 服务：'+(data.chroma_service_available ? '在线' : '不可用'),
      '关键词索引：'+(data.lexical_ready ? '可用 · 磁盘 BM25' : '尚未就绪'),
      '向量索引：'+(data.dense_ready ? '完整可用' : '尚未发布（不影响关键词检索）'),
      '登记：'+data.knowledge_documents+' 个片段', '医学证据：'+(data.partition_counts?.evidence ?? '待统计')+' 个片段', '版本：'+(data.index_version || '尚未构建'),
      '编码器：'+(data.embedding_ready ? '就绪' : '按需加载'),
      '检索：'+data.retrieval, '运行状态：'+(data.degraded ? '降级' : '正常')];
    if (data.agentic) lines.push('MCP：'+data.agentic.mcp, 'OCR 模型：'+(data.agentic.ocr_models ? '已安装' : '未安装'), '联网：'+(data.agentic.network_enabled ? '可按需使用' : '已禁用'));
    if (data.build_stage) lines.push('构建：'+data.build_stage+' / '+data.build_status,
      '持久化向量：'+(data.build_progress?.upserted || 0)+'；复用缓存：'+(data.build_progress?.reused_vectors || 0));
    if (data.pause_reason || data.last_build_error) lines.push('暂停 / 错误：'+(data.pause_reason || data.last_build_error));
    $('runtimeStatus').replaceChildren(...lines.map(x => node('div', x)));
    $('runtimeStatus').classList.toggle('warning', data.degraded);
  } catch (error) { $('runtimeStatus').textContent = '服务不可用：'+error.message; }
}
$('refreshStatus').onclick = refreshStatus;
setInterval(() => { if (researchEnabled && !document.hidden && location.protocol !== 'file:') refreshStatus(); }, 15000);

function resetSession() {
  if (controller) controller.abort();
  sessionEpoch++;
  sessionId = null; conversation = []; persist();
  window.AttachmentUI?.clear();
  $('messages').replaceChildren();
  $('evidenceState').textContent = '新会话尚无引用。';
  $('answerStatus').textContent = ''; $('answerDebug').textContent = ''; $('attachmentRefs').textContent = '';
  $('traceState').textContent = '尚未执行。'; $('toolTimeline').replaceChildren();
  $('requestState').textContent = 'Enter 发送 · Shift + Enter 换行';
  window.BrowserUI?.renderHistory();
}
$('newSession').onclick = resetSession;
$('caseSelect').onchange = resetSession;
$('exampleQuestion').onclick = () => { $('messageInput').value='房颤的常见检查有哪些？';resizeMessageInput();$('messageInput').focus(); };

function showEvidence(data) {
  const modes = {cited:'有来源回答',mixed:'引用与模型补充',model_supplement:'模型补充（未被本地资料支持）',refusal:'拒答 / 固定提示'};
  $('answerStatus').textContent = [modes[data.support_mode], data.uncertainty,
    (data.evidence_gaps || []).length ? '尚未覆盖：'+data.evidence_gaps.join('；') : ''].filter(Boolean).join('\n');
  $('answerDebug').textContent = ['实际模型：'+data.actual_model, '检索：'+data.retrieval_status,
    '记忆：'+data.memory_status, data.uncertainty,
    ...(data.degradation_reasons || [])].filter(Boolean).join('\n');
  $('attachmentRefs').textContent = (data.attachment_context_refs || []).length ? '本轮使用已确认的患者资料：'+data.attachment_context_refs.map(r=>'第 '+r.page+' 页').join('、')+'。这些资料不是医学结论的依据。' : '';
  const target = $('evidenceState');
  target.replaceChildren();
  if (!data.citations?.length) { target.textContent = '本轮没有绑定医学证据。'; return; }
  data.citations.forEach(c => {
    const el = node('article', undefined, 'evidence-item');
    el.append(node('small', '['+c.citation_id+'] '+c.source_type+' / '+c.review_status),
              node('strong', c.title), node('p', c.translated_text || c.snippet),
              node('small', '页码：'+(c.page ?? '未提供')+' · '+(c.translation_status || '无翻译标记')),
              node('p', [c.document_date && '文档日期：'+c.document_date,c.retrieved_at && '获取：'+c.retrieved_at,c.jurisdiction && '地区：'+c.jurisdiction].filter(Boolean).join(' / ')));
    if (c.url) {
      try {
        const url = new URL(c.url);
        if (['http:', 'https:'].includes(url.protocol)) {
          const link = node('a','查看来源'); link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; el.append(link);
        }
      } catch (_) {}
    }
    if (c.translated_text) el.append(node('small', '机器翻译，辅助阅读；未改变原审核状态'));
    if (c.display_translation_status === 'unavailable') el.append(node('small', '翻译校验未通过，保留原文'));
    if (c.original_text) {
      const original = node('details'); original.append(node('summary','查看原文'+(c.original_language ? ' · '+c.original_language : '')),node('p',c.original_text)); el.append(original);
    }
    const detail = node('details'); detail.append(node('summary','来源 ID 与文件'));
    detail.append(node('p',c.evidence_id+'\n'+(c.source_file || '')+'\n文件 SHA256：'+(c.source_sha256 || '未提供')+'\n片段 SHA256：'+(c.content_sha256 || '未提供'),'provenance'));
    el.append(detail); target.append(el);
  });
}
async function showTrace(id) {
  const capturedEpoch = sessionEpoch;
  try {
    const trace = await api('/traces/'+encodeURIComponent(id));
    if (capturedEpoch !== sessionEpoch) return;
    const target = $('traceState'); target.replaceChildren(node('p','路由：'+trace.route+'；模型调用 '+trace.model_calls+' 次；总耗时 '+trace.duration_ms+' ms'));
    const table = node('table'), head = node('tr');
    ['节点','耗时 ms','模型调用'].forEach(x => head.append(node('th',x))); table.append(head);
    trace.nodes.forEach(n => { const row = node('tr'); [n.node,n.duration_ms,n.model_calls].forEach(x => row.append(node('td',String(x)))); table.append(row); });
    target.append(table);
    const timeline = node('ol',undefined,'tool-timeline');
    (trace.tools || []).forEach(t=>{const item=node('li');item.append(node('strong',t.tool+' / '+(t.status || t.transport || '')),node('p',[t.reason,t.query,t.query_language && '查询语言：'+t.query_language,t.cache_hit ? '命中翻译缓存' : '',t.source_host,t.transport,t.duration_ms !== undefined ? t.duration_ms+' ms' : '',t.error,t.degradation].filter(Boolean).join('；')));timeline.append(item);});
    $('toolTimeline').replaceChildren(node('h3','本轮工具步骤'),timeline);
    const raw=node('details');raw.append(node('summary','工具结构化记录'),node('pre',JSON.stringify(trace.tools,null,2)));target.append(raw);
  } catch (error) { if (capturedEpoch === sessionEpoch) $('traceState').textContent = error.message; }
}
$('chatForm').onsubmit = async event => {
  event.preventDefault(); if (controller) return;
  if (!browserIdentity) { $('requestState').textContent='请等待访客身份连接完成。'; return; }
  const message = $('messageInput').value.trim(); if (!message) return;
  const attachmentIds = window.AttachmentUI?.ids() || [];
  const pending = new AbortController(); controller = pending;
  const capturedEpoch = sessionEpoch;
  const timer = setTimeout(() => pending.abort(), 200000);
  addMessage('user','你',message);
  $('messageInput').value = ''; resizeMessageInput(); $('sendChat').disabled = true; $('cancelChat').hidden = false;
  $('caseSelect').disabled = true;
  $('requestState').textContent = '处理中：正在路由、检索与校验…';
  try {
    await window.BrowserUI.ensureSession();
    if (capturedEpoch !== sessionEpoch) return;
    const data = await api('/chat', {method:'POST', signal:pending.signal,
      body:JSON.stringify({session_id:sessionId, case_id:$('caseSelect').value || null,
        message, allow_model_supplement:researchEnabled && $('supplement').checked,
        attachment_ids:attachmentIds, network_mode:$('networkMode').checked ? 'auto' : 'off'})});
    if (capturedEpoch !== sessionEpoch) return;
    sessionId = data.session_id; persist();
    addMessage(data.is_emergency ? 'assistant emergency' : 'assistant',data.is_emergency ? '急救提示' : '心血管助手',data.answer, true, {response:data,attachmentDerived:!!attachmentIds.length});
    showEvidence(data); await showTrace(data.trace_id);
    if (capturedEpoch !== sessionEpoch) return;
    await window.BrowserUI.loadHistory();
    if (researchEnabled) refreshStatus();
    $('requestState').textContent = '本轮完成';
  } catch (error) {
    if (capturedEpoch !== sessionEpoch) return;
    $('requestState').textContent = error.name === 'AbortError' ? '已取消等待；服务端可能仍在完成本轮。' : '请求失败';
    if (error.name !== 'AbortError') addMessage('system-message','系统',error.message);
  } finally {
    clearTimeout(timer);
    if (controller === pending) { controller = null; $('sendChat').disabled = false; $('cancelChat').hidden = true; $('caseSelect').disabled = false; }
  }
};
$('cancelChat').onclick = () => controller?.abort();
$('messageInput').onkeydown = event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('chatForm').requestSubmit(); }
};

async function loadCases() {
  try {
    cases = await api('/cases');
    const chosen = $('caseSelect').value;
    $('caseSelect').replaceChildren(new Option('不关联病例',''));
    $('caseList').replaceChildren();
    cases.forEach(c => {
      const label = (c.chief_complaint || '未填主诉')+' / '+(c.age ?? '?')+'岁';
      $('caseSelect').append(new Option(label,c.case_id));
      const button = node('button',label,'case-option'); button.onclick = () => editCase(c);
      $('caseList').append(button);
    });
    if (cases.some(c => c.case_id === chosen)) $('caseSelect').value = chosen;
    if (!cases.length) $('caseList').textContent = '还没有病例。右侧填写后保存即可。';
  } catch (error) { $('caseList').textContent = error.message; }
}
function editCase(c) {
  editingCase = c?.case_id || null;
  const data = c ? {...c} : {...defaultCase};
  ['case_id','patient_id','schema_version','created_at','updated_at'].forEach(k => delete data[k]);
  $('caseEditor').value = JSON.stringify(data,null,2);
  $('caseTitle').textContent = editingCase ? '编辑病例 '+editingCase.slice(0,8) : '新建病例';
  $('bindCase').disabled = $('deleteCase').disabled = !editingCase;
  $('caseStatus').textContent = '';
}
$('newCase').onclick = () => editCase(null);
$('caseForm').onsubmit = async event => {
  event.preventDefault();
  try {
    const data = JSON.parse($('caseEditor').value);
    const saved = await api('/cases'+(editingCase ? '/'+encodeURIComponent(editingCase) : ''),
      {method:editingCase ? 'PATCH' : 'POST', body:JSON.stringify(data)});
    await loadCases(); editCase(saved); $('caseStatus').textContent = '病例已保存。';
  } catch (error) { $('caseStatus').textContent = '未保存：'+error.message; }
};
$('bindCase').onclick = () => {
  $('caseSelect').value = editingCase;
  resetSession(); view('chat');
};
$('deleteCase').onclick = async () => {
  if (!editingCase || !confirm('确认删除这个本地研究病例？此操作不能在页面撤销。')) return;
  try { await api('/cases/'+encodeURIComponent(editingCase),{method:'DELETE'}); await loadCases(); editCase(null); }
  catch (error) { $('caseStatus').textContent = error.message; }
};
editCase(null);

function renderEvaluation(data) {
  $('evalProgress').textContent = '状态：'+data.status+'；已完成 '+(data.completed_cases || 0)+' 个样例；'+data.run_id;
  $('evalDetails').textContent = JSON.stringify(data,null,2);
  $('evalRunId').value = data.run_id;
  $('evalMetrics').replaceChildren();
  for (const [suite, values] of Object.entries(data.suites || {})) {
    const table = node('table'), caption = node('caption',suite); table.append(caption);
    for (const [key,val] of Object.entries(values)) {
      if (['results','by_intent','confusion_matrix'].includes(key) || typeof val === 'object' && val !== null) continue;
      const row = node('tr'); row.append(node('th',key),node('td',val === null ? '未测量' : String(val))); table.append(row);
    }
    $('evalMetrics').append(table);
  }
}
async function pollEvaluation(id) {
  clearTimeout(evalTimer);
  try {
    const data = await api('/evaluations/'+encodeURIComponent(id)); renderEvaluation(data);
    if (['queued','running'].includes(data.status)) { evalTimer = setTimeout(() => pollEvaluation(id),2000); }
    else $('runEvaluation').disabled = false;
  } catch (error) { $('evalProgress').textContent = error.message; $('runEvaluation').disabled = false; }
}
$('evaluationForm').onsubmit = async event => {
  event.preventDefault(); $('runEvaluation').disabled = true;
  try {
    const data = await api('/evaluations/run',{method:'POST',body:JSON.stringify({
      suite:$('evalSuite').value, mode:$('evalMode').value, max_cases:Number($('evalCount').value),
      use_llm_judge:$('evalJudge').checked, background:true})});
    window.ResearchUI?.save({evaluation:data.run_id}); await pollEvaluation(data.run_id);
  } catch (error) { $('evalProgress').textContent = error.message; $('runEvaluation').disabled = false; }
};
$('loadEvaluation').onclick = () => pollEvaluation($('evalRunId').value.trim());
