"use strict";
(() => {
  const button=document.querySelector("#update-all-bookmarks"), status=document.querySelector("#bookmark-refresh-status");
  // Update all runs here; the backend schedule is told about it so the auto refresh label
  // shows it running, and it counts as today's refresh once every bookmark was tried.
  async function report(body) {
    const response = await fetch("/api/bookmarks/refresh-schedule/manual", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(typeof data.detail === "string" ? data.detail : "Could not update the refresh schedule.");
    return data;
  }
  button.addEventListener("click", async()=>{
    if (!window.bookmarkDatabaseReady || button.disabled) return;
    const targets=[...bookmarks];
    let owner;
    try { ({owner} = await report({action:"start", total:targets.length})); }
    catch (error) { toast(error.message); return; }
    const changed = () => window.dispatchEvent(new CustomEvent("owl-auto-refresh-changed", {detail:"bookmarks"}));
    changed();
    button.disabled=true;status.hidden=false;
    let done=0,failed=0,reported=0;
    try {
      for(const item of targets){
        status.textContent=`Refreshing ${done}/${targets.length} · ${failed} failed`;
        try{
          const data=await resolveBookmark(item.url);
          if (!bookmarks.includes(item)) { done++; continue; }
          Object.assign(item,data,{fetchError:""});
        }catch(error){item.fetchError=error.message;failed++;}
        done++;if(!await persist())throw Error("Database save failed. Reload before retrying.");render();
        if (Date.now()-reported>3000) { reported=Date.now(); void report({action:"progress", owner, completed:done, failed}).catch(()=>{}); }
      }
      // Bookmarks that failed get one more try at the end; only those failing again keep the error.
      for(const item of targets.filter(entry => entry.fetchError && bookmarks.includes(entry))){
        status.textContent=`Retrying failed pages · ${failed} failed`;
        try{
          const data=await resolveBookmark(item.url);
          if (!bookmarks.includes(item)) continue;
          Object.assign(item,data,{fetchError:""});failed--;
          if(!await persist())throw Error("Database save failed. Reload before retrying.");render();
        }catch(error){if(error.message.startsWith("Database save failed"))throw error;item.fetchError=error.message;}
      }
      status.textContent=`Updated ${done}/${targets.length} · ${failed} failed`;
      const previous = window.bookmarkLastUpdateAll;
      window.bookmarkLastUpdateAll = new Date().toISOString();
      if (!await persist()) {
        window.bookmarkLastUpdateAll = previous;
        throw Error("Could not save the Update all timestamp. Reload before retrying.");
      }
      document.querySelector("#bookmark-last-update").textContent="Last update all: "+new Date(window.bookmarkLastUpdateAll).toLocaleString();
      if(selectedBookmarkId!==null)showPageDetails(selectedBookmarkId);
    }catch(error){status.textContent=error.message;}
    finally{
      button.disabled=false;
      // Every bookmark tried: done for the day, even if some pages failed (each shows why).
      await report({action:"finish", owner, completed:done, failed}).catch(()=>{});
      changed();
    }
  });
})();
