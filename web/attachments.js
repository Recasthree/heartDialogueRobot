/* Temporary attachment text deliberately stays out of browser storage. */
(() => {
  const records = new Map(), timers = new Map(), previews = new Map(), drafts = new Map();
  let epoch = 0;
  function scope() {
    if (!sessionId) throw new Error('请先等待访客会话就绪。');
    return new URLSearchParams({session_id:sessionId, case_id:$('caseSelect').value});
  }
  function savedIds() { /* Temporary attachment identifiers remain in this page only. */ }
  function info(message) { $('attachmentState').textContent = message; }
  function discard(id) {
    records.delete(id);clearTimeout(timers.get(id));timers.delete(id);
    URL.revokeObjectURL(previews.get(id));previews.delete(id);
    for(const key of drafts.keys())if(key.startsWith(id+':'))drafts.delete(key);
  }
  async function update(id, body) {
    const capturedEpoch=epoch;
    try {
      const item = await api('/attachments/'+id+'?'+scope(), {method:'PATCH',body:JSON.stringify(body)});
      if(capturedEpoch!==epoch)return;
      if(body.page_texts) for(const number of Object.keys(body.page_texts)) drafts.delete(id+':'+number);
      records.set(id,item); render();
      if(item.status==='ready')info(item.pages.some(p=>p.requires_confirmation&&!p.confirmed)?'请核对并确认关键内容，再发送问题。':'附件内容已确认，可用于本轮问题。');
      if (item.status === 'queued') poll(id, epoch);
      return item;
    } catch (error) { info(error.message); }
  }
  async function preview(item, target) {
    const capturedEpoch=epoch;
    try {
      if (!previews.has(item.id)) {
        const response=await fetch('/api/v1/attachments/'+item.id+'/preview?'+scope(),{cache:'no-store'});
        if (!response.ok) throw new Error('无法读取原文件');
        const blob=await response.blob();if(capturedEpoch!==epoch)return;
        previews.set(item.id,URL.createObjectURL(blob));
      }
      const url=previews.get(item.id);
      if (['png','jpeg'].includes(item.kind)) {const img=node('img');img.src=url;img.alt='上传的原始研究附件';target.append(img);}
      else {const a=node('a','打开原文件对照');a.href=url;a.target='_blank';a.rel='noopener';target.append(a);}
    } catch(error) {target.append(node('p',error.message));}
  }
  function render() {
    const root=$('attachmentList'); root.replaceChildren();
    for (const item of records.values()) {
      const box=node('details',undefined,'attachment-item');
      const pending=(item.pages || []).filter(p=>p.requires_confirmation&&!p.confirmed);
      const label={queued:'等待解析',parsing:'本地解析中',failed:'解析失败',ready:pending.length?'需要确认 '+pending.length+' 页':'可用于本轮'}[item.status] || item.status;
      box.append(node('summary',item.filename+' / '+label));
      const controls=node('div',undefined,'actions');
      const purpose=node('select');purpose.setAttribute('aria-label','附件用途');
      purpose.append(new Option('患者资料','patient_context'),new Option('参考资料','reference'));purpose.value=item.purpose;
      purpose.disabled=['queued','parsing'].includes(item.status);
      purpose.onchange=()=>update(item.id,{version:item.version,purpose:purpose.value});controls.append(purpose);
      const remove=node('button','删除','quiet');remove.type='button';
      remove.onclick=async()=>{try{await api('/attachments/'+item.id+'?'+scope(),{method:'DELETE'});discard(item.id);savedIds();render();info('附件及解析文本已删除。');}catch(error){info(error.message);}};
      controls.append(remove);box.append(controls);
      if(item.status==='failed') {
        box.append(node('p','失败原因：'+item.error+'。可填写页码后重试；OCR 模型未安装时请先运行 setup_ocr。','warning-text'));
        const retry=node('button','重新解析','quiet');retry.type='button';retry.onclick=()=>update(item.id,{version:item.version,retry:true,selected_pages:readPages()});box.append(retry);
      }
      const original=node('div',undefined,'attachment-preview');
      box.append(original);box.addEventListener('toggle',()=>{if(box.open&&!original.childNodes.length)preview(item,original);});
      for(const page of item.pages || []) {
        const group=node('section',undefined,'parsed-page');
        group.append(node('h4','第 '+page.page+' 页 / '+({ocr:'OCR',pdf_text:'PDF 文字',text:'文本',user_corrected:'用户修订'}[page.method]||page.method)));
        if(page.requires_confirmation) group.append(node('p',page.confirmed?'已核对转录；不代表医学审核通过。':'请核对药名、剂量、检验数值与单位，再确认本页。',page.confirmed?'hint':'warning-text'));
        if(page.low_confidence) group.append(node('p','含低置信识别文字，请与原图逐项核对。','warning-text'));
        const textarea=node('textarea');textarea.value=drafts.get(item.id+':'+page.page) ?? page.text;textarea.rows=6;textarea.maxLength=16000;textarea.setAttribute('aria-label','第 '+page.page+' 页识别文本');textarea.oninput=()=>drafts.set(item.id+':'+page.page,textarea.value);group.append(textarea);
        const actions=node('div',undefined,'actions');
        const save=node('button','保存修订','quiet');save.type='button';save.onclick=()=>update(item.id,{version:item.version,page_texts:{[page.page]:textarea.value}});
        const confirm=node('button',page.confirmed?'本页已确认':'确认本页内容');confirm.type='button';confirm.disabled=page.confirmed;
        confirm.onclick=()=>{if(textarea.value!==page.text){info('请先保存修订，再确认新的文本版本。');return;}update(item.id,{version:item.version,confirmed_pages:[page.page]});};
        actions.append(save,confirm);group.append(actions);box.append(group);
      }
      if(item.status==='ready' && item.purpose==='patient_context') {
        const saveCase=node('button','选择事实保存到病例','quiet');saveCase.type='button';saveCase.disabled=!!pending.length;
        saveCase.onclick=()=>{
          if(!$('caseSelect').value){info('请先建立并关联病例，再上传需要保存事实的报告。未关联的附件仍可用于当前对话。');return;}
          const selection=node('div',undefined,'fact-selection');selection.append(node('p','每行粘贴一个已确认的原文事实。只保存你选择的内容；系统会再次脱敏。','hint'));
          const input=node('textarea');input.rows=4;input.setAttribute('aria-label','明确保存到病例的原文事实');selection.append(input);
          const commit=node('button','确认保存所选事实');commit.type='button';commit.onclick=async()=>{try{await api('/attachments/'+item.id+'/save-facts?'+scope(),{method:'POST',body:JSON.stringify({version:item.version,target_case_id:$('caseSelect').value,facts:input.value.split('\n').map(x=>x.trim()).filter(Boolean)})});await loadCases();selection.remove();info('所选脱敏事实已保存到病例；临时附件仍会到期清理。');}catch(error){info(error.message);}};selection.append(commit);box.append(selection);
        };
        box.append(saveCase);
      }
      box.append(node('p','临时保留至 '+new Date(item.expires_at*1000).toLocaleString()+'；版本 '+item.version,'hint'));root.append(box);
      if(pending.length || item.status==='failed') box.open=true;
    }
  }
  function readPages() {return $('attachmentPages').value.split(/[,，\s]+/).filter(Boolean).map(Number);}
  async function poll(id, capturedEpoch) {
    if(capturedEpoch!==epoch || !records.has(id)) return;
    try {
      const item=await api('/attachments/'+id+'?'+scope());
      if(capturedEpoch!==epoch) return;
      records.set(id,item);render();
      if(item.status==='ready')info(item.pages.some(p=>p.requires_confirmation&&!p.confirmed)?'解析完成，请核对关键内容。':'解析完成，附件可用于本轮问题。');
      if(['queued','parsing'].includes(item.status)) timers.set(id,setTimeout(()=>poll(id,capturedEpoch),1200));
    } catch(error){if(/attachment_expired|attachment_not_found/.test(error.message)){discard(id);savedIds();render();}info('附件读取失败：'+error.message);}
  }
  async function upload(files) {
    if(files.length+records.size>3){info('每次对话最多 3 个附件，请先删除不需要的文件。');return;}
    const capturedEpoch=epoch;
    try {await window.BrowserUI.ensureSession();}catch(error){info(error.message);return;}
    if(capturedEpoch!==epoch)return;
    for(const file of files) {
      if(file.size>20*1024*1024){info(file.name+' 超过 20 MB。');continue;}
      const form=new FormData();form.append('file',file);
      for(const [key,value] of scope())form.append(key,value);
      form.append('purpose',$('attachmentPurpose').value);form.append('selected_pages',JSON.stringify(readPages()));
      info('正在上传 '+file.name+'…');
      try {
        const response=await fetch('/api/v1/attachments',{method:'POST',headers:{'X-Browser-Client':'1'},body:form});const item=await response.json();
        if(!response.ok)throw new Error(item.detail||'上传失败');
        if(capturedEpoch!==epoch)return;
        records.set(item.id,item);savedIds();render();poll(item.id,capturedEpoch);info('已上传，正在本机解析。');
      }catch(error){info(error.message);}
    }
  }
  $('attachmentInput').onchange=e=>{upload([...e.target.files]);e.target.value='';};
  const drop=$('attachmentDrop');
  drop.ondragover=e=>{e.preventDefault();drop.classList.add('dragging');};drop.ondragleave=()=>drop.classList.remove('dragging');
  drop.ondrop=e=>{e.preventDefault();drop.classList.remove('dragging');upload([...e.dataTransfer.files]);};
  window.AttachmentUI={ids:()=>[...records.keys()],clear:()=>{epoch++;timers.forEach(clearTimeout);previews.forEach(URL.revokeObjectURL);records.clear();timers.clear();previews.clear();drafts.clear();savedIds();render();info('');}};
  setInterval(()=>{let changed=false;for(const [id,item] of records){if(item.expires_at*1000<=Date.now()){discard(id);changed=true;}}if(changed){savedIds();render();info('临时附件已到期，浏览器内的解析内容已清除。');}},30000);
})();
