// Lightweight dependency-free SVG charts: activity timeline + squarified treemap.

const SVG_NS = "http://www.w3.org/2000/svg";
const COLORS = ["#58a6ff", "#bc8cff", "#3fb950", "#d29922", "#f85149", "#39c5cf", "#db61a2", "#7ee787"];
export const ADDED_COLOR = "#3fb950";
export const REMOVED_COLOR = "#f85149";

function el(name, attrs = {}, parent = null) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}

export function fmtInt(n) {
  if (n === null || n === undefined) return "–";
  return Number(n).toLocaleString("en-US");
}

export function fmtK(n) {
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (abs >= 1000) return (n / 1000).toFixed(abs >= 10000 ? 0 : 1) + "k";
  return String(n);
}

export function fmtDate(ts, withTime = false) {
  if (!ts && ts !== 0) return "–";
  const d = new Date(ts * 1000);
  const date = d.toLocaleDateString("en-CA"); // YYYY-MM-DD
  if (!withTime) return date;
  return `${date} ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`;
}

// A fixed-position tooltip that follows the pointer.
let tipEl = null;
function tip() {
  if (!tipEl) {
    tipEl = document.createElement("div");
    tipEl.className = "tt hidden";
    document.body.appendChild(tipEl);
  }
  return tipEl;
}
function showTip(html, x, y) {
  const t = tip();
  t.innerHTML = html;
  t.classList.remove("hidden");
  const pad = 14;
  const w = t.offsetWidth;
  const h = t.offsetHeight;
  let left = x + pad;
  let top = y + pad;
  if (left + w > window.innerWidth - 8) left = x - w - pad;
  if (top + h > window.innerHeight - 8) top = y - h - pad;
  t.style.left = left + "px";
  t.style.top = top + "px";
}
function hideTip() {
  if (tipEl) tipEl.classList.add("hidden");
}

/* ------------------------------------------------------------------ */
/* Timeline: mirrored area chart (additions up, deletions down)        */

export function renderTimeline(container, points, bucketSeconds) {
  container.innerHTML = "";
  const width = Math.max(container.clientWidth || 600, 320);
  const height = 240;
  const m = { top: 14, right: 12, bottom: 26, left: 46 };
  const iw = width - m.left - m.right;
  const ih = height - m.top - m.bottom;

  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height }, container);
  if (!points || !points.length) {
    el("text", { x: width / 2, y: height / 2, "text-anchor": "middle" }, svg).textContent =
      "no activity in this selection";
    return;
  }

  const xs = points.map((p) => p[0]);
  const x0 = xs[0];
  const x1 = xs[xs.length - 1] + bucketSeconds;
  const span = Math.max(1, x1 - x0);
  const maxY = Math.max(1, ...points.map((p) => Math.max(p[2], p[3])));
  const sx = (ts) => m.left + ((ts - x0) / span) * iw;
  const sy = (v) => m.top + ih / 2 - (v / maxY) * (ih / 2 - 6);

  const zero = m.top + ih / 2;

  // grid + y labels
  for (const frac of [-1, -0.5, 0.5, 1]) {
    const v = maxY * frac;
    const y = sy(v);
    el("line", { x1: m.left, x2: width - m.right, y1: y, y2: y, stroke: "#232a33", "stroke-dasharray": "3 4" }, svg);
    el("text", { x: m.left - 6, y: y + 3, "text-anchor": "end" }, svg).textContent = fmtK(v);
  }
  el("line", { x1: m.left, x2: width - m.right, y1: zero, y2: zero, stroke: "#3a424d" }, svg);

  // areas
  function areaPath(selector, baseline) {
    let d = `M ${sx(xs[0])} ${baseline}`;
    points.forEach((p) => {
      d += ` L ${sx(p[0]).toFixed(1)} ${selector(p).toFixed(1)}`;
    });
    d += ` L ${sx(x1 - bucketSeconds / 2)} ${baseline} Z`;
    return d;
  }
  el("path", {
    d: areaPath((p) => sy(p[2]), zero),
    fill: ADDED_COLOR, "fill-opacity": 0.28, stroke: ADDED_COLOR, "stroke-width": 1.4,
  }, svg);
  el("path", {
    d: areaPath((p) => sy(-p[3]), zero),
    fill: REMOVED_COLOR, "fill-opacity": 0.28, stroke: REMOVED_COLOR, "stroke-width": 1.4,
  }, svg);

  // x labels (6 ticks)
  const ticks = 6;
  for (let i = 0; i < ticks; i++) {
    const ts = x0 + (span * i) / (ticks - 1);
    const label = bucketSeconds >= 2500000
      ? new Date(ts * 1000).toLocaleDateString("en", { year: "numeric", month: "short" })
      : fmtDate(ts);
    el("text", { x: sx(ts), y: height - 8, "text-anchor": i === 0 ? "start" : i === ticks - 1 ? "end" : "middle" }, svg)
      .textContent = label;
  }

  // hover
  const guide = el("line", { x1: 0, x2: 0, y1: m.top, y2: m.top + ih, stroke: "#58a6ff", "stroke-width": 1, opacity: 0 }, svg);
  const overlay = el("rect", { x: m.left, y: m.top, width: iw, height: ih, fill: "transparent" }, svg);
  overlay.addEventListener("mousemove", (ev) => {
    const rect = svg.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * width;
    const ts = x0 + ((px - m.left) / iw) * span;
    let best = 0;
    let bestDist = Infinity;
    points.forEach((p, i) => {
      const d = Math.abs(p[0] + bucketSeconds / 2 - ts);
      if (d < bestDist) { bestDist = d; best = i; }
    });
    const p = points[best];
    const cx = sx(p[0] + bucketSeconds / 2);
    guide.setAttribute("x1", cx);
    guide.setAttribute("x2", cx);
    guide.setAttribute("opacity", 0.7);
    const label = bucketSeconds > 86400
      ? `${fmtDate(p[0])} – ${fmtDate(p[0] + bucketSeconds - 1)}`
      : fmtDate(p[0]);
    showTip(
      `<div class="t">${label}</div>
       <div class="r"><span>commits</span><b>${fmtInt(p[1])}</b></div>
       <div class="r"><span>added</span><b style="color:${ADDED_COLOR}">+${fmtInt(p[2])}</b></div>
       <div class="r"><span>removed</span><b style="color:${REMOVED_COLOR}">−${fmtInt(p[3])}</b></div>
       <div class="r"><span>churn</span><b>${fmtInt(p[2] + p[3])}</b></div>`,
      ev.clientX, ev.clientY,
    );
  });
  overlay.addEventListener("mouseleave", () => {
    guide.setAttribute("opacity", 0);
    hideTip();
  });

  // legend
  el("circle", { cx: m.left + 6, cy: 10, r: 4, fill: ADDED_COLOR }, svg);
  el("text", { x: m.left + 15, y: 13 }, svg).textContent = "added";
  el("circle", { cx: m.left + 68, cy: 10, r: 4, fill: REMOVED_COLOR }, svg);
  el("text", { x: m.left + 77, y: 13 }, svg).textContent = "removed";
}

