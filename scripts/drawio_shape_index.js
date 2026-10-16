#!/usr/bin/env node
// Build the shape index the draw.io Patterns mode picks from: every AWS and Azure shape
// draw.io ships with, as {group, title, style, w, h}. Run again after upgrading draw.io:
//   node scripts/drawio_shape_index.js
// It loads draw.io's own sidebar files with a stand-in Sidebar that records each shape
// instead of drawing it.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const SIDEBARS = path.join(ROOT, "frontend/drawio/js/diagramly/sidebar");
const OUT = path.join(ROOT, "frontend/drawio/plugins/owl");

const LIBRARIES = [
  { name: "aws", file: "Sidebar-AWS4.js", method: "addAWS4Palette", prefix: "aws4" },
  { name: "azure", file: "Sidebar-Azure2.js", method: "addAzure2Palette", prefix: "azure2" },
];

function capture(library) {
  const shapes = [];
  const seen = new Set();
  let group = "";
  function Sidebar() {}
  const proto = Sidebar.prototype;
  proto.setCurrentSearchEntryLibrary = (_id, palette) => {
    group = (palette || "").slice(library.prefix.length);
  };
  proto.getTagsForStencil = () => [];
  proto.createVertexTemplateEntry = (style, w, h, value, title) => {
    title = (title || value || "").toString().trim();
    // Arrows and illustrations are drawing aids, not resources.
    if (!title || /^(Arrows|Illustrations)$/.test(group)) return () => null;
    const key = group + "|" + title;
    if (!seen.has(key)) {
      seen.add(key);
      shapes.push({ group, title, style, w: Math.round(w), h: Math.round(h) });
    }
    return () => null;
  };
  proto.createEdgeTemplateEntry = () => () => null;
  proto.addPaletteFunctions = () => {};
  proto.addPalette = (_id, _title, _expanded, onInit) => {
    onInit({ appendChild() {} });
  };
  const context = {
    Sidebar,
    mxConstants: new Proxy({}, { get: (_t, key) => (key === "STYLE_SHAPE" ? "shape" : String(key).toLowerCase()) }),
    mxUtils: { bind: (scope, fn) => fn.bind(scope) },
    mxStencil: function () {},
    mxResources: { get: (key) => key },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(SIDEBARS, library.file), "utf8"), context);
  new Sidebar()[library.method]();
  return shapes;
}

fs.mkdirSync(OUT, { recursive: true });
for (const library of LIBRARIES) {
  const shapes = capture(library);
  fs.writeFileSync(path.join(OUT, `shapes-${library.name}.json`), JSON.stringify(shapes) + "\n");
  console.log(`${library.name}: ${shapes.length} shapes`);
}
