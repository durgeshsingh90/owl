"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const displayDate = value => value && Number.isFinite(new Date(value).getTime()) ? new Date(value).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}) : '—';
  let roots=[], pages=[], selectedRoot=null, selectedBranch=null, maxEvent=0, pageLimit=100, treeLimit=1000, signature='', loading=false, pendingReload=false;
  let range=trackerDates.preset('all');
  const expanded=new Set();
  const labels={baseline:'Baseline',new:'New page',updated:'Updated',returned:'Returned',missing:'Not in subtree',pending:'Pending'};
  function error(message) { $('error').textContent=message; $('error').hidden=!message; }
  async function api(path,options={}) {
    const response=await fetch(path,{cache:'no-store',...options,headers:{'Content-Type':'application/json',...options.headers}});
    const data=await response.json();
    if(!response.ok) throw Error(typeof data.detail==='string'?data.detail:'The request could not be completed.');
    return data;
  }
  const endpoint = suffix => `/api/confluence-tracker/roots/${selectedRoot}${suffix}`;
  function currentRoot(){return roots.find(root=>root.id===selectedRoot);}
  async function refresh(force=false) {
    if(loading){pendingReload ||= force;return;}
    loading=true;
    try {
      const data=await api('/api/confluence-tracker/roots'); roots=data.roots;
      if(!roots.some(root=>root.id===selectedRoot)) selectedRoot=roots[0]?.id || null;
      const root=currentRoot();
      $('root-select').innerHTML=roots.length?roots.map(item=>`<option value="${item.id}">${esc(item.title)}${item.unread?' · '+item.unread+' changes':''}</option>`).join(''):'<option value="">No tracked roots</option>';
      $('root-select').value=selectedRoot || '';
      $('empty-state').hidden=Boolean(root);$('tracker-content').hidden=!root;
      $('check-now').disabled=!root || ['queued','discovering','downloading'].includes(root.status);
      if(!root){$('page-tree').textContent='No root pages yet.';return;}
      $('root-title').textContent=root.title;
      $('root-description').textContent=`Root page ${root.page_id} · ${root.base_url}`;
      $('total-pages').textContent=root.page_count.toLocaleString();$('unread-count').textContent=root.unread.toLocaleString();
      $('last-sync').textContent=displayDate(root.last_success);
      $('next-sync').textContent=['queued','discovering','downloading'].includes(root.status)?'In progress':root.next_run?displayDate(root.next_run*1000):'Shortly';
      const states={queued:'Queued for sync',discovering:'1. Discovering page IDs',downloading:'2. Downloading pages one by one',completed:'Up to date',failed:'Sync incomplete · retry in 2 hours'};
      $('sync-state').textContent=states[root.status]||root.status;
      $('sync-detail').textContent=root.status==='discovering'?`${root.total} page IDs found · downloads start after discovery`:`${root.completed} / ${root.total} processed${root.failed?' · '+root.failed+' failed':''} · daily automatic checks`;
      $('sync-progress').max=Math.max(1,root.total);$('sync-progress').value=root.completed;
      $('sync-progress').hidden=!['queued','discovering','downloading'].includes(root.status);
      $('sync-error').textContent=root.error;$('sync-error').hidden=!root.error;
      $('review-changes').disabled=!root.unread;
      const nextSignature=JSON.stringify([selectedRoot,root.status,root.completed,root.total,root.last_attempt,root.last_success,root.unread,root.failed]);
      if(force || nextSignature!==signature){
        const id=selectedRoot, result=await api(endpoint('/pages'));
        if(id!==selectedRoot){pendingReload=true;return;}
        pages=result.pages;maxEvent=result.max_event;
        if(!signature)expanded.add(root.page_id);signature=nextSignature;renderTree();renderArchive();renderList();
      }
      error('');
    }catch(failure){error(failure.message);}
    finally{loading=false;if(pendingReload){pendingReload=false;void refresh(true);}}
  }
  function treeData(){
    const root=currentRoot(), byId=new Map(pages.map(page=>[page.page_id,page])), children=new Map();
    for(const page of pages){
      let parent=page.parent_id;
      if(page.page_id===root?.page_id) parent=null;
      else if(!byId.has(parent)) parent=[...(page.ancestors||[])].reverse().find(item=>byId.has(String(item.page_id)))?.page_id || (byId.has(root?.page_id)?root.page_id:null);
      if(parent===page.page_id) parent=null;
      if(!children.has(parent))children.set(parent,[]);
      children.get(parent).push(page);
    }
    for(const list of children.values())list.sort((a,b)=>a.title.localeCompare(b.title));
    return {byId,children};
  }
  function renderTree(){
    const {children}=treeData();let count=0;
    function nodes(list,visited=new Set()){
      return list.map(page=>{
        if(visited.has(page.page_id)||count>=treeLimit)return '';
        count++;const next=new Set(visited);next.add(page.page_id);
        const descendants=children.get(page.page_id)||[],open=expanded.has(page.page_id);
        return `<li><div class="tree-row">${descendants.length?`<button class="tree-expander" data-expand="${esc(page.page_id)}" aria-expanded="${open}" aria-label="${open?'Collapse':'Expand'} ${esc(page.title)}">${open?'▾':'▸'}</button>`:'<span class="tree-expander">·</span>'}<button class="tree-select" data-branch="${esc(page.page_id)}" aria-current="${selectedBranch===page.page_id}" title="${esc(page.title)} · ${esc(page.page_id)}">${esc(page.title)}</button>${page.unread?'<span class="tree-dot" title="Unreviewed changes">•</span>':''}</div>${open && descendants.length?`<ul>${nodes(descendants,next)}</ul>`:''}</li>`;
      }).join('');
    }
    const markup=nodes(children.get(null)||[]);
    $('page-tree').innerHTML=markup?`<ul>${markup}</ul>${count>=treeLimit?'<button id="more-tree">Show more tree entries</button>':''}`:'Page IDs and the tree will appear as the scan progresses.';
  }
  function branchIds(){
    if(!selectedBranch)return null;
    const {children}=treeData(), ids=new Set(), queue=[selectedBranch];
    while(queue.length){const id=queue.pop();if(ids.has(id))continue;ids.add(id);for(const child of children.get(id)||[])queue.push(child.page_id);}
    return ids;
  }
  function filteredPages(){
    const query=$('page-search').value.trim().toLocaleLowerCase(),filter=$('change-filter').value,dateField=$('date-field').value,branch=branchIds();
    return pages.filter(page=>(!branch||branch.has(page.page_id))&&(!query||[page.title,page.page_id,page.author,page.lastEditor,page.space].some(value=>String(value||'').toLocaleLowerCase().includes(query)))&&trackerDates.inRange(page[dateField],range)&&(filter==='all'||filter==='unread'&&page.unread||filter==='failed'&&page.download_status==='failed'||filter==='new'&&page.change_kind==='new'||filter==='updated'&&['updated','returned'].includes(page.change_kind)||filter==='missing'&&!page.present)).sort((a,b)=>(Date.parse(b[dateField])||0)-(Date.parse(a[dateField])||0)||a.title.localeCompare(b.title));
  }
  function renderList(){
    const list=filteredPages(),dateField=$('date-field').value;
    $('result-count').textContent=`${list.length.toLocaleString()} of ${pages.length.toLocaleString()} pages${selectedBranch?' in selected branch':''}`;
    $('clear-branch').hidden=!selectedBranch;$('no-results').hidden=Boolean(list.length);$('load-more').hidden=list.length<=pageLimit;
    let group='';
    $('page-list').innerHTML=list.slice(0,pageLimit).map(page=>{
      const label=trackerDates.group(page[dateField]);let heading='';
      if(label!==group){group=label;heading=`<tr class="date-heading"><td colspan="11">${esc(label)}</td></tr>`;}
      const change=page.download_status==='failed'?'failed':page.change_kind;
      const url=page.url && /^https?:\/\//.test(page.url)?page.url:null;
      return heading+`<tr class="${page.unread?'unread':''}"><td>${url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" data-open-page="${esc(page.page_id)}">${esc(page.title)} ↗</a>`:esc(page.title)}<small>${page.error?esc(page.error):page.breadcrumb?.length?esc(page.breadcrumb.join(' / ')):page.download_status==='pending'?'Waiting to download':''}</small></td><td>${esc(page.page_id)}</td><td><span class="badge ${esc(change)}">${esc(change==='failed'?'Download failed':labels[change]||'Unchanged')}</span>${page.unread?`<small>${page.unread} unreviewed</small>`:''}</td><td>${page.opens||0}</td><td>${esc(displayDate(page.confluenceUpdatedAt))}</td><td>${esc(displayDate(page.writtenAt))}</td><td>${esc(page.lastEditor||'—')}</td><td>${esc(page.author||'—')}</td><td>${esc(page.version??'—')}</td><td>${esc(page.space||'—')}</td><td><button data-details="${esc(page.page_id)}" ${!page.url?'disabled':''}>Details</button></td></tr>`;
    }).join('');
  }
  function renderArchive(){
    const dateField=$('date-field').value,months=new Set(),years=new Set();
    for(const page of pages){const value=page[dateField];if(!value||!Number.isFinite(Date.parse(value)))continue;const key=trackerDates.key(value);months.add(key.slice(0,7));years.add(key.slice(0,4));}
    $('archive-range').innerHTML='<option value="">Choose a month or year…</option>'+[...years].sort().reverse().map(year=>`<option value="${year}">All of ${year}</option>`).join('')+[...months].sort().reverse().map(month=>`<option value="${month}">${esc(new Date(month+'-01T12:00:00').toLocaleDateString(undefined,{month:'long',year:'numeric'}))}</option>`).join('');
  }
  function applyRange(value,label){range=value;pageLimit=100;$('range-label').textContent=label+' ▾';$('range-picker').open=false;$('range-error').textContent='';renderList();}
  $('range-preset').addEventListener('change',event=>{applyRange(trackerDates.preset(event.target.value),event.target.selectedOptions[0].textContent);$('archive-range').value='';});
  $('archive-range').addEventListener('change',event=>{const value=event.target.value;if(!value)return;const [y,m]=value.split('-').map(Number);applyRange({start:new Date(y,(m||1)-1,1),end:m?new Date(y,m,1):new Date(y+1,0,1)},event.target.selectedOptions[0].textContent);});
  $('custom-range').addEventListener('submit',event=>{event.preventDefault();try{applyRange(trackerDates.custom($('range-start').value,$('range-end').value),`${$('range-start').value} – ${$('range-end').value}`);}catch(failure){$('range-error').textContent=failure.message;}});
  for(const id of ['page-search','change-filter','date-field'])$(id).addEventListener(id==='page-search'?'input':'change',()=>{pageLimit=100;if(id==='change-filter'&&$('change-filter').value!=='all')$('date-field').value='changed_at';renderArchive();renderList();});
  $('load-more').onclick=()=>{pageLimit+=100;renderList();};
  $('root-select').onchange=()=>{selectedRoot=Number($('root-select').value)||null;selectedBranch=null;expanded.clear();signature='';pageLimit=100;void refresh(true);};
  $('page-tree').onclick=event=>{const expand=event.target.closest('[data-expand]'),branch=event.target.closest('[data-branch]');if(expand){const id=expand.dataset.expand;expanded.has(id)?expanded.delete(id):expanded.add(id);renderTree();}else if(branch){selectedBranch=branch.dataset.branch;pageLimit=100;renderTree();renderList();}else if(event.target.id==='more-tree'){treeLimit+=1000;renderTree();}};
  $('collapse-tree').onclick=()=>{expanded.clear();renderTree();};
  const clearBranch=()=>{selectedBranch=null;pageLimit=100;renderTree();renderList();};$('all-pages').onclick=clearBranch;$('clear-branch').onclick=clearBranch;
  $('check-now').onclick=async()=>{const id=selectedRoot;$('check-now').disabled=true;try{await api(`/api/confluence-tracker/roots/${id}/sync`,{method:'POST'});await refresh(true);}catch(failure){error(failure.message);$('check-now').disabled=false;}};
  $('review-changes').onclick=async()=>{try{await api(endpoint('/review'),{method:'POST',body:JSON.stringify({through_id:maxEvent})});await refresh(true);}catch(failure){error(failure.message);}};
  function openRootDialog(){$('root-input').value='';$('root-feedback').textContent='';$('root-dialog').showModal();$('root-input').focus();}
  $('add-root').onclick=openRootDialog;$('empty-add-root').onclick=openRootDialog;
  $('root-form').onsubmit=async event=>{event.preventDefault();$('root-submit').disabled=true;$('root-feedback').textContent='Adding root…';try{const result=await api('/api/confluence-tracker/roots',{method:'POST',body:JSON.stringify({root:$('root-input').value})});selectedRoot=result.id;selectedBranch=null;signature='';$('root-dialog').close();await refresh(true);}catch(failure){$('root-feedback').textContent=failure.message;}finally{$('root-submit').disabled=false;}};
  document.addEventListener('click',event=>{const close=event.target.closest('[data-close]');if(close)$(close.dataset.close).close();});
  $('connection-settings').onclick=async()=>{try{const data=await api('/bookmarks/settings/workspace/');$('base-url').value=data.configuration.baseUrl;$('verify-ssl').checked=data.configuration.verifySsl;$('pat').value='';$('connection-feedback').textContent='';$('settings-dialog').showModal();}catch(failure){error(failure.message);}};
  $('settings-dialog').addEventListener('close',()=>{$('pat').value='';});
  $('settings-form').onsubmit=async event=>{event.preventDefault();$('save-connection').disabled=true;$('connection-feedback').textContent='Testing connection…';try{const form=new URLSearchParams(new FormData(event.target));await api('/bookmarks/settings/save/',{method:'POST',body:form,headers:{'Content-Type':'application/x-www-form-urlencoded'}});$('pat').value='';$('connection-feedback').textContent='Connection saved. Add a root or use Check now.';}catch(failure){$('connection-feedback').textContent=failure.message;}finally{$('save-connection').disabled=false;}};
  $('page-list').addEventListener('click',async event=>{
    const link=event.target.closest('[data-open-page]'),details=event.target.closest('[data-details]');
    if(link){const id=selectedRoot,pageId=link.dataset.openPage;try{const result=await api(`/api/confluence-tracker/roots/${id}/pages/${encodeURIComponent(pageId)}/open`,{method:'POST',keepalive:true});if(id===selectedRoot){const page=pages.find(item=>item.page_id===pageId);if(page)page.opens=result.opens;renderList();}}catch(failure){error('Page opened, but its open count could not be saved.');}}
    if(details)void showDetails(details.dataset.details);
  });
  async function showDetails(pageId){
    const id=selectedRoot;$('metadata-title').textContent='Page '+pageId;$('metadata-content').textContent='Loading metadata…';$('metadata-dialog').showModal();
    try{
      const data=await api(`/api/confluence-tracker/roots/${id}/pages/${encodeURIComponent(pageId)}`);if(!$('metadata-dialog').open||id!==selectedRoot)return;
      const meta=data.metadata;$('metadata-title').textContent=meta.title;
      const summary=Object.entries(meta).filter(([key,value])=>!['rawMetadata','contentText'].includes(key)&&!Array.isArray(value)&&typeof value!=='object');
      $('metadata-content').innerHTML=`<dl class="metadata-grid">${summary.map(([key,value])=>`<div><dt>${esc(key)}</dt><dd>${esc(value??'—')}</dd></div>`).join('')}</dl><h3>Change history</h3>${data.changes.length?data.changes.map(change=>`<article class="change-entry"><strong>${esc(labels[change.kind]||change.kind)}</strong> · ${esc(displayDate(change.detected_at))}<pre>${esc(JSON.stringify(change.summary,null,2))}</pre></article>`).join(''):'<p>No changes detected after the baseline yet.</p>'}<details><summary>Cached page text</summary><pre>${esc(meta.contentText||'No text available.')}</pre></details>${data.previous_content!==null?`<details><summary>Text before the latest detected change</summary><pre>${esc(data.previous_content)}</pre></details>`:''}<details><summary>All returned Confluence metadata</summary><pre>${esc(JSON.stringify(meta.rawMetadata||meta,null,2))}</pre></details>`;
    }catch(failure){$('metadata-content').textContent=failure.message;}
  }
  async function poll(){await refresh();setTimeout(poll,5000);}void poll();
})();
