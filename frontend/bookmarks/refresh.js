"use strict";
(() => {
  const button=document.querySelector("#update-all-bookmarks"), status=document.querySelector("#bookmark-refresh-status");
  button.addEventListener("click", async()=>{
    if (!window.bookmarkDatabaseReady || button.disabled) return;
    button.disabled=true;status.hidden=false;
    let done=0,failed=0;
    const targets=[...bookmarks];
    try {
      for(const item of targets){
        status.textContent=`Refreshing ${done}/${targets.length} · ${failed} failed`;
        try{
          const data=await resolveBookmark(item.url);
          if (!bookmarks.includes(item)) continue;
          Object.assign(item,data,{fetchError:""});
        }catch(error){item.fetchError=error.message;failed++;}
        done++;if(!await persist())throw Error("Database save failed. Reload before retrying.");render();
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
    finally{button.disabled=false;}
  });
})();
