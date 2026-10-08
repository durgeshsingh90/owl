"use strict";
// Export formats beyond CSV (which lives in model.js): YAML, Markdown tables and .xlsx.
(function (root) {
  const isObject = value => value !== null && typeof value === "object" && !Array.isArray(value);

  // ---- YAML 1.2 (block style) ----

  const RESERVED = /^(?:true|false|null|yes|no|on|off|y|n|~|<<|=)$/i;
  const NUMBER_LIKE = [
    /^[-+]?(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][-+]?\d+)?$/,
    /^[-+]?0[xob][0-9a-f_]+$/i,
    /^[-+]?\.(?:inf|nan)$/i,
    /^[-+]?\d[\d_]*(?::[0-5]?\d)+(?:\.\d*)?$/,
    /^\d{4}-\d\d?-\d\d?(?:[Tt ]|$)/
  ];
  const INDICATOR_START = /^[-?:,[\]{}#&*!|>'"%@`]/;
  // Control characters (other than tab and newline) and non-characters need escapes.
  const UNPRINTABLE = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u2028\u2029\ufeff\ufffe\uffff]/;

  function needsQuotes(text) {
    if (text === "") return true;
    if (RESERVED.test(text) || NUMBER_LIKE.some(pattern => pattern.test(text))) return true;
    if (/^\s|\s$/.test(text)) return true;
    if (INDICATOR_START.test(text)) return true;
    if (/: |:$| #|\t|[\n\r]/.test(text) || UNPRINTABLE.test(text)) return true;
    return false;
  }

  const quote = text => JSON.stringify(text).replace(/[\u007f-\u009f\u2028\u2029\ufeff\ufffe\uffff]/g, char => "\\u" + char.charCodeAt(0).toString(16).padStart(4, "0"));
  const yamlKey = key => needsQuotes(key) ? quote(key) : key;

  function yamlScalar(value) {
    if (value === null || value === undefined) return "null";
    if (typeof value === "boolean") return value ? "true" : "false";
    if (typeof value === "number") {
      if (Number.isNaN(value)) return ".nan";
      if (!Number.isFinite(value)) return value > 0 ? ".inf" : "-.inf";
      return String(value);
    }
    if (typeof value === "bigint") return String(value);
    const text = String(value);
    return needsQuotes(text) ? quote(text) : text;
  }

  // Multi-line text that a literal block keeps exactly: no carriage returns, control characters,
  // leading indentation or trailing spaces (those are written double quoted instead).
  const blockable = text => text.includes("\n") && /^[^\s]/.test(text) && !/\r/.test(text) && !UNPRINTABLE.test(text)
    && !/[ \t]\n|[ \t]$/.test(text.replace(/\n+$/, ""));

  function blockScalar(text, indent) {
    const trailing = /\n+$/.exec(text);
    const chomp = !trailing ? "-" : trailing[0].length === 1 ? "" : "+";
    const body = trailing && chomp !== "+" ? text.slice(0, -1) : text;
    const lines = (chomp === "+" ? text.slice(0, -1) : body).split("\n");
    const pad = " ".repeat(indent);
    return `|${chomp}\n` + lines.map(line => line ? pad + line : "").join("\n");
  }

  const isEmptyContainer = value => Array.isArray(value) ? value.length === 0 : isObject(value) && Object.keys(value).length === 0;

  function yamlValue(value, indent) {
    if (Array.isArray(value)) return value.length ? null : "[]";
    if (isObject(value)) return Object.keys(value).length ? null : "{}";
    if (typeof value === "string" && blockable(value)) return blockScalar(value, indent + 2);
    return yamlScalar(value);
  }

  function yamlLines(value, indent, out) {
    const pad = " ".repeat(indent);
    if (Array.isArray(value)) {
      for (const item of value) {
        const inline = yamlValue(item, indent);
        if (inline !== null) { out.push(`${pad}- ${inline}`); continue; }
        // A nested container starts on the dash line: "- key: value" or "- - item".
        const nested = [];
        yamlLines(item, indent + 2, nested);
        nested[0] = `${pad}- ${nested[0].slice(indent + 2)}`;
        out.push(...nested);
      }
      return out;
    }
    for (const key of Object.keys(value)) {
      const item = value[key];
      const inline = yamlValue(item, indent);
      if (inline !== null) out.push(`${pad}${yamlKey(key)}: ${inline}`);
      else {
        out.push(`${pad}${yamlKey(key)}:`);
        yamlLines(item, indent + 2, out);
      }
    }
    return out;
  }

  function toYaml(value) {
    if (value === undefined) return "null\n";
    if ((Array.isArray(value) || isObject(value)) && !isEmptyContainer(value)) return yamlLines(value, 0, []).join("\n") + "\n";
    return (isEmptyContainer(value) ? yamlValue(value, 0) : yamlScalar(value)) + "\n";
  }

  // ---- Markdown ----

  function markdownCell(value) {
    let text;
    if (value === null || value === undefined) text = "";
    else if (typeof value === "object") text = JSON.stringify(value);
    else text = String(value);
    return text.replace(/\\/g, "\\\\").replace(/\|/g, "\\|").replace(/\r\n|\r|\n/g, "<br>");
  }

  function toMarkdownTable(header, rows) {
    const width = Math.max(header.length, ...rows.map(row => row.length), 1);
    const line = cells => `| ${Array.from({length: width}, (_, i) => markdownCell(cells[i])).join(" | ")} |`;
    const head = header.length ? header : Array.from({length: width}, (_, i) => `Column ${i + 1}`);
    return [line(head), `| ${Array(width).fill("---").join(" | ")} |`, ...rows.map(line)].join("\n") + "\n";
  }

  // ---- XLSX (Office Open XML in a STORE-only ZIP) ----

  const INVALID_XML = /[^\t\n\r\u0020-\uD7FF\uE000-\uFFFD\u{10000}-\u{10FFFF}]/gu;
  const xml = text => String(text).replace(INVALID_XML, "").replace(/[<>&"]/g, char => ({"<": "&lt;", ">": "&gt;", "&": "&amp;", "\"": "&quot;"})[char]);

  function columnName(index) {
    let name = "";
    for (let n = index + 1; n > 0; n = Math.floor((n - 1) / 26)) name = String.fromCharCode(65 + (n - 1) % 26) + name;
    return name;
  }

  const cellString = value => value === null || value === undefined ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);

  function cellXml(value, ref, style) {
    const s = style ? ` s="${style}"` : "";
    if (typeof value === "number" && Number.isFinite(value)) return `<c r="${ref}"${s}><v>${value}</v></c>`;
    const text = cellString(value).slice(0, 32767);
    if (text === "") return style ? `<c r="${ref}"${s}/>` : "";
    const space = /^\s|\s$/.test(text) ? " xml:space=\"preserve\"" : "";
    return `<c r="${ref}"${s} t="inlineStr"><is><t${space}>${xml(text)}</t></is></c>`;
  }

  function sheetXml(header, rows) {
    const all = [header, ...rows];
    const width = Math.max(1, ...all.map(row => row.length));
    const widths = Array.from({length: width}, (_, col) => {
      let longest = 0;
      for (const row of all.slice(0, 2000)) {
        const text = cellString(row[col]);
        const line = Math.max(0, ...text.split("\n").map(part => part.length));
        if (line > longest) longest = line;
      }
      return Math.min(60, Math.max(8, longest + 2));
    });
    const cols = widths.map((w, i) => `<col min="${i + 1}" max="${i + 1}" width="${w}" customWidth="1"/>`).join("");
    const rowXml = all.map((row, r) => {
      const cells = [];
      for (let c = 0; c < row.length; c++) cells.push(cellXml(row[c], columnName(c) + (r + 1), r === 0 ? 1 : 0));
      return `<row r="${r + 1}">${cells.join("")}</row>`;
    }).join("");
    const last = columnName(width - 1) + all.length;
    return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n`
      + `<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">`
      + `<dimension ref="A1:${last}"/>`
      + `<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>`
      + `<sheetFormatPr defaultRowHeight="15"/><cols>${cols}</cols><sheetData>${rowXml}</sheetData></worksheet>`;
  }

  const CONTENT_TYPES = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n`
    + `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">`
    + `<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>`
    + `<Default Extension="xml" ContentType="application/xml"/>`
    + `<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>`
    + `<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`
    + `<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>`
    + `</Types>`;
  const ROOT_RELS = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n`
    + `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">`
    + `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>`
    + `</Relationships>`;
  const WORKBOOK_RELS = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n`
    + `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">`
    + `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>`
    + `<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>`
    + `</Relationships>`;
  const STYLES = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n`
    + `<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">`
    + `<fonts count="2"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font><font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font></fonts>`
    + `<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>`
    + `<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>`
    + `<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>`
    + `<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>`
    + `<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>`
    + `</styleSheet>`;

  function workbookXml(sheetName) {
    return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n`
      + `<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">`
      + `<sheets><sheet name="${xml(sheetName)}" sheetId="1" r:id="rId1"/></sheets></workbook>`;
  }

  let crcTable = null;
  function crc32(bytes) {
    if (!crcTable) {
      crcTable = new Uint32Array(256);
      for (let n = 0; n < 256; n++) {
        let c = n;
        for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
        crcTable[n] = c >>> 0;
      }
    }
    let crc = 0xffffffff;
    for (let i = 0; i < bytes.length; i++) crc = crcTable[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
    return (crc ^ 0xffffffff) >>> 0;
  }

  // A ZIP archive with every entry STOREd (no compression). files: [{name, data: Uint8Array}].
  function zipStore(files) {
    const encoder = new TextEncoder();
    const now = new Date();
    const time = now.getHours() << 11 | now.getMinutes() << 5 | Math.floor(now.getSeconds() / 2);
    const date = Math.max(0, now.getFullYear() - 1980) << 9 | (now.getMonth() + 1) << 5 | now.getDate();
    const entries = files.map(file => ({name: encoder.encode(file.name), data: file.data, crc: crc32(file.data)}));
    const localSize = entries.reduce((sum, entry) => sum + 30 + entry.name.length + entry.data.length, 0);
    const centralSize = entries.reduce((sum, entry) => sum + 46 + entry.name.length, 0);
    const out = new Uint8Array(localSize + centralSize + 22);
    const view = new DataView(out.buffer);
    let at = 0;
    const u16 = value => { view.setUint16(at, value, true); at += 2; };
    const u32 = value => { view.setUint32(at, value, true); at += 4; };
    for (const entry of entries) {
      entry.offset = at;
      u32(0x04034b50); u16(20); u16(0x0800); u16(0); u16(time); u16(date);
      u32(entry.crc); u32(entry.data.length); u32(entry.data.length); u16(entry.name.length); u16(0);
      out.set(entry.name, at); at += entry.name.length;
      out.set(entry.data, at); at += entry.data.length;
    }
    const centralStart = at;
    for (const entry of entries) {
      u32(0x02014b50); u16(20); u16(20); u16(0x0800); u16(0); u16(time); u16(date);
      u32(entry.crc); u32(entry.data.length); u32(entry.data.length); u16(entry.name.length);
      u16(0); u16(0); u16(0); u16(0); u32(0); u32(entry.offset);
      out.set(entry.name, at); at += entry.name.length;
    }
    const centralLength = at - centralStart;
    u32(0x06054b50); u16(0); u16(0); u16(entries.length); u16(entries.length);
    u32(centralLength); u32(centralStart); u16(0);
    return out;
  }

  function toXlsx(header, rows, sheetName = "Sheet1") {
    const name = String(sheetName || "Sheet1").replace(/[[\]:*?/\\]/g, " ").replace(INVALID_XML, "").trim().slice(0, 31) || "Sheet1";
    const encoder = new TextEncoder();
    const files = [
      ["[Content_Types].xml", CONTENT_TYPES],
      ["_rels/.rels", ROOT_RELS],
      ["xl/workbook.xml", workbookXml(name)],
      ["xl/_rels/workbook.xml.rels", WORKBOOK_RELS],
      ["xl/styles.xml", STYLES],
      ["xl/worksheets/sheet1.xml", sheetXml(header || [], rows || [])]
    ];
    return zipStore(files.map(([path, text]) => ({name: path, data: encoder.encode(text)})));
  }

  const api = {toYaml, toMarkdownTable, toXlsx};
  if (typeof module !== "undefined") module.exports = api;
  else root.JVExport = api;
})(typeof self !== "undefined" ? self : this);
