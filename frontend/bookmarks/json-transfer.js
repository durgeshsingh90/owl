"use strict";
// Shared by JSON import/export. Only bookmark fields are transferred, never settings.
const bookmarkTransfer = (() => {
  const textFields = ["title", "description", "space", "spaceKey", "author", "lastEditor", "writtenAt", "confluenceUpdatedAt", "lastRefreshed", "contentText", "confluenceBaseUrl", "new_tag_date", "amended_tag_date", "tree_number", "tree_item", "saved_at", "modified", "last_refreshed"];
  const timestamp = (value, fallback = null) => {
    const result = typeof value === "number" ? value : Date.parse(value);
    return Number.isFinite(result) ? result : fallback;
  };
  function normalize(record) {
    if (!record || typeof record !== "object" || record.deleted === true) return null;
    let url;
    try { url = new URL(record.url); } catch { return null; }
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
    const item = {};
    for (const key of textFields) if (typeof record[key] === "string") item[key] = record[key];
    const pageId = String(record.page_id || url.pathname.match(/\/pages\/(\d+)(?:\/|$)/i)?.[1] || url.searchParams.get("pageId") || "");
    if (/^\d+$/.test(pageId)) item.page_id = pageId;
    Object.assign(item, {
      url: url.href, domain: url.hostname, title: item.title || url.hostname,
      sourceType: item.page_id || record.sourceType === "confluence" ? "confluence" : "web",
      spaceKey: item.spaceKey || (typeof record.space_key === "string" ? record.space_key : ""),
      confluenceUpdatedAt: item.confluenceUpdatedAt || item.modified || null,
      lastRefreshed: item.lastRefreshed || item.last_refreshed || null,
      added: timestamp(record.added ?? record.saved_at, Date.now()),
      updatedInOwlAt: timestamp(record.updatedInOwlAt ?? record.amended_tag_date),
      lastViewed: timestamp(record.lastViewed ?? record.last_opened),
      views: Math.max(0, Math.floor(Number(record.views ?? record.open_count) || 0)),
      favorite: record.favorite === true, pinned: record.pinned === true, custom: true,
      notes: typeof record.notes === "string" ? record.notes : "",
    });
    if (!Number.isFinite(item.views)) item.views = 0;
    for (const key of ["breadcrumb", "folderPath"]) item[key] = Array.isArray(record[key]) ? record[key].filter(value => typeof value === "string") : [];
    if (Array.isArray(record.ancestors)) item.ancestors = record.ancestors.filter(value => value && typeof value.title === "string").map(value => ({page_id: String(value.page_id || ""), title: value.title, url: typeof value.url === "string" ? value.url : ""}));
    if (Number.isFinite(Number(record.version))) item.version = Number(record.version);
    if (typeof record.contentText === "string") item.pageTextSizeBytes = new TextEncoder().encode(record.contentText).length;
    if (item.sourceType === "confluence" && !item.confluenceBaseUrl) item.confluenceBaseUrl = url.origin + url.pathname.split(/\/(?:spaces|pages|display|x)\//)[0];
    return item;
  }
  function identity(record) {
    if (record.page_id) {
      const url = new URL(record.url);
      return `${url.origin}${url.pathname.split(/\/(?:spaces|pages|display|x)\//)[0]}:page:${record.page_id}`;
    }
    return new URL(record.url).href;
  }
  function parse(raw, existing = []) {
    const data = JSON.parse(raw.replace(/^\uFEFF/, ""));
    const records = Array.isArray(data) ? data : data?.bookmarks;
    if (!Array.isArray(records)) throw new Error("JSON must contain an array of bookmarks.");
    const seen = new Set(existing.map(identity));
    const items = []; let skipped = 0;
    for (const record of records) {
      const item = normalize(record);
      if (!item || seen.has(identity(item))) { skipped++; continue; }
      if (!Array.isArray(data) && typeof data.notes?.[record.id] === "string") item.notes = data.notes[record.id];
      seen.add(identity(item)); items.push(item);
    }
    return { items, skipped };
  }
  return { normalize, identity, parse };
})();
if (typeof module !== "undefined") module.exports = bookmarkTransfer;
