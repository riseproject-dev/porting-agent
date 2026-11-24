// SPDX-FileCopyrightText: 2026 The RISE Project
// SPDX-License-Identifier: MIT
//
// The interactive graph of the lifecycle table (docs/state-machine/graph.md). It
// reads graph.json, which scripts/generate_state_machine_doc.py writes from the
// table, and draws it with Cytoscape.js and the dagre layout (loaded by the page
// from a CDN at exact versions, with integrity hashes). No build step.
(function () {
  "use strict";

  var root = document.querySelector(".sg");
  if (!root) { return; }

  var TRIGGERS = ["command", "observation", "result", "operation", "tick"];
  var ORIGINS = ["table", "rule", "flag"];
  var ORIGIN_WORD = { table: "table", rule: "rule", flag: "flag" };
  var KIND_VARS = ["work", "auto_wait", "human_wait", "gate", "locked", "generation_end",
    "terminal", "released"];
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var narrow = window.matchMedia("(max-width: 49.99rem)");

  var cy = null;
  var data = null;
  var nodesById = {};
  var rowsByState = {};
  var colors = {};
  var filters = defaultFilters();
  var selected = null;
  var hovered = null;
  var query = "";

  // ------------------------------------------------------------------ helpers

  function h(tag, attrs, kids) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (attrs[k] === false || attrs[k] == null) { return; }
      if (k === "text") { el.textContent = attrs[k]; } else { el.setAttribute(k, attrs[k]); }
    });
    (kids || []).forEach(function (kid) {
      el.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
    });
    return el;
  }

  function anchorOf(id) { return "state-" + id.replace(/\./g, "-"); }

  function defaultFilters() {
    return { triggers: TRIGGERS.slice(), loops: true, rules: true, flags: true, phases: null };
  }

  function fail(message) {
    var box = root.querySelector("noscript");
    var p = h("p", { class: "sg__error", role: "alert", text: message });
    if (box) { box.parentNode.insertBefore(p, box); } else { root.insertBefore(p, root.firstChild); }
    var table = root.getAttribute("data-table");
    p.appendChild(document.createTextNode(" "));
    p.appendChild(h("a", { href: table, text: "Read the lifecycle table instead." }));
  }

  // ------------------------------------------------------------------- colors

  function parseColor(text) {
    var m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(text.trim());
    if (m) {
      var v = m[1].length === 3 ? m[1].replace(/./g, "$&$&") : m[1];
      return [parseInt(v.slice(0, 2), 16), parseInt(v.slice(2, 4), 16), parseInt(v.slice(4, 6), 16)];
    }
    m = /rgba?\(([^)]+)\)/.exec(text);
    if (m) { return m[1].split(/[ ,\/]+/).slice(0, 3).map(Number); }
    return [128, 128, 128];
  }

  function mix(fg, bg, alpha) {
    var a = parseColor(fg), b = parseColor(bg);
    return "rgb(" + a.map(function (c, i) { return Math.round(c * alpha + b[i] * (1 - alpha)); })
      .join(",") + ")";
  }

  function readColors() {
    var cs = getComputedStyle(root);
    function v(name, fallback) { return cs.getPropertyValue(name).trim() || fallback; }
    colors = {
      bg: v("--sg-bg", "#fff"), text: v("--sg-text", "#222"), edge: v("--sg-edge", "#8a8a8a"),
      inbound: v("--sg-in", "#1d6fd0"), outbound: v("--sg-out", "#c05a00"),
      select: v("--sg-select", "#7253ed"), phase: v("--sg-phase", "#eee"),
      phaseText: v("--sg-phase-text", "#555"), font: cs.fontFamily || "sans-serif", kind: {}
    };
    KIND_VARS.forEach(function (k) { colors.kind[k] = v("--sg-kind-" + k, "#888"); });
  }

  function kindColor(kind, phase) {
    return colors.kind[kind === "terminal" && phase === "released" ? "released" : kind];
  }

  // ------------------------------------------------------------- the elements

  // Rows of the table between the same two states and from the same origin are
  // drawn as one edge that lists their verbs.
  function bundles() {
    var map = {};
    data.edges.forEach(function (e) {
      var key = e.source + ">" + e.target + ">" + e.origin;
      (map[key] = map[key] || { id: key, source: e.source, target: e.target, origin: e.origin,
        rows: [] }).rows.push(e);
    });
    return Object.keys(map).map(function (k) { return map[k]; });
  }

  // The size of a state: its label (two lines) measured on a canvas, plus padding.
  function size(lines) {
    var ctx = document.createElement("canvas").getContext("2d");
    ctx.font = "12px " + colors.font;
    var width = Math.max.apply(null, lines.map(function (l) { return ctx.measureText(l).width; }));
    return { w: Math.ceil(width) + 26, h: lines.length * 15 + 16 };
  }

  function elements() {
    var els = data.phases.map(function (p) {
      return { group: "nodes", data: { id: "phase:" + p, label: p.replace(/_/g, " ") },
        classes: "phase", selectable: false, grabbable: false };
    });
    data.nodes.forEach(function (n) {
      var abbr = (data.kinds[n.kind] || {}).abbreviation || n.kind;
      var box = size([n.activity, abbr]);
      els.push({ group: "nodes", data: { id: n.id, parent: "phase:" + n.phase, kind: n.kind,
        phase: n.phase, label: n.activity + "\n" + abbr, w: box.w, h: box.h },
        classes: "state" });
    });
    bundles().forEach(function (b) {
      els.push({ group: "edges", data: { id: b.id, source: b.source, target: b.target,
        origin: b.origin, rows: b.rows, hl: "" },
        classes: "origin-" + b.origin + (b.source === b.target ? " loop" : "") });
    });
    return els;
  }

  // ------------------------------------------------------------------- styles

  function buildStyle() {
    var kindStyles = data.nodes.map(function (n) { return n; }).reduce(function (acc, n) {
      var key = n.kind + "/" + (n.kind === "terminal" ? n.phase : "");
      if (!acc.seen[key]) {
        acc.seen[key] = true;
        var color = kindColor(n.kind, n.phase);
        var sel = n.kind === "terminal" ? 'node[kind = "terminal"][phase = "' + n.phase + '"]'
          : 'node[kind = "' + n.kind + '"]';
        acc.list.push({ selector: sel, style: {
          "background-color": mix(color, colors.bg, 0.2), "border-color": color,
          "border-style": n.kind === "locked" ? "dashed" : (n.kind === "terminal" ? "double" : "solid"),
          "border-width": n.kind === "terminal" ? 4 : 2 } });
      }
      return acc;
    }, { seen: {}, list: [] }).list;
    return [
      { selector: "node.state", style: { shape: "round-rectangle", label: "data(label)",
        "text-wrap": "wrap", "text-valign": "center", "text-halign": "center",
        color: colors.text, "font-family": colors.font, "font-size": 12, "line-height": 1.25,
        width: "data(w)", height: "data(h)",
        "min-zoomed-font-size": 6, "z-index": 10 } },
      { selector: "node.phase", style: { shape: "round-rectangle", label: "data(label)",
        "text-valign": "top", "text-halign": "center", "font-size": 12, "font-weight": 700,
        "font-family": colors.font, color: colors.phaseText, "background-color": colors.phase,
        "background-opacity": 0.55, "border-width": 1, "border-color": colors.edge,
        "border-opacity": 0.4, padding: "14px", "text-margin-y": -2, events: "no",
        "z-index": 1 } }
    ].concat(kindStyles, [
      { selector: "edge", style: { "curve-style": "bezier", width: 1.2, "line-color": colors.edge,
        "target-arrow-color": colors.edge, "target-arrow-shape": "triangle", "arrow-scale": 0.8,
        opacity: 0.5, label: "data(hl)", "font-size": 11, "font-family": colors.font,
        color: colors.text, "text-background-color": colors.bg, "text-background-opacity": 0.92,
        "text-background-padding": 2, "text-background-shape": "roundrectangle",
        "text-rotation": "autorotate", "control-point-step-size": 60, "z-index": 5,
        "min-zoomed-font-size": 6 } },
      { selector: "edge.origin-rule", style: { "line-style": "dashed", "line-dash-pattern": [7, 4],
        width: 1 } },
      { selector: "edge.origin-flag", style: { "line-style": "dotted", width: 1.6 } },
      { selector: "edge.loop", style: { "loop-direction": "-45deg", "control-point-step-size": 40 } },
      { selector: "edge.loop.origin-rule", style: { "loop-direction": "0deg" } },
      { selector: "edge.loop.origin-flag", style: { "loop-direction": "45deg" } },
      { selector: ".hidden", style: { display: "none" } },
      { selector: "node.dim", style: { opacity: 0.22 } },
      { selector: "edge.dim", style: { opacity: 0.05 } },
      { selector: "node.dim.phase", style: { opacity: 0.5 } },
      { selector: "node.match", style: { "border-width": 4, "border-color": colors.select } },
      { selector: "node.focus", style: { "border-width": 5, "border-color": colors.select,
        "z-index": 20, "font-weight": 700 } },
      { selector: "node.neighbor", style: { "font-weight": 700, "z-index": 15 } },
      { selector: "edge.in", style: { "line-color": colors.inbound,
        "target-arrow-color": colors.inbound, width: 3, opacity: 1, "z-index": 30,
        "target-arrow-shape": "triangle", "arrow-scale": 1.3 } },
      { selector: "edge.out", style: { "line-color": colors.outbound,
        "target-arrow-color": colors.outbound, width: 3, opacity: 1, "z-index": 30,
        "target-arrow-shape": "vee", "arrow-scale": 1.3 } },
      { selector: "edge.in.out", style: { "line-color": colors.select,
        "target-arrow-color": colors.select, "target-arrow-shape": "triangle-tee" } },
      { selector: "edge.origin-rule.in, edge.origin-rule.out", style: { width: 2.4 } }
    ]);
  }

  // ----------------------------------------------------------------- filtering

  function rowVisible(row) {
    if (filters.triggers.indexOf(row.trigger) < 0) { return false; }
    if (row.self && !filters.loops) { return false; }
    if (row.origin === "rule" && !filters.rules) { return false; }
    if (row.origin === "flag" && !filters.flags) { return false; }
    return true;
  }

  function phaseVisible(phase) {
    return !filters.phases || filters.phases.indexOf(phase) >= 0;
  }

  function applyFilters() {
    cy.batch(function () {
      cy.nodes(".state").forEach(function (n) {
        n.toggleClass("hidden", !phaseVisible(n.data("phase")));
      });
      cy.nodes(".phase").forEach(function (n) {
        n.toggleClass("hidden", !phaseVisible(n.id().slice(6)));
      });
      cy.edges().forEach(function (e) {
        var show = e.source().visible() && e.target().visible() &&
          !e.source().hasClass("hidden") && !e.target().hasClass("hidden") &&
          e.data("rows").some(rowVisible);
        e.toggleClass("hidden", !show);
      });
    });
    updateHighlight();
  }

  // ---------------------------------------------------------------- highlight

  function verbs(edge, filterRows) {
    var rows = edge.data("rows").filter(function (r) { return !filterRows || rowVisible(r); });
    var names = rows.map(function (r) { return r.verb; });
    return names.length > 2 ? names[0] + " +" + (names.length - 1) : names.join(", ");
  }

  function clearHighlight() {
    cy.batch(function () {
      cy.elements().removeClass("dim in out focus neighbor match");
      cy.edges().forEach(function (e) { if (e.data("hl")) { e.data("hl", ""); } });
    });
  }

  function highlightNode(node) {
    cy.batch(function () {
      cy.elements().removeClass("in out focus neighbor match").addClass("dim");
      node.removeClass("dim").addClass("focus");
      node.parent().removeClass("dim");
      node.connectedEdges().not(".hidden").forEach(function (e) {
        var out = e.source().id() === node.id();
        var inn = e.target().id() === node.id();
        e.removeClass("dim").toggleClass("out", out).toggleClass("in", inn);
        var other = out ? e.target() : e.source();
        if (other.id() !== node.id()) {
          other.removeClass("dim").addClass("neighbor");
          other.parent().removeClass("dim");
        }
        var label = verbs(e, true);
        e.data("hl", out && inn ? "↻ " + label : (out ? label + " →" : "← " + label));
      });
      cy.edges().forEach(function (e) {
        if (e.hasClass("dim") && e.data("hl")) { e.data("hl", ""); }
      });
    });
  }

  function highlightQuery() {
    var q = query.toLowerCase();
    cy.batch(function () {
      cy.elements().removeClass("in out focus neighbor match dim");
      cy.edges().forEach(function (e) { if (e.data("hl")) { e.data("hl", ""); } });
      if (!q) { return; }
      var matched = matchingStates(q);
      cy.elements().addClass("dim");
      matched.forEach(function (id) {
        var n = cy.getElementById(id);
        n.removeClass("dim").addClass("match");
        n.parent().removeClass("dim");
      });
    });
  }

  function matchingStates(q) {
    var found = {};
    data.nodes.forEach(function (n) { if (n.id.toLowerCase().indexOf(q) >= 0) { found[n.id] = 1; } });
    data.edges.forEach(function (e) {
      if (e.verb.toLowerCase().indexOf(q) >= 0) { found[e.source] = 1; found[e.target] = 1; }
    });
    return Object.keys(found);
  }

  // The hovered or focused state wins over the selected one; with none of them
  // the search shows its matches.
  function updateHighlight() {
    var id = hovered || selected;
    if (id && !cy.getElementById(id).hasClass("hidden")) {
      highlightNode(cy.getElementById(id));
    } else if (query) {
      highlightQuery();
    } else {
      clearHighlight();
    }
  }

  // --------------------------------------------------------------- the panel

  var panel = root.querySelector(".sg__panel");
  var lastFocus = null;

  function badge(node) {
    var spec = data.kinds[node.kind] || {};
    var cls = "state state--" + node.kind + (node.kind === "terminal" && node.phase === "released"
      ? " state--released" : "");
    return h("span", { class: cls }, [
      h("span", { class: "state__kind", "aria-hidden": "true", text: spec.abbreviation || node.kind }),
      node.id, h("span", { class: "sr-only", text: " (" + (spec.label || node.kind) + ")" })]);
  }

  function conditions(row) {
    if (!row.preconditions.length) { return h("span", { text: "none" }); }
    return h("ul", { class: "sg__conds" }, row.preconditions.map(function (c) {
      if (typeof c === "string") { return h("li", { text: c }); }
      var how = { kept: "kept from the source state", implied: "implied by the guard",
        established: "established by the row", masked: "assured by the pause mask" }[c.how];
      return h("li", { text: data.invariants[c.invariant], title: how || "" });
    }));
  }

  var COLUMNS = ["Other state", "Verb", "Trigger", "Preconditions", "Context"];
  var SORTS = ["Table order", "Other state", "Verb", "Trigger"];

  function transitionTable(title, rows, side) {
    var table = h("table", { class: "sg__table" });
    var head = h("tr");
    COLUMNS.forEach(function (name) { head.appendChild(h("th", { scope: "col", text: name })); });
    table.appendChild(h("caption", { text: title + " (" + rows.length + ")" }));
    table.appendChild(h("thead", {}, [head]));
    var body = h("tbody");
    rows.forEach(function (row, index) {
      var other = side === "in" ? row.source : row.target;
      var tr = h("tr", { "data-edge": row.id, "data-order": index });
      var otherCell = h("td", { "data-label": COLUMNS[0], class: "sg__c-other" });
      if (row.self) { otherCell.textContent = "same state"; } else {
        otherCell.appendChild(h("button", { type: "button", class: "sg__go", "data-go": other }, [
          badge(nodesById[other])]));
      }
      var verbCell = h("td", { "data-label": COLUMNS[1], class: "sg__c-verb" }, [
        h("a", { href: root.getAttribute("data-table") + "#" + row.anchor, text: row.verb })]);
      if (row.origin !== "table") {
        verbCell.appendChild(h("span", { class: "sg__origin",
          text: " (" + (row.origin === "rule" ? "rule" : "flag " + row.flag) + ")" }));
      }
      var trigger = h("td", { "data-label": COLUMNS[2], class: "sg__c-trigger", text: row.trigger,
        title: data.triggers[row.trigger] || "" });
      if (row.command) { trigger.appendChild(h("code", { text: row.command })); }
      tr.appendChild(otherCell);
      tr.appendChild(verbCell);
      tr.appendChild(trigger);
      tr.appendChild(h("td", { "data-label": COLUMNS[3], class: "sg__c-pre" }, [conditions(row)]));
      tr.appendChild(h("td", { "data-label": COLUMNS[4], class: "sg__c-context", text: row.context }));
      tr._key = [other, other, row.verb, row.trigger];
      body.appendChild(tr);
    });
    table.appendChild(body);
    return table;
  }

  function sorter(table) {
    var select = h("select", { class: "sg__sorter", "aria-label": "Sort the rows" },
      SORTS.map(function (name, i) { return h("option", { value: String(i), text: "Sort: " + name }); }));
    select.addEventListener("change", function () {
      var column = Number(select.value);
      var body = table.tBodies[0];
      Array.prototype.slice.call(body.rows).sort(function (a, b) {
        var ka = column ? a._key[column] : "", kb = column ? b._key[column] : "";
        return ka.localeCompare(kb) ||
          Number(a.getAttribute("data-order")) - Number(b.getAttribute("data-order"));
      }).forEach(function (tr) { body.appendChild(tr); });
    });
    return select;
  }

  function section(title, rows, side, open) {
    if (!rows.length) { return h("p", { class: "sg__none", text: title + ": none." }); }
    var table = transitionTable(title, rows, side);
    var details = h("details", { class: "sg__group" }, [
      h("summary", { text: title + " (" + rows.length + ")" }), sorter(table), table]);
    if (open) { details.setAttribute("open", ""); }
    return details;
  }

  function renderPanel(id) {
    var node = nodesById[id];
    var rows = rowsByState[id];
    var close = h("button", { type: "button", class: "sg__close", "aria-label": "Close the panel",
      text: "×" });
    close.addEventListener("click", function () { select(null, true); });
    var handle = h("button", { type: "button", class: "sg__handle",
      "aria-label": "Resize the panel (arrow keys up and down)" });
    wireHandle(handle);
    var title = h("h2", { id: "sg-panel-title", class: "sg__title" }, [badge(node)]);
    var requires = node.requires.length
      ? h("ul", { class: "sg__conds" }, node.requires.map(function (n) {
        return h("li", { text: data.invariants[n] }); }))
      : h("span", { text: "nothing" });
    panel.textContent = "";
    [handle, h("div", { class: "sg__head" }, [title, close]),
      h("p", { class: "sg__summary", text: node.summary }),
      h("p", { class: "sg__links" }, [
        h("a", { href: root.getAttribute("data-table") + "#" + node.anchor,
          text: "Open in the lifecycle table" })]),
      h("div", { class: "sg__requires" }, [h("strong", { text: "Every record here satisfies: " }),
        requires]),
      section("Inbound", rows.filter(function (r) { return r.target === id && !r.self; }), "in", true),
      section("Outbound", rows.filter(function (r) { return r.source === id && !r.self; }), "out", true),
      section("Stays in state", rows.filter(function (r) { return r.self; }), "self", false)
    ].forEach(function (el) { panel.appendChild(el); });
    panel.hidden = false;
    panel.scrollTop = 0;
  }

  function wireHandle(handle) {
    var startY = 0, startH = 0;
    function height(px) {
      var max = window.innerHeight * 0.92;
      panel.style.setProperty("--sg-sheet-h", Math.max(120, Math.min(max, px)) + "px");
    }
    handle.addEventListener("pointerdown", function (ev) {
      startY = ev.clientY; startH = panel.getBoundingClientRect().height;
      handle.setPointerCapture(ev.pointerId);
      handle.onpointermove = function (m) { height(startH + (startY - m.clientY)); };
      handle.onpointerup = function () { handle.onpointermove = null; handle.onpointerup = null; };
    });
    handle.addEventListener("keydown", function (ev) {
      var step = ev.key === "ArrowUp" ? 40 : ev.key === "ArrowDown" ? -40 : 0;
      if (step) { ev.preventDefault(); height(panel.getBoundingClientRect().height + step); }
    });
  }

  panel.addEventListener("click", function (ev) {
    var go = ev.target.closest("[data-go]");
    if (go) { select(go.getAttribute("data-go"), false, true); }
  });

  panel.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") { select(null, true); }
  });

  // ---------------------------------------------------------------- selection

  function select(id, restoreFocus, center) {
    if (id && !nodesById[id]) { id = null; }
    selected = id;
    if (id) {
      if (document.activeElement && !panel.contains(document.activeElement)) {
        lastFocus = document.activeElement;
      }
      renderPanel(id);
      root.classList.add("sg--open");
      var node = cy.getElementById(id);
      if (node.hasClass("hidden")) { node.removeClass("hidden"); }
      // The panel changes the width of the canvas: center once it has.
      requestAnimationFrame(function () { cy.resize(); if (center !== false) { reveal(node); } });
    } else {
      panel.hidden = true;
      panel.textContent = "";
      root.classList.remove("sg--open");
      requestAnimationFrame(function () { cy.resize(); });
      if (restoreFocus && lastFocus && document.contains(lastFocus)) { lastFocus.focus(); }
    }
    markSelected();
    updateHighlight();
    writeHash();
  }

  // Bring a state into view at a zoom where its label reads, in the part of the canvas
  // that the bottom sheet (on a narrow screen) does not cover.
  function reveal(node) {
    var zoom = Math.max(cy.zoom(), 0.8);
    var visible = cy.height();
    if (narrow.matches && !panel.hidden) {
      var top = panel.getBoundingClientRect().top - root.querySelector(".sg__canvas")
        .getBoundingClientRect().top;
      visible = Math.max(80, Math.min(visible, top));
    }
    var p = node.position();
    var pan = { x: cy.width() / 2 - zoom * p.x, y: visible / 2 - zoom * p.y };
    if (reduced) { cy.viewport({ zoom: zoom, pan: pan }); }
    else { cy.animate({ zoom: zoom, pan: pan, duration: 220 }); }
  }

  function markSelected() {
    Array.prototype.forEach.call(root.querySelectorAll(".sg__states button"), function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-id") === selected));
    });
  }

  // --------------------------------------------------------------------- hash

  function writeHash() {
    var parts = [];
    if (filters.triggers.length !== TRIGGERS.length) { parts.push("t=" + filters.triggers.join(",")); }
    if (!filters.loops) { parts.push("loops=0"); }
    if (!filters.rules) { parts.push("rules=0"); }
    if (!filters.flags) { parts.push("flags=0"); }
    if (filters.phases) { parts.push("p=" + filters.phases.join(",")); }
    var hash = (selected ? anchorOf(selected) : "") + (parts.length ? "?" + parts.join("&") : "");
    try {
      history.replaceState(null, "", location.pathname + location.search + (hash ? "#" + hash : ""));
    } catch (e) { /* a sandboxed frame may refuse; the page works without the hash */ }
  }

  function readHash() {
    var raw = decodeURIComponent(location.hash.slice(1));
    var split = raw.split("?");
    var f = defaultFilters();
    (split[1] || "").split("&").forEach(function (pair) {
      var kv = pair.split("=");
      var list = (kv[1] || "").split(",").filter(Boolean);
      if (kv[0] === "t") { f.triggers = list.filter(function (t) { return TRIGGERS.indexOf(t) >= 0; }); }
      if (kv[0] === "loops") { f.loops = kv[1] !== "0"; }
      if (kv[0] === "rules") { f.rules = kv[1] !== "0"; }
      if (kv[0] === "flags") { f.flags = kv[1] !== "0"; }
      if (kv[0] === "p") {
        f.phases = list.filter(function (p) { return data.phases.indexOf(p) >= 0; });
      }
    });
    var id = null;
    data.nodes.forEach(function (n) { if (anchorOf(n.id) === split[0]) { id = n.id; } });
    return { filters: f, id: id };
  }

  // ------------------------------------------------------------------ toolbar

  var toolbar = root.querySelector(".sg__toolbar");

  function check(label, checked, onChange, title) {
    var input = h("input", { type: "checkbox" });
    input.checked = checked;
    input.addEventListener("change", function () { onChange(input.checked); });
    var wrap = h("label", { class: "sg__check", title: title || false }, [input, " " + label]);
    wrap.input = input;
    return wrap;
  }

  var controls = {};

  function buildToolbar() {
    toolbar.textContent = "";
    var trig = h("fieldset", { class: "sg__fieldset" }, [h("legend", { text: "Trigger" })]);
    controls.triggers = {};
    TRIGGERS.forEach(function (t) {
      var c = check(t, filters.triggers.indexOf(t) >= 0, function (on) {
        filters.triggers = TRIGGERS.filter(function (x) {
          return x === t ? on : filters.triggers.indexOf(x) >= 0;
        });
        changed();
      }, data.triggers[t]);
      controls.triggers[t] = c;
      trig.appendChild(c);
    });
    var show = h("fieldset", { class: "sg__fieldset" }, [h("legend", { text: "Show" })]);
    [["loops", "self-loops"], ["rules", "rules"], ["flags", "flags"]].forEach(function (p) {
      var c = check(p[1], filters[p[0]], function (on) { filters[p[0]] = on; changed(); },
        { loops: "Transitions that stay in their state", rules: "Rows expanded from a rule",
          flags: "Rows of the paused flag" }[p[0]]);
      controls[p[0]] = c;
      show.appendChild(c);
    });
    var phases = h("details", { class: "sg__phases" }, [h("summary", { text: "Phases" })]);
    var plist = h("div", { class: "sg__phase-list" });
    controls.phases = {};
    data.phases.forEach(function (p) {
      var c = check(p, !filters.phases || filters.phases.indexOf(p) >= 0, function (on) {
        var current = filters.phases || data.phases.slice();
        current = data.phases.filter(function (x) { return x === p ? on : current.indexOf(x) >= 0; });
        filters.phases = current.length === data.phases.length ? null : current;
        changed();
      });
      controls.phases[p] = c;
      plist.appendChild(c);
    });
    phases.appendChild(plist);
    var search = h("input", { type: "search", class: "sg__search", list: "sg-datalist",
      placeholder: "Find a state or verb", "aria-label": "Find a state or a verb",
      autocomplete: "off" });
    var datalist = h("datalist", { id: "sg-datalist" }, data.nodes.map(function (n) {
      return h("option", { value: n.id }); }));
    search.addEventListener("input", function () { query = search.value.trim(); updateHighlight(); });
    search.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && query) {
        var exact = nodesById[query] ? query : matchingStates(query.toLowerCase())[0];
        if (exact) { select(exact); }
      }
    });
    var fit = h("button", { type: "button", class: "sg__button", text: "Fit" });
    fit.addEventListener("click", function () { fitView(); });
    var zoomIn = h("button", { type: "button", class: "sg__button", "aria-label": "Zoom in",
      text: "+" });
    zoomIn.addEventListener("click", function () { zoomBy(1.25); });
    var zoomOut = h("button", { type: "button", class: "sg__button", "aria-label": "Zoom out",
      text: "−" });
    zoomOut.addEventListener("click", function () { zoomBy(0.8); });
    var reset = h("button", { type: "button", class: "sg__button", text: "Reset" });
    reset.addEventListener("click", function () {
      filters = defaultFilters(); query = ""; search.value = "";
      buildToolbar(); toolbar.querySelector(".sg__search").value = "";
      select(null); applyFilters(); fitView(); writeHash();
    });
    [trig, show, phases, h("div", { class: "sg__tools" }, [search, datalist, fit, zoomIn, zoomOut,
      reset]), h("p", { class: "sg__hint",
      text: "Scroll the page as usual; hold Ctrl or Cmd to zoom with the wheel, or pinch." })
    ].forEach(function (el) { toolbar.appendChild(el); });
    toolbar.hidden = false;
  }

  function changed() {
    applyFilters();
    writeHash();
  }

  function zoomBy(factor) {
    cy.zoom({ level: cy.zoom() * factor, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  }

  function fitView() {
    cy.fit(cy.elements(":visible"), 24);
  }

  // The graph is deep, so the whole of it does not read: the first view is the whole
  // graph when it reads, else the start of the machine at a zoom where labels read.
  function firstView() {
    fitView();
    if (cy.zoom() < 0.55) {
      cy.zoom(0.7);
      cy.center(cy.getElementById(data.initial.state));
    }
  }

  // --------------------------------------------------------------- state list

  function buildList() {
    var holder = root.querySelector(".sg__states");
    holder.textContent = "";
    var buttons = [];
    data.phases.forEach(function (phase) {
      var group = h("div", { class: "sg__phase", role: "group", "aria-label": phase });
      group.appendChild(h("span", { class: "sg__phase-name", "aria-hidden": "true", text: phase }));
      data.nodes.filter(function (n) { return n.phase === phase; }).forEach(function (n) {
        var b = h("button", { type: "button", class: "sg__state", "data-id": n.id,
          "aria-pressed": "false", tabindex: buttons.length ? "-1" : "0" }, [badge(n)]);
        b.addEventListener("focus", function () { hover(n.id); });
        b.addEventListener("blur", function () { hover(null); });
        b.addEventListener("click", function () { select(n.id); });
        buttons.push(b);
        group.appendChild(b);
      });
      holder.appendChild(group);
    });
    holder.addEventListener("keydown", function (ev) {
      var i = buttons.indexOf(document.activeElement);
      if (i < 0) { return; }
      var next = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[ev.key];
      if (ev.key === "Home") { next = -i; }
      if (ev.key === "End") { next = buttons.length - 1 - i; }
      if (next == null) { return; }
      ev.preventDefault();
      var target = buttons[Math.max(0, Math.min(buttons.length - 1, i + next))];
      buttons.forEach(function (b) { b.setAttribute("tabindex", b === target ? "0" : "-1"); });
      target.focus();
    });
    holder.hidden = false;
  }

  function hover(id) {
    hovered = id;
    updateHighlight();
  }

  // -------------------------------------------------------------------- setup

  function themeChanged() {
    if (!cy) { return; }
    readColors();
    cy.style(buildStyle()).update();
  }

  function watchTheme() {
    var timer = null;
    function later() { clearTimeout(timer); timer = setTimeout(themeChanged, 60); }
    function hookLinks() {
      Array.prototype.forEach.call(document.querySelectorAll('link[rel="stylesheet"]'),
        function (link) {
          if (!link._sg) { link._sg = true; link.addEventListener("load", later); }
        });
    }
    hookLinks();
    new MutationObserver(function () { hookLinks(); later(); })
      .observe(document.head, { childList: true, subtree: true, attributes: true });
    var dark = window.matchMedia("(prefers-color-scheme: dark)");
    if (dark.addEventListener) { dark.addEventListener("change", later); }
  }

  function init(graph) {
    data = graph;
    data.nodes.forEach(function (n) { nodesById[n.id] = n; rowsByState[n.id] = []; });
    data.edges.forEach(function (e) {
      rowsByState[e.source].push(e);
      if (e.target !== e.source) { rowsByState[e.target].push(e); }
    });
    var start = readHash();
    filters = start.filters;
    readColors();
    var canvas = root.querySelector(".sg__canvas");
    cy = cytoscape({ container: canvas, elements: elements(), style: buildStyle(),
      autoungrabify: true, boxSelectionEnabled: false, minZoom: 0.15, maxZoom: 2.5,
      textureOnViewport: false });
    // The structure of the layout is the table rows that change state, so that the rule and
    // the flag rows (which every state has) do not pull the picture apart.
    var layoutElements = cy.nodes().union(cy.edges(".origin-table").not(".loop"));
    layoutElements.layout({ name: "dagre", rankDir: "TB", nodeSep: 50, rankSep: 70, edgeSep: 35,
      ranker: "network-simplex", animate: false, fit: false }).run();
    cy.on("mouseover", "node.state", function (ev) { hover(ev.target.id()); });
    cy.on("mouseout", "node.state", function () { hover(null); });
    cy.on("tap", "node.state", function (ev) { select(ev.target.id(), false, false); });
    cy.on("tap", "edge", function (ev) {
      var e = ev.target;
      select(e.source().id(), false, false);
      var row = panel.querySelector('[data-edge="' + e.data("rows")[0].id + '"]');
      if (row) { flash(row); }
    });
    cy.on("tap", function (ev) { if (ev.target === cy) { select(null); } });
    // Wheel zooming only with a modifier, so that the page scrolls over the graph.
    root.querySelector(".sg__body").addEventListener("wheel", function (ev) {
      if (!(ev.ctrlKey || ev.metaKey)) { ev.stopPropagation(); }
    }, { capture: true, passive: true });
    if (window.ResizeObserver) {
      new ResizeObserver(function () { cy.resize(); }).observe(canvas);
    }
    buildToolbar();
    buildList();
    applyFilters();
    firstView();
    watchTheme();
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && selected && !ev.defaultPrevented) { select(null, true); }
    });
    window.addEventListener("hashchange", function () {
      var next = readHash();
      filters = next.filters;
      buildToolbar();
      applyFilters();
      if (next.id !== selected) { select(next.id); }
    });
    if (start.id) { select(start.id); }
    root.classList.add("sg--ready");
    root.__cy = cy;
  }

  function flash(row) {
    var details = row.closest("details");
    if (details) { details.open = true; }
    row.scrollIntoView({ block: "center", behavior: reduced ? "auto" : "smooth" });
    row.classList.add("sg__flash");
    setTimeout(function () { row.classList.remove("sg__flash"); }, 1600);
  }

  function start() {
    if (!window.cytoscape || !window.dagre) {
      fail("The graph libraries did not load.");
      return;
    }
    fetch(root.getAttribute("data-src"), { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.json();
      })
      .then(init)
      .catch(function (err) { fail("The graph data did not load (" + err.message + ")."); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else { start(); }
})();
