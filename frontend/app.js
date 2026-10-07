(function(){
  'use strict';
  var $=function(id){return document.getElementById(id);};
  var input=$('urlInput'), goBtn=$('goBtn'), detect=$('detect'), dropZone=$('dropZone');
  var detDot=$('detDot'), detName=$('detName'), detHint=$('detHint');
  var track=$('track'), trackState=$('trackState'), trackEmpty=$('trackEmpty'), trackRun=$('trackRun');
  var rail=$('rail'), flow=$('flow'), nodeDst=$('nodeDst');
  var pct=$('pct'), pctNote=$('pctNote');
  var recList=$('recList'), recCount=$('recCount'), noteEl=$('listNote'), modeBadge=$('modeBadge');
  var toastEl=$('toast'), toastTimer=null;
  var tasks=[], pollers={}, prevLen=0;
  var GO_HTML=goBtn.innerHTML;

  var token=new URLSearchParams(location.search).get('token')||'';
  function withToken(path){
    return path+(token?((path.indexOf('?')>-1?'&':'?')+'token='+encodeURIComponent(token)):'');
  }
  function api(path,opts){
    opts=opts||{};
    return fetch(withToken(path),Object.assign({headers:{'Content-Type':'application/json'}},opts)).then(function(r){
      return r.json().then(function(j){
        if(!r.ok) throw new Error((j&&j.detail)||('请求失败 '+r.status));
        return j;
      });
    });
  }

  var PF={
    bilibili:{name:'哔哩哔哩',color:'#23ade5',icon:'哔',hint:'将获取最高可用清晰度（高清可能需登录）'},
    douyin:  {name:'抖音',    color:'#fe2c55',icon:'抖',hint:'将解析无水印直链'},
    youtube: {name:'YouTube', color:'#ff0033',icon:'YT',hint:'将选择最高可用画质'},
    hls:     {name:'HLS 流',  color:'#ffb454',icon:'H',hint:'将按 m3u8 分片下载并合并为 MP4'},
    other:   {name:'通用链接', color:'#8b98ab',icon:'网',hint:'将尝试解析直链或 m3u8 流'}
  };
  function plat(s){return PF[s]||PF.other;}

  var ST={
    queued:      ['st-idle','QUEUED','color:var(--dim);border:1px solid var(--line);'],
    parsing:     ['st-parse','PARSE','color:var(--amber);background:var(--amber-dim);'],
    downloading: ['st-run','LIVE','color:var(--accent);background:var(--accent-dim);'],
    merging:     ['st-run','MUX','color:var(--accent);background:var(--accent-dim);'],
    done:        ['st-done','DONE','color:var(--green);background:rgba(74,222,128,.12);'],
    failed:      ['st-fail','FAIL','color:var(--red);background:rgba(248,113,113,.12);']
  };

  /* ---------- 服务状态 ---------- */
  api('/api/health').then(function(){
    modeBadge.textContent='ONLINE'; modeBadge.classList.add('on');
  }).catch(function(){
    modeBadge.textContent='OFFLINE'; modeBadge.classList.add('off');
  });

  /* ---------- 平台即时识别 ---------- */
  function detectPlatform(url){
    if(/bilibili\.com|b23\.tv/i.test(url)) return PF.bilibili;
    if(/douyin\.com|iesdouyin\.com/i.test(url)) return PF.douyin;
    if(/youtube\.com|youtu\.be/i.test(url)) return PF.youtube;
    if(/\.m3u8/i.test(url)) return PF.hls;
    return PF.other;
  }
  input.addEventListener('input',function(){
    var v=input.value.trim();
    if(!/^https?:\/\//i.test(v)){detect.hidden=true;return;}
    var p=detectPlatform(v);
    detDot.style.background=p.color; detName.textContent=p.name; detHint.textContent=p.hint;
    detect.hidden=false;
  });

  function toast(msg){
    toastEl.textContent=msg;
    toastEl.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer=setTimeout(function(){toastEl.classList.remove('show');},2600);
  }
  function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function isHttp(v){return /^https?:\/\//i.test(v);}

  /* ---------- 任务 ---------- */
  function applyTask(t){
    var i=tasks.findIndex(function(x){return x.id===t.id;});
    if(i>-1) tasks[i]=t; else tasks.unshift(t);
  }
  function poll(tid){
    if(pollers[tid]) clearInterval(pollers[tid]);
    pollers[tid]=setInterval(function(){
      api('/api/tasks/'+tid).then(function(t){
        applyTask(t); render(); updateTrack();
        if(t.status==='done'||t.status==='failed'){
          clearInterval(pollers[tid]); delete pollers[tid];
          if(t.status==='done'){flashTrack(); toast('下载完成：'+t.title);}
          else toast('下载失败：'+(t.error||'未知原因'));
        }
      }).catch(function(){ clearInterval(pollers[tid]); delete pollers[tid]; });
    },1200);
  }
  function startDownload(url){
    return api('/api/download',{method:'POST',body:JSON.stringify({url:url})}).then(function(t){
      applyTask(t); render(); updateTrack(); poll(t.id);
      return t;
    });
  }

  /* ---------- 轨道 ---------- */
  function updateTrack(){
    var active=null;
    for(var i=0;i<tasks.length;i++){
      var s=tasks[i].status;
      if(s==='parsing'||s==='downloading'||s==='merging'){active=tasks[i];break;}
    }
    if(!active){
      var last=tasks[0];
      if(!last){setTrack('idle',null);return;}
      if(last.status==='done') setTrack('done',last);
      else if(last.status==='failed') setTrack('fail',last);
      else setTrack('idle',null);
      return;
    }
    if(active.status==='parsing') setTrack('parse',active);
    else if(active.status==='merging') setTrack('done',active,true);
    else setTrack('run',active);
  }
  function setTrack(state,t,forceFull){
    track.dataset.state=state;
    trackState.className='st '+({idle:'st-idle',parse:'st-parse',run:'st-run',done:'st-done',fail:'st-fail'})[state];
    trackState.textContent=({idle:'IDLE',parse:'PARSING',run:'DOWNLOADING',done:'DONE',fail:'FAILED'})[state];
    trackEmpty.style.display=state==='idle'?'flex':'none';
    trackRun.style.display=state==='idle'?'none':'flex';
    rail.dataset.phase=state;
    flow.className='flow'+(state==='run'?' on':state==='done'?' solid':state==='fail'?' err':'');
    nodeDst.classList.toggle('on',state==='done');
    nodeDst.classList.toggle('err',state==='fail');
    if(state==='parse'){pct.innerHTML='—';flow.style.width='10%';}
    else if(state==='run'){
      var pr=t?Math.max(0,Math.min(100,Math.round((t.progress||0)*100))):0;
      pct.innerHTML=pr+'<span class="unit">%</span>';flow.style.width=pr+'%';
    }else if(state==='done'){pct.innerHTML='100<span class="unit">%</span>';flow.style.width='100%';}
    else if(state==='fail'){pct.innerHTML='ERR';flow.style.width='42%';}
    pctNote.innerHTML=t&&t.title?('<span class="fn">SOURCE</span> '+esc(t.title)):'<span class="fn">SOURCE</span> —';
    if(state==='done'&&t&&t.size) pctNote.innerHTML+=' · '+t.size+' MB';
    if(state==='fail'&&t&&t.error) pctNote.innerHTML+=' · '+esc(t.error);
  }
  function flashTrack(){
    track.classList.remove('flash'); void track.offsetWidth; track.classList.add('flash');
    setTimeout(function(){track.classList.remove('flash');},600);
  }

  /* ---------- 渲染记录列表 ---------- */
  function render(){
    recCount.textContent=tasks.length+' RECORDS';
    var hasRun=tasks.some(function(t){return t.status==='parsing'||t.status==='downloading'||t.status==='merging';});
    noteEl.style.display=hasRun?'block':'none';
    noteEl.textContent=hasRun?'任务与进度实时同步自后端服务。':'';
    var newRow=tasks.length>prevLen;
    prevLen=tasks.length;
    if(tasks.length===0){
      recList.innerHTML='<div class="empty">暂无记录。粘贴一个视频链接，点击「解析下载」开始。</div>';
      return;
    }
    var html='';
    tasks.forEach(function(t,i){
      var st=ST[t.status]||ST.queued;
      var meta='<span class="url">'+esc(t.url)+'</span>';
      if(t.status==='done'&&t.size) meta+='<span>'+esc(t.size)+' MB</span>';
      if(t.status==='failed'&&t.error) meta+='<span class="err">'+esc(t.error)+'</span>';
      if(t.status==='downloading') meta+='<span>'+Math.max(0,Math.min(100,Math.round((t.progress||0)*100)))+'%</span>';
      var bar=(t.status==='downloading'||t.status==='merging')
        ?'<div class="bar-wrap"><div class="bar" style="width:'+(t.status==='merging'?100:Math.max(0,Math.min(100,Math.round((t.progress||0)*100))))+'%"></div></div>':'';
      var acts='<button class="btn btn-ghost btn-sm act-copy" data-id="'+t.id+'" title="复制下载链接">复制链接</button>';
      if(t.status==='done') acts+='<button class="btn btn-ghost btn-sm act-open" data-id="'+t.id+'">打开</button>';
      else if(t.status==='failed') acts+='<button class="btn btn-ghost btn-sm act-retry" data-id="'+t.id+'">重试</button>';
      else acts+='<button class="btn btn-ghost btn-sm act-cancel" data-id="'+t.id+'">取消</button>';
      acts+='<button class="del act-del" data-id="'+t.id+'" aria-label="删除记录">'+
        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="square"><path d="M6 6l12 12M18 6L6 18"/></svg></button>';
      var p=plat(t.platform);
      html+='<div class="rec-row'+(newRow&&i===0?' row-enter':'')+'" data-id="'+t.id+'">'+
        '<div class="rec-icon" style="background:'+p.color+'">'+p.icon+'</div>'+
        '<div class="rec-main"><div class="rec-title">'+esc(t.title||'解析中…')+'</div><div class="rec-meta">'+meta+'</div>'+bar+'</div>'+
        '<span class="rec-status" style="'+st[2]+'">'+st[1]+'</span>'+
        '<div class="rec-actions">'+acts+'</div>'+
      '</div>';
    });
    recList.innerHTML=html;
  }

  /* ---------- 删除 / 取消 ---------- */
  function removeTask(id,animate){
    var row=document.querySelector('.rec-row[data-id="'+id+'"]');
    var doDelete=function(){
      return api('/api/tasks/'+id,{method:'DELETE'}).then(function(){
        tasks=tasks.filter(function(x){return x.id!==id;});
        if(pollers[id]){clearInterval(pollers[id]);delete pollers[id];}
        render(); updateTrack();
      }).catch(function(){});
    };
    if(animate&&row){
      row.classList.add('row-leave');
      setTimeout(doDelete,230);
    }else{
      doDelete();
    }
  }

  /* ---------- 事件 ---------- */
  goBtn.addEventListener('click',function(){
    var v=input.value.trim();
    if(!isHttp(v)){toast('请先粘贴一个以 http(s):// 开头的视频链接');input.focus();return;}
    goBtn.disabled=true; goBtn.innerHTML='提交中…';
    startDownload(v).then(function(){
      goBtn.disabled=false; goBtn.innerHTML=GO_HTML;
      input.value=''; detect.hidden=true; input.focus();
      toast('任务已创建，开始解析');
    }).catch(function(e){
      goBtn.disabled=false; goBtn.innerHTML=GO_HTML;
      toast(e.message||'创建任务失败');
    });
  });
  input.addEventListener('keydown',function(e){if(e.key==='Enter')goBtn.click();});

  ['dragenter','dragover'].forEach(function(ev){
    dropZone.addEventListener(ev,function(e){e.preventDefault();dropZone.classList.add('drop-hover');});
  });
  ['dragleave','drop'].forEach(function(ev){
    dropZone.addEventListener(ev,function(e){e.preventDefault();dropZone.classList.remove('drop-hover');});
  });
  dropZone.addEventListener('drop',function(e){
    var text=(e.dataTransfer&&e.dataTransfer.getData('text'))||'';
    if(isHttp(text.trim())){input.value=text.trim();input.dispatchEvent(new Event('input'));toast('已接收链接，点击「解析下载」');}
    else toast('拖入的内容不是有效链接');
  });

  recList.addEventListener('click',function(e){
    var btn=e.target.closest('button'); if(!btn) return;
    var id=btn.dataset.id;
    var t=tasks.find(function(x){return x.id===id;}); if(!t) return;
    if(btn.classList.contains('act-del')) removeTask(id,true);
    else if(btn.classList.contains('act-cancel')){removeTask(id,true);toast('任务已取消');}
    else if(btn.classList.contains('act-retry')){
      toast('重新创建下载任务');
      startDownload(t.url);
    }else if(btn.classList.contains('act-open')){
      window.location.href=withToken('/api/files/'+id);
    }else if(btn.classList.contains('act-copy')){
      var dlUrl = (t.status==='done') ? (window.location.origin + withToken('/api/files/'+id)) : t.url;
      navigator.clipboard.writeText(dlUrl).then(function(){
        var origText = btn.textContent;
        btn.textContent = '已复制';
        toast('下载链接已复制到剪贴板');
        setTimeout(function(){ btn.textContent = origText; }, 1500);
      }).catch(function(){
        prompt('请手动复制下载链接：', dlUrl);
      });
    }
  });

  $('clearBtn').addEventListener('click',function(){
    var ids=tasks.slice();
    ids.forEach(function(t){ if(t.id) removeTask(t.id,false); });
    toast('正在清理记录');
  });

  /* ---------- 初始化：加载服务端任务 ---------- */
  api('/api/tasks').then(function(res){
    tasks=(res.tasks||[]).slice();
    prevLen=tasks.length;
    render(); updateTrack();
    tasks.forEach(function(t){
      if(t.status==='parsing'||t.status==='downloading'||t.status==='merging') poll(t.id);
    });
  }).catch(function(){});
})();
