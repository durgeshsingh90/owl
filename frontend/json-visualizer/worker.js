"use strict";
// One worker per open document: it reads the file (gzip included), parses it, keeps
// the parsed value for searching and answers searches in slices so they can be
// cancelled. Closing or cancelling a document terminates its worker.
importScripts("parse.js?v=2", "search.js?v=2");

let document = null;
let searchId = 0;

async function readAll(blob) {
  const total = blob.size;
  const reader = blob.stream().getReader();
  const chunks = [];
  let loaded = 0, reported = 0;
  for (;;) {
    const {done, value} = await reader.read();
    if (done) break;
    chunks.push(value);
    loaded += value.length;
    if (loaded - reported > 2_000_000) {
      reported = loaded;
      postMessage({type: "progress", phase: "Reading", loaded, total});
    }
  }
  const bytes = new Uint8Array(loaded);
  let at = 0;
  for (const chunk of chunks) { bytes.set(chunk, at); at += chunk.length; }
  return bytes;
}

async function gunzip(bytes) {
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function open({blob, text, name, format}) {
  const notices = [];
  if (text === undefined) {
    let bytes = await readAll(blob);
    if (bytes[0] === 0x1f && bytes[1] === 0x8b) {
      postMessage({type: "progress", phase: "Decompressing", loaded: bytes.length, total: bytes.length});
      bytes = await gunzip(bytes);
      notices.push(`Decompressed gzip (${bytes.length.toLocaleString()} bytes).`);
    }
    const decoded = JVParse.decodeBytes(bytes);
    notices.push(...decoded.notices);
    text = decoded.text;
  }
  postMessage({type: "progress", phase: "Parsing", loaded: text.length, total: text.length});
  const result = JVParse.parseDocument(text, {name, format});
  document = result.value;
  postMessage({
    type: "opened",
    value: result.value,
    format: result.format,
    lines: result.lines || null,
    errors: result.errors,
    notices: [...notices, ...result.notices],
    textLength: text.length,
  });
}

function search({id, query, options}) {
  searchId = id;
  if (document === null || !query) return postMessage({type: "matches", id, matches: [], done: true, scanned: 0});
  let walker;
  try {
    walker = JVSearch.searcher(document, query, options);
  } catch (error) {
    return postMessage({type: "matches", id, matches: [], done: true, scanned: 0, error: `Invalid pattern: ${error.message}`});
  }
  const slice = () => {
    if (searchId !== id) return;
    const started = Date.now();
    let found = [], result;
    // Work for about 40 ms, then report and let a newer search (or cancel) arrive.
    do {
      result = walker.next(5000);
      found = found.concat(result.matches);
    } while (!result.done && Date.now() - started < 40);
    postMessage({type: "matches", id, matches: found, done: result.done, scanned: result.scanned});
    if (!result.done) setTimeout(slice, 0);
  };
  slice();
}

onmessage = async ({data}) => {
  try {
    if (data.type === "open") await open(data);
    else if (data.type === "search") search(data);
    else if (data.type === "cancel-search") searchId = -1;
  } catch (error) {
    postMessage({
      type: "error",
      message: error.message || String(error),
      line: error.line || null,
      column: error.column || null,
      snippet: error.snippet || "",
      caret: error.caret ?? null,
    });
  }
};
