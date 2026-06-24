/* ============================================================================
   how-it-works.js — rendu des formules, thème, sommaire actif, 3 démos canvas.
   Vanilla JS, aucune dépendance hors KaTeX (chargé dans le <head>).
   ========================================================================== */
"use strict";

// ── Formules KaTeX ────────────────────────────────────────────────────────────
function renderMath() {
  if (!window.renderMathInElement) return;
  window.renderMathInElement(document.body, {
    delimiters: [
      { left: "\\[", right: "\\]", display: true },
      { left: "\\(", right: "\\)", display: false },
    ],
    throwOnError: false,
  });
}
window.addEventListener("DOMContentLoaded", renderMath);
window.addEventListener("load", renderMath);

// ── Thème clair / sombre (même clé que l'app) ─────────────────────────────────
const themeBtn = document.getElementById("theme-toggle");
if (themeBtn) themeBtn.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("sc-theme", next); } catch (e) { /* */ }
});

// ── Sommaire : surlignage de la section visible ───────────────────────────────
(() => {
  const links = [...document.querySelectorAll(".doc-toc a")];
  const map = new Map();
  links.forEach((a) => { const id = a.getAttribute("href").slice(1); const el = document.getElementById(id); if (el) map.set(el, a); });
  const obs = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (!e.isIntersecting) return;
      links.forEach((a) => a.classList.remove("active"));
      const a = map.get(e.target);
      if (a) a.classList.add("active");
    });
  }, { rootMargin: "-72px 0px -70% 0px", threshold: 0 });
  map.forEach((_a, el) => obs.observe(el));
})();

// ── Utilitaires communs aux démos ─────────────────────────────────────────────
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888"; }
function pal() {
  return {
    brand: cssVar("--brand"), brandHi: cssVar("--brand-hi"),
    text: cssVar("--text"), dim: cssVar("--text-dim"), faint: cssVar("--text-faint"),
    line: cssVar("--line"), lineSoft: cssVar("--line-soft"), surface: cssVar("--surface-1"),
    sev0: cssVar("--sev0"), sev2: cssVar("--sev2"), sev3: cssVar("--sev3"), sev5: cssVar("--sev5"),
  };
}
const CLUSTER_COLORS = () => [cssVar("--brand"), cssVar("--sev0"), cssVar("--sev3"), "#a06bf0", cssVar("--sev5"), "#19b3a6", "#e3679b", cssVar("--sev2")];

