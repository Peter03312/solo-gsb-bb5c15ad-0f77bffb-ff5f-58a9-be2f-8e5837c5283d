// 公共 JS 工具
"use strict";

async function api(path, options = {}) {
  const opts = Object.assign({
    method: "GET",
    headers: { "Content-Type": "application/json" },
  }, options);
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, opts);
  let data = null;
  try { data = await resp.json(); } catch (e) { /* 非 JSON */ }
  if (!resp.ok) {
    const err = new Error((data && data.message) || `请求失败（${resp.status}）`);
    err.data = data || {};
    err.status = resp.status;
    throw err;
  }
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2), v);
    } else if (v !== null && v !== undefined) {
      node.setAttribute(k, v);
    }
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function alertBox(kind, message) {
  return el("div", { class: `alert ${kind}` }, message);
}

function yuan(v) {
  return Number(v || 0).toFixed(2);
}

function periodLabel(t) {
  const total = t * 15;
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:` +
         `${String(total % 60).padStart(2, "0")}`;
}

// 简易 SVG 折线图
function lineChart(series, opts = {}) {
  const width = 1100, height = 160, pad = 26;
  const n = (series[0] && series[0].data.length) || 0;
  if (!n) return document.createDocumentFragment();
  let max = 0;
  for (const s of series) max = Math.max(max, ...s.data);
  max = max || 1;
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "chart");
  svg.setAttribute("preserveAspectRatio", "none");

  const x = (i) => pad + i * (width - 2 * pad) / (n - 1);
  const y = (v) => height - pad - v * (height - 2 * pad) / max;

  // 网格
  for (let g = 0; g <= 4; g++) {
    const gy = pad + g * (height - 2 * pad) / 4;
    const line = document.createElementNS(svgNS, "line");
    line.setAttribute("x1", pad); line.setAttribute("x2", width - pad);
    line.setAttribute("y1", gy); line.setAttribute("y2", gy);
    line.setAttribute("stroke", "#e8edf2");
    svg.appendChild(line);
    const txt = document.createElementNS(svgNS, "text");
    txt.setAttribute("x", 4); txt.setAttribute("y", gy + 4);
    txt.setAttribute("font-size", "10"); txt.setAttribute("fill", "#93a0ad");
    txt.textContent = Math.round(max * (1 - g / 4));
    svg.appendChild(txt);
  }
  // x 轴时间标签（每 3 小时）
  for (let t = 0; t < 96; t += 12) {
    const txt = document.createElementNS(svgNS, "text");
    txt.setAttribute("x", x(t) - 12); txt.setAttribute("y", height - 6);
    txt.setAttribute("font-size", "10"); txt.setAttribute("fill", "#93a0ad");
    txt.textContent = periodLabel(t);
    svg.appendChild(txt);
  }
  for (const s of series) {
    const pts = s.data.map((v, i) => `${x(i)},${y(v)}`).join(" ");
    const poly = document.createElementNS(svgNS, "polyline");
    poly.setAttribute("points", pts);
    poly.setAttribute("fill", "none");
    poly.setAttribute("stroke", s.color);
    poly.setAttribute("stroke-width", s.width || 1.6);
    svg.appendChild(poly);
    if (s.area) {
      const p = document.createElementNS(svgNS, "polygon");
      p.setAttribute("points",
        `${x(0)},${height - pad} ${pts} ${x(n - 1)},${height - pad}`);
      p.setAttribute("fill", s.color);
      p.setAttribute("opacity", "0.08");
      svg.insertBefore(p, poly);
    }
  }
  return svg;
}
