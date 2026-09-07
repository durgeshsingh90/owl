"use strict";
(() => {
  let statuses = new Map(), scopes = new Map(), searchSequence = 0, searchTimer;
  window.bookmarkFolderDownloadButton = path => {
    const candidates = bookmarks.filter(item => item.sourceType === 'confluence' &&
      JSON.stringify([item.space || 'Pages', ...(item.breadcrumb || [])].slice(0, path.length)) === JSON.stringify(path));
    const base = candidates[0]?.confluenceBaseUrl || '';
    const key = JSON.stringify([base, ...path]);
    const roots = [...new Set(candidates.flatMap(item => {
      const ancestor = item.ancestors?.find(a => a.title === path[path.length - 1]);
      return ancestor?.page_id ? [String(ancestor.page_id)] : [];
    }))];
    const space = candidates[0]?.spaceKey || '';
    const rootTitle = path.length > 1 ? path[path.length - 1] : '';
    scopes.set(key, {folder_key:key,space_key:space,root_ids:roots,root_title:rootTitle,base_url:base});
    const status = statuses.get(key), running = status?.status === 'running', done = status?.status === 'completed';
    const supported = !!(space || roots.length);
    const title = !supported ? 'No Confluence page or space identity available for this folder' :
      done ? `${status.count} pages downloaded for search. Click to refresh.` : running ? (status.phase === "discovering" ? `Finding pages: ${status.total || 0} found` : `Downloading: ${status.count}/${status.total} pages`) : status?.error || 'Download all pages in this folder for search';
    return `<span class="folder-download-progress"><button type="button" class="folder-content-download ${done ? 'download-complete' : ''}" data-folder-download="${esc(key)}" title="${esc(title)}" aria-label="${esc(title)}" ${!supported || running ? 'disabled' : ''}>${done ? '✓' : running ? '…' : '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M12 3v12m-4-4 4 4 4-4M4 17v4h16v-4" fill="none" stroke="currentColor" stroke-width="2"/></svg>'}</button>${running ? `<span class="tree-context" role="status">${status.phase === 'discovering' ? `Finding pages · ${status.total || 0} found` : `Downloading ${status.count}/${status.total}`}</span>` : ''}</span>`;
  };
  async function refresh() {
    try {
      const response = await fetch('/api/bookmarks/downloads');
      if (!response.ok) return;
      const updated = new Map((await response.json()).map(item => [item.folder_key,item]));
      for (const [key, status] of updated) {
        if (status.status === 'failed' && statuses.get(key)?.status === 'running') {
          toast(status.error || 'Folder download failed.');
        }
      }
      statuses = updated;
      document.querySelectorAll('[data-folder-download]').forEach(button => {
        const key = button.dataset.folderDownload, scope = scopes.get(key);
        if (!scope) return;
        const path = JSON.parse(key).slice(1);
        (button.closest(".folder-download-progress") || button).outerHTML = bookmarkFolderDownloadButton(path);
      });
    } catch {}
  }
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-folder-download]');
    if (!button) return;
    event.preventDefault(); event.stopPropagation();
    const scope = scopes.get(button.dataset.folderDownload);
    button.disabled = true;
    try {
      const response = await fetch('/api/bookmarks/downloads', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(scope)});
      if (!response.ok) throw Error((await response.json()).detail || 'Download could not start.');
      await refresh();
      toast('Downloading folder pages for search.');
    } catch(error) {button.disabled=false;toast(error.message);}
  });
  window.searchDownloadedBookmarkPages = (savedMatches = []) => {
    clearTimeout(searchTimer);
    const sequence = ++searchSequence;
    document.getElementById('downloaded-page-results')?.remove();
    const showDownloaded = document.getElementById("show-downloaded-pages").checked;
    if (!query.trim() && !showDownloaded) return;
    searchTimer=setTimeout(async()=>{
      const fields=[...document.querySelectorAll('[name="bookmark-search-field"]:checked')].map(el=>el.value);
      const mode=document.querySelector('[name="bookmark-search-mode"]:checked').value;
      try {
        const response=await fetch('/api/bookmarks/downloaded-search?'+new URLSearchParams({q:query,fields:fields.join(','),mode,include_all:String(showDownloaded)}));
        if(!response.ok) throw Error();
        const data=await response.json();if(sequence!==searchSequence)return;
        const matches=[...savedMatches], downloaded=[];
        for (const item of data.items) {
          const host=new URL(item.url).hostname;
          if(domain && host!==domain)continue;
          if(selectedDomainGroup && !domainGroups.find(g=>g.id===selectedDomainGroup)?.domains.includes(host))continue;
          // Personal activity filters apply to saved bookmarks only.
          if(selectedPerson || !['all'].includes(view))continue;
          const saved=bookmarks.find(b=>b.url===item.url || (String(b.page_id)===item.page_id && (b.confluenceBaseUrl ? item.url.startsWith(b.confluenceBaseUrl+'/') : b.domain===host)));
          if(saved) {if(!matches.some(b=>b.id===saved.id))matches.push(saved);continue;}
          downloaded.push({...item,id:'downloaded:'+item.url,domain:host,views:0,searchOnly:true});
        }
        renderBookmarkTree(matches,downloaded,false);
        updateBookmarkSearchCount(matches.length + downloaded.length);
        document.getElementById('bookmark-empty').hidden=matches.length+downloaded.length>0;
        document.getElementById('bookmark-summary').textContent=`${matches.length} bookmarks · ${downloaded.length} downloaded pages${query.trim() ? " matching search" : ""}`;
        document.getElementById('bookmark-total').textContent=`Showing ${matches.length} saved bookmarks and ${downloaded.length} downloaded pages`;
      } catch {if(sequence===searchSequence){updateBookmarkSearchCount(savedMatches.length);document.getElementById('bookmark-search-count').textContent += ' · saved bookmarks only';toast('Could not search downloaded pages.');}}
    },250);
  };
  document.addEventListener('DOMContentLoaded', () => {
    const toggle = document.getElementById('show-downloaded-pages');
    try { toggle.checked = localStorage.getItem('owl-show-downloaded-pages') === 'true'; } catch {}
    toggle.addEventListener('change', () => {
      try { localStorage.setItem('owl-show-downloaded-pages', String(toggle.checked)); } catch {}
      render();
    });
    refresh();
  });
  setInterval(()=>{if(!document.hidden)refresh();},4000);
})();