function fitCanvas(cv) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const cssW = cv.clientWidth || cv.parentElement.clientWidth;
  const cssH = parseInt(cv.getAttribute("height"), 10) || 280;
  cv.width = Math.round(cssW * dpr); cv.height = Math.round(cssH * dpr);
  cv.style.height = cssH + "px";
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w: cssW, h: cssH };
}
function mulberry32(a) { return function () { a |= 0; a = (a + 0x6D2B79F5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }

const demos = [];
function redrawAll() { demos.forEach((d) => { try { d(); } catch (e) { /* */ } }); }
let _rt; window.addEventListener("resize", () => { clearTimeout(_rt); _rt = setTimeout(redrawAll, 120); });
new MutationObserver(redrawAll).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

// ════════════════════════════ DÉMO 1 — DBSCAN ════════════════════════════════
(() => {
  const cv = document.getElementById("db-canvas"); if (!cv) return;
  const epsEl = document.getElementById("db-eps"), minEl = document.getElementById("db-min");
  const epsO = document.getElementById("db-eps-o"), minO = document.getElementById("db-min-o");
  const readout = document.getElementById("db-readout");
  let pts = [], seed = 7;

  function gen(w, h) {
    const r = mulberry32(seed);
    pts = [];
    const blobs = [[0.28, 0.40], [0.62, 0.30], [0.55, 0.70]];
    blobs.forEach(([fx, fy], k) => {
      const n = 12 + Math.floor(r() * 6);
      for (let i = 0; i < n; i++) {
        const ang = r() * 6.283, rad = (0.6 + r() * 0.6) * (0.07 + 0.02 * k);
        pts.push({ x: (fx + Math.cos(ang) * rad) * w, y: (fy + Math.sin(ang) * rad) * h });
      }
    });
    for (let i = 0; i < 10; i++) pts.push({ x: (0.08 + r() * 0.84) * w, y: (0.08 + r() * 0.84) * h }); // bruit
  }

  function dbscan(eps, minPts) {
    const n = pts.length, lab = new Array(n).fill(-2);
    const reg = (i) => { const o = []; for (let j = 0; j < n; j++) { const dx = pts[i].x - pts[j].x, dy = pts[i].y - pts[j].y; if (dx * dx + dy * dy <= eps * eps) o.push(j); } return o; };
    let cid = -1;
    for (let i = 0; i < n; i++) {
      if (lab[i] !== -2) continue;
      const nb = reg(i);
      if (nb.length < minPts) { lab[i] = -1; continue; }
      cid++; lab[i] = cid;
      const seeds = nb.slice();
      for (let s = 0; s < seeds.length; s++) {
        const q = seeds[s];
        if (lab[q] === -1) lab[q] = cid;
        if (lab[q] !== -2) continue;
        lab[q] = cid;
        const nb2 = reg(q);
        if (nb2.length >= minPts) for (const x of nb2) if (!seeds.includes(x)) seeds.push(x);
      }
    }
    return { lab, k: cid + 1 };
  }

  function draw() {
    const { ctx, w, h } = fitCanvas(cv);
    if (!pts.length) gen(w, h);
    const eps = +epsEl.value, minPts = +minEl.value;
    const { lab, k } = dbscan(eps, minPts);
    const P = pal(), COL = CLUSTER_COLORS();
    ctx.clearRect(0, 0, w, h);
    // halo ε autour des points cœurs (premier point de chaque cluster ≈ illustratif)
    ctx.save();
    for (let i = 0; i < pts.length; i++) {
      if (lab[i] < 0) continue;
      ctx.beginPath(); ctx.arc(pts[i].x, pts[i].y, eps, 0, 6.283);
      ctx.fillStyle = COL[lab[i] % COL.length] + "14"; ctx.fill();
    }
    ctx.restore();
    let noise = 0;
    for (let i = 0; i < pts.length; i++) {
      const c = lab[i] < 0 ? P.faint : COL[lab[i] % COL.length];
      if (lab[i] < 0) noise++;
      ctx.beginPath(); ctx.arc(pts[i].x, pts[i].y, lab[i] < 0 ? 3.2 : 4.4, 0, 6.283);
      ctx.fillStyle = c; ctx.fill();
      if (lab[i] < 0) { ctx.strokeStyle = P.line; ctx.lineWidth = 1; ctx.stroke(); }
    }
    epsO.textContent = eps; minO.textContent = minPts;
    readout.innerHTML = `<b>${k}</b> cellule${k > 1 ? "s" : ""} détectée${k > 1 ? "s" : ""} · <span class="hot">${noise}</span> impact${noise > 1 ? "s" : ""} de bruit · ${pts.length} points`;
  }
  epsEl.addEventListener("input", draw); minEl.addEventListener("input", draw);
  document.getElementById("db-regen").addEventListener("click", () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; const r = fitCanvas(cv); gen(r.w, r.h); draw(); });
  demos.push(draw); draw();
})();

// ════════════════════════════ DÉMO 2 — LIGHTNING JUMP ════════════════════════
(() => {
  const cv = document.getElementById("jp-canvas"); if (!cv) return;
  const ampEl = document.getElementById("jp-amp"), ampO = document.getElementById("jp-amp-o");
  const readout = document.getElementById("jp-readout");
  const N = 49, TMIN = 12, JUMP_AT = 6.5, L = 2; // points, minutes, début flambée, lookback min
  let series = [], reveal = N, anim = null;

  function build() {
    const amp = +ampEl.value / 100;
    const r = mulberry32(99);
    series = [];
    for (let i = 0; i < N; i++) {
      const t = (i / (N - 1)) * TMIN;
      let base = 5 + Math.sin(t * 1.1) * 1.2;
      if (t > JUMP_AT) base += amp * 32 * (1 - Math.exp(-(t - JUMP_AT) / 1.2));
      series.push({ t, r: Math.max(0, base + (r() - 0.5) * 1.6) });
    }
  }
  function interp(tq) {
    for (let i = 1; i < series.length; i++) if (series[i].t >= tq) {
      const a = series[i - 1], b = series[i], f = (tq - a.t) / (b.t - a.t || 1);
      return a.r + (b.r - a.r) * f;
    }
    return series[series.length - 1].r;
  }
  function analyse() {
    const dfrdt = series.map((p) => p.t - L < 0 ? null : (p.r - interp(p.t - L)) / L);
    const bg = dfrdt.filter((d, i) => d !== null && series[i].t < JUMP_AT);
    const mean = bg.reduce((s, x) => s + x, 0) / (bg.length || 1);
    const sigma = Math.sqrt(bg.reduce((s, x) => s + (x - mean) ** 2, 0) / (bg.length || 1)) || 0.5;
    let detIdx = -1;
    for (let i = 0; i < N; i++) if (i < reveal && dfrdt[i] !== null && dfrdt[i] / sigma >= 2 && series[i].r >= 10) { detIdx = i; break; }
    const lvl = dfrdt.map((d) => d === null ? 0 : d / sigma);
    return { dfrdt, sigma, detIdx, lvl };
  }
  function draw() {
    const { ctx, w, h } = fitCanvas(cv);
    const P = pal();
    const padL = 38, padR = 12, padT = 14, padB = 26;
    const x = (t) => padL + (t / TMIN) * (w - padL - padR);
    const maxR = Math.max(20, ...series.map((s) => s.r)) * 1.1;
    const y = (v) => h - padB - (v / maxR) * (h - padT - padB);
    ctx.clearRect(0, 0, w, h);
    // grille
    ctx.strokeStyle = P.lineSoft; ctx.lineWidth = 1; ctx.fillStyle = P.faint; ctx.font = "10px ui-monospace,monospace";
    for (let g = 0; g <= maxR; g += 10) { ctx.beginPath(); ctx.moveTo(padL, y(g)); ctx.lineTo(w - padR, y(g)); ctx.stroke(); ctx.fillText(String(g), 6, y(g) + 3); }
    for (let t = 0; t <= TMIN; t += 3) ctx.fillText(t + "′", x(t) - 4, h - 8);
    const A = analyse();
    // seuil 10 flashs/min (plancher)
    ctx.setLineDash([4, 4]); ctx.strokeStyle = P.sev2; ctx.beginPath(); ctx.moveTo(padL, y(10)); ctx.lineTo(w - padR, y(10)); ctx.stroke(); ctx.setLineDash([]);
    // courbe du taux
    ctx.beginPath();
    for (let i = 0; i < Math.min(reveal, N); i++) { const px = x(series[i].t), py = y(series[i].r); i ? ctx.lineTo(px, py) : ctx.moveTo(px, py); }
    ctx.strokeStyle = P.brand; ctx.lineWidth = 2; ctx.stroke();
    // remplissage léger
    ctx.lineTo(x(series[Math.min(reveal, N) - 1].t), y(0)); ctx.lineTo(x(0), y(0)); ctx.closePath();
    ctx.fillStyle = P.brand + "16"; ctx.fill();
    // marqueur de détection
    if (A.detIdx >= 0) {
      const px = x(series[A.detIdx].t);
      ctx.setLineDash([3, 3]); ctx.strokeStyle = P.sev5; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(px, padT); ctx.lineTo(px, h - padB); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = P.sev5; ctx.beginPath(); ctx.arc(px, y(series[A.detIdx].r), 5, 0, 6.283); ctx.fill();
      ctx.font = "600 11px ui-monospace,monospace"; ctx.fillText("⚡ jump 2σ", px + 7, padT + 12);
    }
    const peak = Math.max(...A.lvl.slice(0, Math.min(reveal, N)));
    readout.innerHTML = A.detIdx >= 0
      ? `Jump détecté à <b>t = ${series[A.detIdx].t.toFixed(1)}′</b> · niveau DFRDT/σ = <span class="hot">${A.lvl[A.detIdx].toFixed(1)}σ</span> (seuil 2σ) · σ fond = ${A.sigma.toFixed(2)}`
      : `Pas de jump · niveau max <b>${peak.toFixed(1)}σ</b> &lt; 2σ · ligne pointillée = plancher 10 flashs/min`;
  }
  function replay() {
    if (anim) cancelAnimationFrame(anim);
    reveal = 6; const step = () => { reveal += 1; draw(); if (reveal < N) anim = requestAnimationFrame(() => setTimeout(step, 28)); };
    step();
  }
  ampEl.addEventListener("input", () => { ampO.textContent = ampEl.value; build(); reveal = N; draw(); });
  document.getElementById("jp-replay").addEventListener("click", () => { build(); replay(); });
  build(); demos.push(() => { draw(); }); draw();
})();

// ════════════════════════════ DÉMO 3 — CÔNE D'ETA ════════════════════════════
(() => {
  const cv = document.getElementById("et-canvas"); if (!cv) return;
  const headEl = document.getElementById("et-head"), spdEl = document.getElementById("et-spd"), sigEl = document.getElementById("et-sig");
  const headO = document.getElementById("et-head-o"), spdO = document.getElementById("et-spd-o"), sigO = document.getElementById("et-sig-o");
  const readout = document.getElementById("et-readout");
  const DOMAIN = 170;            // km de large
  const RING = 20, CELLR = 12;   // anneau d'alerte, rayon de cellule (km)
  const C = { x: 56, y: 40 };    // position cellule (km, est/nord depuis HOME)

  function draw() {
    const { ctx, w, h } = fitCanvas(cv);
    const P = pal();
    const scale = Math.min(w, h) / DOMAIN;
    const HX = w / 2, HY = h / 2;
    const sx = (kx) => HX + kx * scale, sy = (ky) => HY - ky * scale;
    const head = (+headEl.value) * Math.PI / 180, spd = +spdEl.value, sig = +sigEl.value;
    const reff = CELLR + RING;
    const V = { x: Math.sin(head) * spd / 60, y: Math.cos(head) * spd / 60 }; // km/min (est, nord)
    const vmag2 = V.x * V.x + V.y * V.y, vmag = Math.sqrt(vmag2);

    // géométrie : t* approche, distance mini, ETA-bord (quadratique)
    const tStar = (-(C.x) * V.x - C.y * V.y) / vmag2; // (H-C)·V/|V|², H=0
    const Pc = { x: C.x + V.x * Math.max(tStar, 0), y: C.y + V.y * Math.max(tStar, 0) };
    const dmin = Math.hypot(Pc.x, Pc.y);
    const a = vmag2, b = 2 * (C.x * V.x + C.y * V.y), c = (C.x * C.x + C.y * C.y) - reff * reff;
    const disc = b * b - 4 * a * c;
    let eta = null;
    if (disc >= 0) { const sq = Math.sqrt(disc); const r1 = (-b - sq) / (2 * a), r2 = (-b + sq) / (2 * a); const roots = [r1, r2].filter((r) => r >= 0); if (roots.length) eta = Math.min(...roots); }

    ctx.clearRect(0, 0, w, h);
    // grille de distance
    ctx.strokeStyle = P.lineSoft; ctx.fillStyle = P.faint; ctx.font = "10px ui-monospace,monospace";
    [40, 80].forEach((km) => { ctx.beginPath(); ctx.arc(HX, HY, km * scale, 0, 6.283); ctx.stroke(); ctx.fillText(km + " km", HX + 3, HY - km * scale + 12); });
    // anneau d'alerte effectif
    ctx.beginPath(); ctx.arc(HX, HY, reff * scale, 0, 6.283); ctx.fillStyle = P.sev5 + "12"; ctx.fill();
    ctx.setLineDash([5, 4]); ctx.strokeStyle = P.sev3; ctx.lineWidth = 1.4; ctx.stroke(); ctx.setLineDash([]);
    // HOME
    ctx.fillStyle = P.brandHi; ctx.beginPath(); ctx.arc(HX, HY, 5, 0, 6.283); ctx.fill();
    ctx.fillStyle = P.dim; ctx.font = "600 11px ui-monospace,monospace"; ctx.fillText("HOME", HX + 9, HY + 4);

    // cône d'incertitude : largeur croissante (≈ σ) le long de V
    const udir = { x: V.x / vmag, y: V.y / vmag }, perp = { x: -udir.y, y: udir.x };
    const Lcone = Math.max(0, tStar) * vmag; // jusqu'à l'approche la plus proche
    const tip = { x: C.x + udir.x * Lcone, y: C.y + udir.y * Lcone };
    const hw = sig; // demi-largeur au bout ≈ σ (km)
    ctx.beginPath();
    ctx.moveTo(sx(C.x), sy(C.y));
    ctx.lineTo(sx(tip.x + perp.x * hw), sy(tip.y + perp.y * hw));
    ctx.lineTo(sx(tip.x - perp.x * hw), sy(tip.y - perp.y * hw));
    ctx.closePath(); ctx.fillStyle = P.brand + "1f"; ctx.fill();

    // trajectoire projetée
    const far = { x: C.x + udir.x * 200, y: C.y + udir.y * 200 };
    ctx.setLineDash([6, 5]); ctx.strokeStyle = P.brand; ctx.lineWidth = 1.6;
    ctx.beginPath(); ctx.moveTo(sx(C.x), sy(C.y)); ctx.lineTo(sx(far.x), sy(far.y)); ctx.stroke(); ctx.setLineDash([]);

    // point d'approche la plus proche
    if (tStar > 0) {
      ctx.strokeStyle = P.faint; ctx.setLineDash([2, 3]); ctx.beginPath(); ctx.moveTo(HX, HY); ctx.lineTo(sx(Pc.x), sy(Pc.y)); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = P.text; ctx.beginPath(); ctx.arc(sx(Pc.x), sy(Pc.y), 3.5, 0, 6.283); ctx.fill();
    }
    // cellule
    const insideNow = Math.hypot(C.x, C.y) <= reff;
    const threat = insideNow || eta !== null;   // déjà dedans, ou va y entrer
    const cc = threat ? P.sev5 : P.brandHi;
    ctx.beginPath(); ctx.arc(sx(C.x), sy(C.y), CELLR * scale, 0, 6.283);
    ctx.fillStyle = cc + "22"; ctx.fill(); ctx.strokeStyle = cc; ctx.lineWidth = 1.6; ctx.stroke();
    ctx.fillStyle = cc; ctx.beginPath(); ctx.arc(sx(C.x), sy(C.y), 3, 0, 6.283); ctx.fill();
    // flèche vitesse
    const aTip = { x: C.x + udir.x * 22, y: C.y + udir.y * 22 };
    ctx.strokeStyle = cc; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(sx(C.x), sy(C.y)); ctx.lineTo(sx(aTip.x), sy(aTip.y)); ctx.stroke();

    headO.textContent = headEl.value + "°"; spdO.textContent = spdEl.value; sigO.textContent = sig;
    const head1 = insideNow
      ? `<span class="hot">cellule déjà dans l'anneau</span>`
      : (eta !== null
        ? `ETA-bord : <b>${eta.toFixed(0)} min</b> avant la foudre dans l'anneau`
        : `<b>n'atteint pas</b> l'anneau sur cette trajectoire`);
    readout.innerHTML = `${head1} · approche mini <b>${dmin.toFixed(0)} km</b> · anneau effectif ${reff} km`;
  }
  [headEl, spdEl, sigEl].forEach((el) => el.addEventListener("input", draw));
  demos.push(draw); draw();
})();
