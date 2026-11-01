"use strict";
// The architecture graph: nodes from every traced path, edges carrying the flows over them.
// Transit gateway hops are folded into the edges ("via TGW") so the picture reads left to right.
AA.graph = (() => {
  const {worst, flowLabel, title} = AA;
  const NODE_W = 178, NODE_H = 54, COL_GAP = 150, ROW_GAP = 28, PAD = 30;
  const NS = "http://www.w3.org/2000/svg";
  const KIND_COLOR = {
    workload: "var(--k-workload)", external: "var(--k-external)", firewall: "var(--k-security)",
    "load-balancer": "var(--k-network)", vpc: "var(--k-network)", "nat-gateway": "var(--k-network)",
    "internet-gateway": "var(--k-network)", internet: "var(--k-edge)", "direct-connect": "var(--k-network)", peering: "var(--k-network)",
    "vpc-endpoint": "var(--k-network)", "endpoint-service": "var(--k-network)", "eks-cluster": "var(--k-workload)", subnet: "var(--k-workload)",
    tgw: "var(--k-network)", vpn: "var(--k-network)",
  };
  const KIND_TITLE = {
    workload: "", external: "", vpc: "VPC", firewall: "Network Firewall", "nat-gateway": "NAT", "internet-gateway": "IGW",
    internet: "Public internet", "direct-connect": "Hybrid connectivity", "load-balancer": "Load balancer", peering: "Peering",
    "vpc-endpoint": "PrivateLink endpoint", "endpoint-service": "Endpoint service", "eks-cluster": "EKS cluster", subnet: "Subnet",
    tgw: "Transit gateway", vpn: "Site-to-Site VPN",
  };

  function build(results) {
    const nodes = new Map();
    const edges = new Map();
    for (const result of results) {
      let previous = null;
      let via = [];
      for (const hop of result.path) {
        if (hop.kind === "tgw") {
          via.push("TGW");
          continue;
        }
        if (!nodes.has(hop.id)) nodes.set(hop.id, {...hop, results: []});
        const node = nodes.get(hop.id);
        if (!node.results.includes(result)) node.results.push(result);
        if (previous && previous !== hop.id) {
          const key = previous + ">" + hop.id;
          if (!edges.has(key)) edges.set(key, {key, from: previous, to: hop.id, results: [], via: new Set()});
          const edge = edges.get(key);
          edge.results.push(result);
          via.forEach((v) => edge.via.add(v));
        }
        previous = hop.id;
        via = [];
      }
    }
    for (const edge of edges.values()) edge.status = worst(edge.results.map((r) => r.status));
    return {nodes, edges};
  }

  function layout(nodes, edges) {
    const ids = [...nodes.keys()];
    const rank = Object.fromEntries(ids.map((id) => [id, 0]));
    // Longest path from the sources; a cycle stops growing once it passes the node count.
    for (let i = 0; i < ids.length; i++) {
      let changed = false;
      for (const edge of edges.values()) {
        if (rank[edge.to] < rank[edge.from] + 1 && rank[edge.from] + 1 < ids.length) {
          rank[edge.to] = rank[edge.from] + 1;
          changed = true;
        }
      }
      if (!changed) break;
    }
    const columns = [];
    ids.forEach((id) => (columns[rank[id]] ||= []).push(id));
    const columnList = columns.filter(Boolean);
    const row = {};
    columnList.forEach((column) => column.forEach((id, i) => (row[id] = i)));
    const incoming = {};
    for (const edge of edges.values()) (incoming[edge.to] ||= []).push(edge.from);
    for (let sweep = 0; sweep < 2; sweep++) {
      for (const column of columnList) {
        const score = (id) => {
          const from = incoming[id] || [];
          return from.length ? from.reduce((sum, f) => sum + row[f], 0) / from.length : row[id];
        };
        column.sort((a, b) => score(a) - score(b) || a.localeCompare(b));
        column.forEach((id, i) => (row[id] = i));
      }
    }
    const tallest = Math.max(...columnList.map((c) => c.length), 1);
    const position = {};
    columnList.forEach((column, c) => {
      const offset = ((tallest - column.length) * (NODE_H + ROW_GAP)) / 2;
      column.forEach((id, r) => {
        position[id] = {x: PAD + c * (NODE_W + COL_GAP), y: PAD + 18 + offset + r * (NODE_H + ROW_GAP), column: c};
      });
    });
    return {
      position,
      width: PAD * 2 + columnList.length * NODE_W + (columnList.length - 1) * COL_GAP,
      height: PAD * 2 + 18 + tallest * NODE_H + (tallest - 1) * ROW_GAP,
    };
  }

  function el(name, attributes = {}, parent) {
    const node = document.createElementNS(NS, name);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    if (parent) parent.appendChild(node);
    return node;
  }

  function truncate(text, max) {
    return text.length > max ? text.slice(0, max - 1) + "…" : text;
  }

  function edgeLabel(edge) {
    const flows = [...new Set(edge.results.map((r) => flowLabel(r.requirement)))];
    const text = flows.length > 2 ? `${edge.results.length} flows` : flows.join(", ");
    return `${text} · ${edge.status}`;
  }

  // Draws into `container`. Options: onEdge(edge, event), onNode(node, event), scale.
  function render(container, results, options = {}) {
    container.innerHTML = "";
    const {nodes, edges} = build(results);
    if (!nodes.size) {
      container.innerHTML = `<div class="empty">No connectivity requirements in this environment.</div>`;
      return null;
    }
    const {position, width, height} = layout(nodes, edges);
    const scale = options.scale || 1;
    const svg = el("svg", {viewBox: `0 0 ${width} ${height}`, width: width * scale, height: height * scale, role: "img",
      "aria-label": "Architecture graph"});
    const defs = el("defs", {}, svg);
    for (const s of AA.STATUSES) {
      const marker = el("marker", {id: `arrow-${s}`, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse"}, defs);
      el("path", {d: "M0,0 L10,5 L0,10 z", class: `g-arrow-${s}`}, marker);
    }
    const edgeLayer = el("g", {}, svg);
    const nodeLayer = el("g", {}, svg);
    const edgeViews = [];
    const nodeViews = new Map();

    // Edges between the same columns are spread so their labels don't overlap.
    const sorted = [...edges.values()].sort((a, b) => a.key.localeCompare(b.key));
    for (const edge of sorted) {
      const a = position[edge.from], b = position[edge.to];
      let d, lx, ly;
      if (b.column > a.column) {
        const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x - 2, y2 = b.y + NODE_H / 2;
        const bend = Math.max(40, (x2 - x1) / 2);
        d = `M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`;
        lx = (x1 + x2) / 2;
        ly = (y1 + y2) / 2 - 6;
      } else {
        // Backwards: loop under the nodes.
        const x1 = a.x + NODE_W / 2, y1 = a.y + NODE_H, x2 = b.x + NODE_W / 2, y2 = b.y + NODE_H + 2;
        const dip = Math.max(y1, y2) + 46;
        d = `M${x1},${y1} C${x1},${dip} ${x2},${dip} ${x2},${y2}`;
        lx = (x1 + x2) / 2;
        ly = dip - 8;
      }
      const group = el("g", {class: `g-edge ${edge.status}`, tabindex: options.onEdge ? 0 : -1, role: "button",
        "aria-label": `${nodes.get(edge.from).label} to ${nodes.get(edge.to).label}: ${edgeLabel(edge)}`}, edgeLayer);
      el("path", {d, class: "hit"}, group);
      el("path", {d, class: "line", "marker-end": `url(#arrow-${edge.status})`}, group);
      const label = el("text", {x: lx, y: ly, "text-anchor": "middle"}, group);
      label.textContent = edgeLabel(edge);
      if (edge.via.size) {
        const via = el("text", {x: lx, y: ly + 13, "text-anchor": "middle", style: "font-weight:400;fill:var(--muted)"}, group);
        via.textContent = "via " + [...edge.via].join(", ");
      }
      if (options.onEdge) {
        group.addEventListener("click", (event) => {
          event.stopPropagation();
          options.onEdge(edge, event);
        });
        group.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            options.onEdge(edge, event);
          }
        });
      }
      edgeViews.push({edge, group});
    }

    for (const node of [...nodes.values()].sort((a, b) => a.id.localeCompare(b.id))) {
      const p = position[node.id];
      const group = el("g", {class: "g-node", transform: `translate(${p.x},${p.y})`, tabindex: options.onNode ? 0 : -1, role: "button",
        "aria-label": node.label}, nodeLayer);
      el("rect", {class: "box", width: NODE_W, height: NODE_H, rx: 10}, group);
      el("rect", {class: "stripe", width: 5, height: NODE_H - 16, x: 8, y: 8, rx: 2.5, fill: KIND_COLOR[node.kind] || "var(--k-edge)"}, group);
      const name = el("text", {x: 22, y: 23}, group);
      name.textContent = truncate(node.label, 22);
      const sub = el("text", {x: 22, y: 40, class: "sub"}, group);
      sub.textContent = truncate(KIND_TITLE[node.kind] || title(node.role) || "", 26);
      const tip = el("title", {}, group);
      tip.textContent = `${node.label}\n${node.id}`;
      if (options.onNode) {
        group.addEventListener("click", (event) => {
          event.stopPropagation();
          options.onNode(node, event);
        });
        group.addEventListener("keydown", (event) => {
          if (event.key === "Enter") options.onNode(node, event);
        });
      }
      nodeViews.set(node.id, group);
    }
    container.appendChild(svg);

    return {
      nodes, edges, svg,
      // Fades everything not on the given flows' paths.
      focus(resultsToShow) {
        const keep = resultsToShow ? new Set(resultsToShow) : null;
        for (const {edge, group} of edgeViews) group.classList.toggle("dim", !!keep && !edge.results.some((r) => keep.has(r)));
        for (const [id, group] of nodeViews) group.classList.toggle("dim", !!keep && !nodes.get(id).results.some((r) => keep.has(r)));
      },
      select(id) {
        for (const [nodeId, group] of nodeViews) group.classList.toggle("selected", nodeId === id);
      },
    };
  }

  return {render, build};
})();
