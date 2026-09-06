"use strict";
window.resolveBookmark = async url => {
  const response = await fetch("/api/bookmarks/resolve", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url})});
  const data = await response.json();
  if (!response.ok) throw Error(typeof data.detail === "string" ? data.detail : "Unable to fetch bookmark details.");
  return data;
};
document.querySelector("#bookmark-search-form").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.querySelector("#bookmark-search"), url = parseBookmarkUrl(input.value);
  if (!url || !window.bookmarkDatabaseReady) return;
  const button = document.querySelector("#add-bookmark");
  if (button.disabled) return;
  button.disabled = true;
  try {
    if (bookmarks.some(item => item.url === url.href)) {toast("This bookmark is already saved."); return;}
    toast("Checking page and fetching details…");
    const data = await resolveBookmark(url.href);
    // Recheck after the network request to avoid duplicate submissions.
    if (bookmarks.some(item => item.url === data.url || (data.page_id && item.page_id === data.page_id && item.confluenceBaseUrl === data.confluenceBaseUrl))) {
      toast("This page is already saved."); return;
    }
    const added = {...data,id:nextBookmarkId(),views:0,lastViewed:null,added:Date.now(),favorite:false,pinned:false,custom:true};
    bookmarks.push(added);
    if (!await persist()) throw Error("Bookmark has not been saved. Reload to resolve a conflicting edit, then add it again.");
    view="all";domain=data.sourceType === "confluence" ? "" : data.domain;selectedDomainGroup="";selectedPerson="";query="";
    input.value="";button.hidden=true;render();
    const row = document.querySelector(`[data-bookmark-row="${added.id}"]`);
    if (row) {
      for (let parent = row.parentElement; parent; parent = parent.parentElement) {
        if (parent.matches("details[data-branch]")) {
          collapsedBranches.delete(parent.dataset.branch);
          parent.open = true;
        }
      }
      showPageDetails(added.id);
      const number = row.querySelector(".tree-number")?.textContent;
      const link = row.querySelector(".tree-title");
      link?.focus({preventScroll:true});
      row.scrollIntoView({block:"center",behavior:"smooth"});
      toast(`Bookmark added at ${number || "the selected position"}: ${added.title}`);
    } else {
      toast("Bookmark and page details saved.");
    }
  } catch(error) {toast(error.message);}
  finally {button.disabled=false;}
});