/* ------------------------------------------------------------------ */
/* Squarified treemap                                                  */

function squarify(children, x, y, w, h, out) {
  const items = children.filter((c) => c.value > 0).sort((a, b) => b.value - a.value);
  if (!items.length || w <= 0 || h <= 0) return;
  const total = items.reduce((s, c) => s + c.value, 0);
  const area = w * h;

  const worst = (row, side) => {
    const s = row.reduce((a, c) => a + c.area, 0);
    let maxA = 0;
    let minA = Infinity;
    for (const c of row) {
      if (c.area > maxA) maxA = c.area;
      if (c.area < minA) minA = c.area;
    }
    return Math.max((side * side * maxA) / (s * s), (s * s) / (side * side * minA));
  };

  let rx = x;
  let ry = y;
  let rw = w;
  let rh = h;
  let i = 0;
  while (i < items.length) {
    const side = Math.min(rw, rh);
    const row = [{ node: items[i].node, area: (items[i].value / total) * area }];
    i++;
    while (i < items.length) {
      const cand = { node: items[i].node, area: (items[i].value / total) * area };
      const withCand = [...row, cand];
      if (worst(withCand, side) <= worst(row, side)) {
        row.push(cand);
        i++;
      } else break;
    }
    const rowArea = row.reduce((a, c) => a + c.area, 0);
    if (rw >= rh) {
      const stripW = Math.min(rw, rowArea / Math.max(rh, 1e-9));
      let oy = ry;
      for (const c of row) {
        const cellH = Math.min(rh - (oy - ry), c.area / Math.max(stripW, 1e-9));
        out.push({ rect: { x: rx, y: oy, w: Math.max(stripW - 1.2, 0.4), h: Math.max(cellH - 1.2, 0.4) }, node: c.node });
        oy += cellH;
      }
      rx += stripW;
      rw -= stripW;
    } else {
      const stripH = Math.min(rh, rowArea / Math.max(rw, 1e-9));
      let ox = rx;
      for (const c of row) {
        const cellW = Math.min(rw - (ox - rx), c.area / Math.max(stripH, 1e-9));
        out.push({ rect: { x: ox, y: ry, w: Math.max(cellW - 1.2, 0.4), h: Math.max(stripH - 1.2, 0.4) }, node: c.node });
        ox += cellW;
      }
      ry += stripH;
      rh -= stripH;
    }
  }
}

