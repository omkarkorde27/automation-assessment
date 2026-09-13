// Observation extractor -- runs inside ONE document (one frame).
//
// Produces role / name / value / states / bounds / anchors per node: the
// surface-neutral vocabulary in perception/model.py. Everything here is a
// deliberate re-implementation of what an accessibility tree gives you, rather
// than a call to Playwright's aria_snapshot(), for three reasons:
//
//   1. we need bounding boxes (redaction masking, screenshot annotation, the
//      last-resort coordinate candidate),
//   2. we need ANCHORS -- row labels, column headers, section labels -- which no
//      ARIA snapshot exposes and which are the only durable way into legacy
//      table markup where controls have no accessible name at all,
//   3. writing the role/name derivation ourselves is what makes the claim
//      "a desktop adapter emits the same nodes" concrete rather than aspirational.
//
// Nothing here emits a selector. The output describes what is on screen; how to
// find it again is the locator layer's problem.

() => {
  const MAX_NODES = 500;
  const MAX_TEXT = 200;

  const vw = window.innerWidth || 1;
  const vh = window.innerHeight || 1;

  const clean = (s) => (s || "").replace(/\s+/g, " ").trim().slice(0, MAX_TEXT);

  // ---- visibility -------------------------------------------------------
  function isVisible(el) {
    const cs = window.getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden" || cs.visibility === "collapse") return false;
    if (parseFloat(cs.opacity || "1") === 0) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    return true;
  }

  // ---- role -------------------------------------------------------------
  // Simplified HTML-AAM mapping. An explicit role attribute always wins, which
  // is what legacy apps actually use when they bother at all.
  function roleOf(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit.trim().toLowerCase();

    const tag = el.tagName.toLowerCase();
    if (tag === "input") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      if (t === "hidden") return null;
      if (["submit", "button", "reset", "image"].includes(t)) return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "search") return "searchbox";
      return "textbox"; // text, password, tel, email, url, number, date...
    }
    if (tag === "textarea") return "textbox";
    if (tag === "select") return el.multiple || el.size > 1 ? "listbox" : "combobox";
    if (tag === "button") return "button";
    if (tag === "a") return el.hasAttribute("href") ? "link" : null;
    if (/^h[1-6]$/.test(tag)) return "heading";
    if (tag === "th") return el.closest("tr")?.rowIndex === 0 ? "columnheader" : "rowheader";
    if (tag === "td") return "cell";
    if (tag === "tr") return "row";
    if (tag === "table") return "table";
    if (tag === "label") return "label";
    if (tag === "legend") return "legend";
    if (tag === "fieldset") return "group";
    if (tag === "img") return "img";
    if (tag === "form") return "form";
    if (tag === "li") return "listitem";
    return null;
  }

  const INTERACTIVE = new Set([
    "textbox", "searchbox", "button", "link", "checkbox", "radio",
    "combobox", "listbox", "menuitem", "tab", "switch", "slider",
  ]);

  function hasInteractiveDescendant(el) {
    return !!el.querySelector("input,select,textarea,button,a[href],[role=button],[role=textbox]");
  }

  // ---- accessible name --------------------------------------------------
  // Simplified accname algorithm, in specification precedence order. For the
  // fixture's member-id field this correctly returns "" -- no labelledby, no
  // aria-label, no <label for>, no title, no placeholder. That empty result is
  // the point: it forces anchor-relative locating.
  function accName(el, role) {
    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
      const txt = labelledby
        .split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((n) => n.innerText || n.textContent || "")
        .join(" ");
      if (clean(txt)) return clean(txt);
    }

    const arialabel = el.getAttribute("aria-label");
    if (clean(arialabel)) return clean(arialabel);

    const tag = el.tagName.toLowerCase();

    if (["input", "select", "textarea"].includes(tag)) {
      const t = (el.getAttribute("type") || "").toLowerCase();
      // Button-ish inputs are named by their value, not by a label.
      if (["submit", "button", "reset"].includes(t)) return clean(el.value);
      const lbl = labelElementFor(el);
      if (lbl) return clean(lbl.innerText || lbl.textContent);
      if (clean(el.getAttribute("title"))) return clean(el.getAttribute("title"));
      if (clean(el.getAttribute("placeholder"))) return clean(el.getAttribute("placeholder"));
      return "";
    }

    if (tag === "img") return clean(el.getAttribute("alt"));
    if (clean(el.getAttribute("title"))) return clean(el.getAttribute("title"));

    // Content-named roles.
    if (["button", "link", "heading", "cell", "columnheader", "rowheader", "label",
         "legend", "listitem", "option", "alert", "status", "dialog"].includes(role)) {
      // For a container, use its own text but not a nested control's value.
      return clean(el.innerText || el.textContent);
    }
    return "";
  }

  function labelElementFor(el) {
    if (el.id) {
      const esc = (window.CSS && CSS.escape) ? CSS.escape(el.id) : el.id.replace(/"/g, '\\"');
      const byFor = document.querySelector(`label[for="${esc}"]`);
      if (byFor) return byFor;
    }
    return el.closest("label");
  }

  // ---- value ------------------------------------------------------------
  function valueOf(el, role) {
    const tag = el.tagName.toLowerCase();
    if (tag === "select") {
      const opt = el.options[el.selectedIndex];
      return opt ? clean(opt.text) : "";
    }
    if (tag === "textarea") return clean(el.value);
    if (tag === "input") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      if (["checkbox", "radio"].includes(t)) return el.checked ? "checked" : "unchecked";
      if (["submit", "button", "reset"].includes(t)) return null;
      // Password values are never read out of the DOM. Nothing downstream has a
      // legitimate use for one, so the extractor refuses to carry it at all.
      if (t === "password") return el.value ? "<password>" : "";
      return clean(el.value);
    }
    return null;
  }

  // ---- anchors ----------------------------------------------------------
  function rowLabel(el) {
    const cell = el.closest("td,th");
    const row = el.closest("tr");
    if (!row) return "";
    for (const c of row.querySelectorAll("td,th")) {
      if (c === cell || c.contains(el)) continue;
      if (hasInteractiveDescendant(c)) continue;
      const t = clean(c.innerText || c.textContent);
      if (t) return t;
    }
    return "";
  }

  // Full text of the containing row. A data row has no "label" -- it is
  // identified by what is in it ("the row containing Savings"), which is how a
  // person reads a grid.
  function rowText(el) {
    const row = el.closest("tr");
    return row ? clean(row.innerText || row.textContent) : "";
  }

  function colHeader(el) {
    const cell = el.closest("td,th");
    const row = el.closest("tr");
    const table = el.closest("table");
    if (!cell || !row || !table) return "";
    const idx = Array.prototype.indexOf.call(row.children, cell);
    if (idx < 0) return "";
    const headerRow = table.querySelector("tr");
    if (!headerRow || headerRow === row) return "";
    const hdr = headerRow.children[idx];
    return hdr ? clean(hdr.innerText || hdr.textContent) : "";
  }

  // The heading-like first child of an ancestor container. Legacy apps style a
  // <div> as a panel header instead of using <h1>-<h6>, so a purely semantic
  // heading lookup finds nothing on exactly the screens we care about.
  const TABLE_INTERNALS = new Set(["TD", "TH", "TR", "TBODY", "THEAD", "TFOOT", "TABLE"]);
  function sectionLabel(el) {
    let cur = el.parentElement;
    let hops = 0;
    while (cur && hops++ < 10) {
      // Skip table internals: those are row/column context, already captured by
      // rowLabel/colHeader. A section is the enclosing panel, not the grid.
      if (TABLE_INTERNALS.has(cur.tagName)) { cur = cur.parentElement; continue; }
      const first = cur.firstElementChild;
      if (first && !first.contains(el) && !hasInteractiveDescendant(first)) {
        const t = clean(first.innerText || first.textContent);
        if (t && t.length <= 80) return t;
      }
      cur = cur.parentElement;
    }
    return "";
  }

  function nearestHeading(el) {
    const all = Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6,[role=heading]"));
    let best = "";
    for (const h of all) {
      const pos = h.compareDocumentPosition(el);
      if (pos & Node.DOCUMENT_POSITION_FOLLOWING) best = clean(h.innerText || h.textContent);
    }
    return best;
  }

  function precedingText(el) {
    let sib = el.previousSibling;
    while (sib) {
      if (sib.nodeType === Node.TEXT_NODE) {
        const t = clean(sib.textContent);
        if (t) return t;
      } else if (sib.nodeType === Node.ELEMENT_NODE && !hasInteractiveDescendant(sib)) {
        const t = clean(sib.innerText || sib.textContent);
        if (t) return t;
      }
      sib = sib.previousSibling;
    }
    return "";
  }

  function dialogLabel(el) {
    const dlg = el.closest("[role=dialog],[role=alertdialog],dialog");
    if (!dlg) return "";
    return clean(dlg.getAttribute("aria-label") || dlg.innerText || "");
  }

  // ---- walk -------------------------------------------------------------
  const out = [];
  let seq = 0;

  const walker = document.createTreeWalker(document.body || document, NodeFilter.SHOW_ELEMENT);
  const elements = [];
  while (walker.nextNode()) elements.push(walker.currentNode);

  for (const el of elements) {
    if (out.length >= MAX_NODES) break;

    const role = roleOf(el);
    if (!role) continue;
    if (!isVisible(el)) continue;

    const name = accName(el, role);

    // Structural roles earn a slot only if they carry text a locator could
    // anchor on; otherwise they are noise that crowds out real controls.
    const structural = !INTERACTIVE.has(role);
    if (structural && !name) continue;
    if (structural && ["row", "table", "form", "group"].includes(role)) continue;

    const r = el.getBoundingClientRect();
    out.push({
      node_id: `n${++seq}`,
      role,
      name,
      value: valueOf(el, role),
      states: {
        visible: true,
        enabled: !el.disabled && el.getAttribute("aria-disabled") !== "true",
        readonly: !!el.readOnly,
        required: !!el.required,
        checked: !!el.checked,
        focused: document.activeElement === el,
      },
      bbox: {
        x: r.x, y: r.y, w: r.width, h: r.height,
        nx: r.x / vw, ny: r.y / vh, nw: r.width / vw, nh: r.height / vh,
      },
      anchors: {
        label: (() => { const l = labelElementFor(el); return l ? clean(l.innerText || l.textContent) : ""; })(),
        row_label: rowLabel(el),
        row_text: rowText(el),
        col_header: colHeader(el),
        section_label: sectionLabel(el),
        nearest_heading: nearestHeading(el),
        preceding_text: precedingText(el),
        dialog_label: dialogLabel(el),
      },
      tag: el.tagName.toLowerCase(),
    });
  }

  return { url: document.location.href, title: document.title, nodes: out };
}