export function renderTreemap(container, root, onNodeClick) {
  container.innerHTML = "";
  const width = Math.max(container.clientWidth || 600, 320);
  const height = 300;
  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height }, container);
  if (!root || !root.children || !root.children.length) {
    el("text", { x: width / 2, y: height / 2, "text-anchor": "middle" }, svg).textContent =
      "no churn in this selection";
    return;
  }

  const level1 = [];
  squarify(
    root.children.map((c, i) => ({ node: { ...c, _ci: i }, value: Math.max(c.churn, 1) })),
    0, 0, width, height, level1,
  );

  for (const { rect, node } of level1) {
    const hue = COLORS[(node._ci ?? 0) % COLORS.length];
    const rectEl = el("rect", {
      x: rect.x.toFixed(1), y: rect.y.toFixed(1), width: rect.w.toFixed(1), height: rect.h.toFixed(1),
      rx: 3, fill: hue, "fill-opacity": node.children && node.children.length ? 0.32 : 0.5,
      stroke: "#0d1117", "stroke-width": 1, style: "cursor:pointer",
    }, svg);
    rectEl.addEventListener("click", () => onNodeClick && onNodeClick(node.path));
    rectEl.addEventListener("mousemove", (ev) => {
      showTip(
        `<div class="t">${node.path || "repo root"}</div>
         <div class="r"><span>churn</span><b>${fmtInt(node.churn)}</b></div>
         <div class="r"><span>added / removed</span><b>+${fmtK(node.added)} / −${fmtK(node.removed)}</b></div>
         <div class="r"><span>modifications</span><b>${fmtInt(node.modifications)}</b></div>
         <div class="r"><span>growth</span><b>${node.added - node.removed >= 0 ? "+" : ""}${fmtK(node.added - node.removed)}</b></div>`,
        ev.clientX, ev.clientY,
      );
    });
    rectEl.addEventListener("mouseleave", hideTip);

    if (node.children && node.children.length && rect.w > 90 && rect.h > 56) {
      const inner = [];
      squarify(
        node.children.map((c, i) => ({ node: { ...c, _ci: i + 1 }, value: Math.max(c.churn, 1) })),
        rect.x + 3, rect.y + 3, rect.w - 6, rect.h - 6, inner,
      );
      for (const sub of inner) {
        const subEl = el("rect", {
          x: sub.rect.x.toFixed(1), y: sub.rect.y.toFixed(1),
          width: sub.rect.w.toFixed(1), height: sub.rect.h.toFixed(1),
          rx: 2, fill: COLORS[(sub.node._ci ?? 1) % COLORS.length], "fill-opacity": 0.4,
          stroke: "#0d1117", "stroke-width": 0.8, style: "cursor:pointer",
        }, svg);
        subEl.addEventListener("click", (ev) => {
          ev.stopPropagation();
          onNodeClick && onNodeClick(sub.node.path);
        });
        subEl.addEventListener("mousemove", (ev) => {
          showTip(
            `<div class="t">${sub.node.path}</div>
             <div class="r"><span>churn</span><b>${fmtInt(sub.node.churn)}</b></div>
             <div class="r"><span>added / removed</span><b>+${fmtK(sub.node.added)} / −${fmtK(sub.node.removed)}</b></div>`,
            ev.clientX, ev.clientY,
          );
        });
        subEl.addEventListener("mouseleave", hideTip);
        if (sub.rect.w > 64 && sub.rect.h > 18) {
          el("text", {
            x: (sub.rect.x + 5).toFixed(1), y: (sub.rect.y + 13).toFixed(1), "pointer-events": "none",
          }, svg).textContent = truncate(sub.node.name, sub.rect.w / 6.2);
        }
      }
    }

    if (!node.children || !node.children.length) {
      if (rect.w > 60 && rect.h > 18) {
        el("text", {
          x: (rect.x + 6).toFixed(1), y: (rect.y + 14).toFixed(1), "pointer-events": "none",
        }, svg).textContent = truncate(node.name, rect.w / 6.2);
      }
    } else if (rect.h > 20 && rect.w > 40) {
      el("text", {
        x: (rect.x + 6).toFixed(1), y: (rect.y + 14).toFixed(1), "pointer-events": "none",
        "font-weight": "600",
      }, svg).textContent = truncate(`${node.name}/`, rect.w / 6.6);
    }
  }
}

function truncate(text, maxChars) {
  const n = Math.max(3, Math.floor(maxChars));
  if (text.length <= n) return text;
  return text.slice(0, n - 1) + "…";
}
