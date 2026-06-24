// ─── StormCell Ops — Leaflet + WebSocket ────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ── State ───────────────────────────────────────────────────────────────────
const state = {
  config: null,
  recentStrikes: [],
  strikeLayer: null,
  cellLayer: null,
  homeMarker: null,
  radiusCircle: null,
  alertCircle: null,
  cells: new Map(),
  uptimeStart: Date.now(),
  alertEnabled: localStorage.getItem("alert-sound") === "1",
  useLocal: localStorage.getItem("tz-local") !== "0",
  alertRules: null,
};

// Règles d'alerte (préférences client, persistées dans le navigateur).
const ALERT_DEFAULTS = { sound: true, jump: true, approach: false, etaMin: 15 };
function loadAlertRules() {
  try { return Object.assign({}, ALERT_DEFAULTS, JSON.parse(localStorage.getItem("alert-rules") || "{}")); }
  catch { return { ...ALERT_DEFAULTS }; }
}
function saveAlertRules() { localStorage.setItem("alert-rules", JSON.stringify(state.alertRules)); }
state.alertRules = loadAlertRules();

// ── Temps : local (défaut) ou UTC ────────────────────────────────────────────
function fmtClock(unix) {
  const d = new Date(unix * 1000);
  return state.useLocal ? d.toTimeString().slice(0, 8) : d.toISOString().substr(11, 8);
}
function fmtDateTime(unix) {
  const d = new Date(unix * 1000);
  return state.useLocal ? d.toLocaleString() : d.toUTCString();
}
function setText(sel, val) {
  const el = $(sel);
  if (!el) return;
  if (el.textContent !== String(val)) {
    el.textContent = val;
    el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
  }
}

// ── Écran de configuration & lancement (sondage des endpoints + paramètres) ──
const boot = { done: false, ready: false, est: 12, phraseTimer: null, pollTimer: null };

function bootDismiss() {
  if (boot.done) return;
  boot.done = true;
  clearInterval(boot.phraseTimer);
  clearInterval(boot.pollTimer);
  const ov = $("#boot-overlay");
  if (ov) { ov.classList.add("done"); setTimeout(() => ov.remove(), 650); }
  setTimeout(() => { try { mapLive.invalidateSize(); } catch (e) { /* */ } }, 80);
}

function bootSetReady(s) {
  if (boot.ready) return;
  boot.ready = true;
  clearInterval(boot.phraseTimer);
  const bar = $("#boot-bar"), ph = $("#boot-phrase"), st = $("#boot-status"), btn = $("#boot-launch");
  if (bar) { bar.style.transition = "width .4s ease"; bar.style.width = "100%"; }
  if (ph) ph.textContent = "Flux prêt — vérifiez vos paramètres puis lancez";
  if (st) {
    st.classList.add("ready");
    st.textContent = `Connecté · ${s.source ?? "?"}` + (s.latency_s != null ? ` · ⏱ ${s.latency_s} s` : "");
  }
  if (btn) btn.classList.add("ready");
}

function applyConfigToMap(cfg) {
  if (!cfg) return;
  state.config = Object.assign(state.config || {}, {
    home: cfg.home, max_distance_km: cfg.max_distance_km, alert_distance_km: cfg.alert_distance_km,
  });
  if (cfg.home && state.homeMarker) state.homeMarker.setLatLng([cfg.home.lat, cfg.home.lon]);
  if (state.alertCircle) {
    if (cfg.home) state.alertCircle.setLatLng([cfg.home.lat, cfg.home.lon]);
    if (cfg.alert_distance_km) state.alertCircle.setRadius(cfg.alert_distance_km * 1000);
  }
  if (state.radiusCircle) {
    if (cfg.home) state.radiusCircle.setLatLng([cfg.home.lat, cfg.home.lon]);
    if (cfg.max_distance_km) state.radiusCircle.setRadius(cfg.max_distance_km * 1000);
    try { mapLive.fitBounds(state.radiusCircle.getBounds(), { padding: [24, 24] }); } catch (e) { /* */ }
  } else if (cfg.home) {
    mapLive.setView([cfg.home.lat, cfg.home.lon], mapLive.getZoom());
  }
  // Recharge la fenêtre d'impacts, désormais bornée à l'anneau côté serveur.
  fetch(`/api/strikes/live?since=${Math.floor(Date.now() / 1000 - 1800)}`)
    .then((r) => r.json()).then((j) => addStrikesBatch(j.strikes || [])).catch(() => { /* */ });
}

async function bootLaunch() {
  const num = (id) => { const v = parseFloat($(id).value); return Number.isFinite(v) ? v : null; };
  const body = {
    home_lat: num("#cfg-lat"), home_lon: num("#cfg-lon"),
    max_distance_km: num("#cfg-maxdist"), alert_distance_km: num("#cfg-alert"),
    cluster_eps_km: num("#cfg-eps"), cluster_min_samples: num("#cfg-minsamp"),
    cell_window_minutes: num("#cfg-window"), strike_ring_km: num("#cfg-ring"),
    min_mds_quality: num("#cfg-minmds"), tick_seconds: num("#cfg-tick"),
  };
  // Règles d'alerte (préférences client).
  const chk = (id) => !!($(id) && $(id).checked);
  state.alertRules = {
    sound: chk("#rule-sound"), jump: chk("#rule-jump"), approach: chk("#rule-approach"),
    etaMin: Math.max(1, num("#rule-eta") || 15),
  };
  saveAlertRules();
  try {
    const r = await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (r.ok) applyConfigToMap((await r.json()).config);
  } catch (e) { console.error("config", e); }
  bootDismiss();
}

function bootInit() {
  const ov = $("#boot-overlay");
  if (!ov) return;
  // Préremplir le formulaire avec la config courante.
  fetch("/api/config").then((r) => r.json()).then((c) => {
    const set = (id, v) => { const el = $(id); if (el && v != null) el.value = v; };
    set("#cfg-lat", c.home && c.home.lat); set("#cfg-lon", c.home && c.home.lon);
    set("#cfg-maxdist", c.max_distance_km); set("#cfg-alert", c.alert_distance_km);
    set("#cfg-ring", c.strike_ring_km); set("#cfg-eps", c.cluster_eps_km);
    set("#cfg-minsamp", c.cluster_min_samples); set("#cfg-minmds", c.min_mds_quality);
    set("#cfg-window", c.cell_window_minutes); set("#cfg-tick", c.tick_seconds);
  }).catch(() => {});

  // Préremplir les règles d'alerte depuis les préférences sauvegardées.
  const ar = state.alertRules;
  if ($("#rule-sound")) $("#rule-sound").checked = ar.sound;
  if ($("#rule-jump")) $("#rule-jump").checked = ar.jump;
  if ($("#rule-approach")) $("#rule-approach").checked = ar.approach;
  if ($("#rule-eta")) $("#rule-eta").value = ar.etaMin;

  // Bouton « Tester les serveurs » : affiche le classement de latence des endpoints.
  const probeBtn = $("#boot-probe");
  if (probeBtn) probeBtn.addEventListener("click", async () => {
    const res = $("#boot-srv-res");
    if (res) res.textContent = "test en cours…";
    probeBtn.disabled = true;
    try {
      const j = await fetch("/api/source/probe").then((x) => x.json());
      if (res) res.textContent = (j.endpoints || []).map((e) => `${e.endpoint} ${e.latency_s != null ? e.latency_s + " s" : "—"}`).join("  ·  ") || "aucun";
    } catch { if (res) res.textContent = "indisponible"; }
    probeBtn.disabled = false;
  });

  const phrases = [
    "Connexion au réseau Blitzortung…",
    "Mesure de la latence des serveurs…",
    "Sélection de l'endpoint le plus rapide…",
    "Calcul des orages…",
    "Triangulation des impacts…",
    "Étalonnage du nowcast…",
  ];
  const ph = $("#boot-phrase");
  let pi = 0;
  const nextPhrase = () => {
    if (!ph) return;
    ph.style.opacity = "0";
    setTimeout(() => { ph.textContent = phrases[pi % phrases.length]; ph.style.opacity = "1"; pi++; }, 180);
  };
  nextPhrase();
  boot.phraseTimer = setInterval(nextPhrase, 1900);

  const bar = $("#boot-bar");
  const startBar = () => {
    if (!bar) return;
    bar.style.transition = "none"; bar.style.width = "6%";
    requestAnimationFrame(() => {
      bar.style.transition = `width ${boot.est}s cubic-bezier(.2,.7,.25,1)`;
      bar.style.width = "92%";
    });
  };
  fetch("/api/stats").then((r) => r.json()).then((s) => {
    if (s.probe_est_s) boot.est = Math.max(2, s.probe_est_s);
    if (s.source) bootSetReady(s); else startBar();
  }).catch(startBar);

  // Sonde la disponibilité du flux (sans fermer l'overlay — l'utilisateur lance).
  boot.pollTimer = setInterval(async () => {
    try { const s = await fetch("/api/stats").then((r) => r.json()); if (s.source) bootSetReady(s); } catch { /* */ }
  }, 800);

  const btn = $("#boot-launch");
  if (btn) btn.addEventListener("click", bootLaunch);
}
bootInit();

// ── Tabs ────────────────────────────────────────────────────────────────────
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.toggle("active", x === t));
  const tab = t.dataset.tab;
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
  if (tab === "history") setTimeout(() => mapHist.invalidateSize(), 60);
  else if (tab === "analyse") loadAnalyse();
  else setTimeout(() => mapLive.invalidateSize(), 60);
}));

// ── Maps ────────────────────────────────────────────────────────────────────
const TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_OPTS = { attribution: "© OpenStreetMap", maxZoom: 18 };

const mapLive = L.map("map-live", { zoomControl: true, preferCanvas: true }).setView([44.24, 4.72], 8);
L.tileLayer(TILE_URL, TILE_OPTS).addTo(mapLive);
mapLive.attributionControl.setPosition("bottomleft");   // libère le coin bas-droit (coords)
state.strikeLayer = L.layerGroup().addTo(mapLive);
state.cellLayer = L.layerGroup().addTo(mapLive);

const mapHist = L.map("map-hist", { zoomControl: true }).setView([44.24, 4.72], 7);
L.tileLayer(TILE_URL, TILE_OPTS).addTo(mapHist);
let histHeat = null;
let histMarkers = null;

// curseur → coordonnées
mapLive.on("mousemove", (e) => {
  const el = $("#ov-coords");
  if (el) el.innerHTML = `<b>${e.latlng.lat.toFixed(3)}</b>°, <b>${((e.latlng.lng + 540) % 360 - 180).toFixed(3)}</b>°`;
});

// ── Couleurs (rampe sévérité 0..5 + distance) ────────────────────────────────
function colorForDistance(d) {
  if (d < 15) return "#f85149";
  if (d < 30) return "#ec8a2c";
  if (d < 250) return "#d6a01f";
  return "#3fb950";
}
function distClass(d) {
  if (d < 15) return "c-red";
  if (d < 30) return "c-orange";
  if (d < 250) return "c-yellow";
  return "c-green";
}
function severityColor(sev) {
  if (sev >= 4.5) return "#f85149";
  if (sev >= 3.5) return "#f2603a";
  if (sev >= 2.5) return "#ec8a2c";
  if (sev >= 1.5) return "#d6a01f";
  if (sev >= 0.5) return "#86c33a";
  return "#3fb950";
}

// ── Impacts (Leaflet canvas circleMarker) ────────────────────────────────────
function strikeStyle(distance, ageMin) {
  const opacity = Math.max(0.12, 1 - ageMin / 30);
  const c = colorForDistance(distance);
  return { radius: distance < 15 ? 6 : 4, color: c, fillColor: c, weight: 1, opacity, fillOpacity: opacity * 0.7 };
}
function strikePopupHtml(s) {
  return `<b>${fmtDateTime(s.ts_unix)}</b><br>` +
    `${s.distance_km.toFixed(1)} km · azimut ${s.bearing_deg.toFixed(0)}°<br>` +
    `${s.mds ?? "?"} stations`;
}
function plotStrike(s) {
  const ageMin = Math.max(0, (Date.now() / 1000 - s.ts_unix) / 60);
  const m = L.circleMarker([s.lat, s.lon], strikeStyle(s.distance_km, ageMin));
  m.bindPopup(strikePopupHtml(s));
  m.addTo(state.strikeLayer);
  m._ts = s.ts_unix;
  m._dist = s.distance_km;
  return m;
}
function addStrike(s) {
  state.recentStrikes.push(s);
  if (state.recentStrikes.length > 5000) state.recentStrikes.shift();
  plotStrike(s);
  prependStrikeRow(s);
  maybeAlert(s);
  if (s.distance_km < 60) {
    const sec = (s.distance_km * 1000) / 343;
    setText("#st-thunder", sec < 90 ? `~${sec.toFixed(0)} s` : `~${(sec / 60).toFixed(1)} min`);
  }
}
function addStrikesBatch(arr) {
  state.strikeLayer.clearLayers();
  const ticker = $("#ticker");
  while (ticker.children.length > 1) ticker.removeChild(ticker.lastChild);  // garde l'en-tête
  state.recentStrikes = [];
  arr.forEach((s) => { state.recentStrikes.push(s); plotStrike(s); });
  if (state.recentStrikes.length > 5000) state.recentStrikes = state.recentStrikes.slice(-5000);
  arr.slice(-30).forEach((s) => prependStrikeRow(s));
}
function prependStrikeRow(s) {
  const ticker = $("#ticker");
  const row = document.createElement("div");
  row.className = "ticker-row";
  const delay = (s.distance_km * 1000) / 343;
  const delayStr = delay < 120 ? `${delay.toFixed(0)} s` : `${(delay / 60).toFixed(1)} min`;
  row.innerHTML =
    `<span class="t">${fmtClock(s.ts_unix)}</span>` +
    `<span class="dir">${cardinal(s.bearing_deg)}</span>` +
    `<span class="d ${distClass(s.distance_km)}">${s.distance_km.toFixed(1)}</span>` +
    `<span class="dl">${delayStr}</span>` +
    `<span class="mds">${s.mds ?? "?"}</span>`;
  const head = ticker.firstElementChild;
  ticker.insertBefore(row, head ? head.nextSibling : null);
  while (ticker.children.length > 31) ticker.removeChild(ticker.lastChild);
  setText("#strikes-count", Math.max(0, ticker.children.length - 1));
}
function cardinal(deg) {
  const dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSO","SO","OSO","O","ONO","NO","NNO"];
  return dirs[Math.floor((deg + 11.25) / 22.5) % 16];
}
function fadeOldMarkers() {
  const now = Date.now() / 1000;
  state.strikeLayer.eachLayer((m) => {
    const age = now - (m._ts || now);
    if (age > 30 * 60) state.strikeLayer.removeLayer(m);
    else m.setStyle(strikeStyle(m._dist ?? 50, age / 60));
  });
}
setInterval(fadeOldMarkers, 30_000);

// ── Cellules ────────────────────────────────────────────────────────────────
function trendBadge(t) {
  if (!t) return "";
  if (t === "growing") return '<span class="badge badge-up">↑ intensif.</span>';
  if (t === "declining") return '<span class="badge badge-down">↓ déclin</span>';
  return "";
}

const cellRefs = new Map();
const notifiedJump = new Set();
const notifiedApproach = new Set();

function selectCell(id) {
  document.querySelectorAll(".cell-card.selected").forEach((el) => el.classList.remove("selected"));
  cellRefs.forEach((ref) => { if (ref.circle) ref.circle.setStyle({ weight: ref.baseWeight }); });
  const ref = cellRefs.get(id);
  if (!ref) return;
  if (ref.card) { ref.card.classList.add("selected"); ref.card.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
  if (ref.circle) { ref.circle.setStyle({ weight: ref.baseWeight + 2 }); ref.circle.bringToFront(); }
}

function cellPopupHtml(c) {
  const vel = c.velocity_kmh ? `${c.velocity_kmh.toFixed(0)} km/h cap ${c.heading_deg?.toFixed(0) ?? "?"}°` : "immobile / inconnu";
  const eta = c.eta_minutes != null
    ? `ETA centroïde ${c.eta_minutes.toFixed(0)}${c.eta_uncertainty_min ? `±${c.eta_uncertainty_min.toFixed(0)}` : ""} min · ${c.closest_approach_km.toFixed(1)} km`
    : ((c.misses ?? 0) > 0 ? `cellule fantôme (${c.misses}×)` : "ne s'approche pas");
  const etaEdge = c.eta_strike_minutes != null ? `<br><i>⚡ foudre dans l'anneau dans ~${c.eta_strike_minutes.toFixed(0)} min</i>` : "";
  const prob = c.strike_probability ? `<br>proba de coup ${Math.round(c.strike_probability * 100)}%` : "";
  const jump = c.jump_detected ? '<br><b style="color:#f85149">⚠ intensification rapide (jump)</b>' : "";
  const lineage = c.parent_id ? ` <small>(split #${c.parent_id})</small>` : "";
  return `<b>Cellule #${c.cell_id}</b>${lineage}<br>
    ${c.strikes_count} impacts · ${c.flash_rate_per_min != null ? c.flash_rate_per_min.toFixed(0) : "?"}/min · rayon ${c.radius_km.toFixed(1)} km<br>
    sévérité ${(c.severity ?? 0).toFixed(1)}/5 · confiance ${Math.round((c.confidence ?? 0) * 100)}%<br>
    ${vel}<br><i>${eta}</i>${etaEdge}${prob}${jump}<br>
    <small class="geo-name muted"></small>
    <small>${c.centroid.lat.toFixed(3)}°, ${c.centroid.lon.toFixed(3)}°</small>`;
}

async function fillGeoName(layer, lat, lon) {
  try {
    const g = await fetch(`/api/geocode?lat=${lat}&lon=${lon}`).then((r) => r.json());
    if (!g.name) return;
    const el = layer.getPopup()?.getElement()?.querySelector(".geo-name");
    if (el) el.textContent = "📍 près de " + g.name;
  } catch { /* géocodage indisponible */ }
}

function drawSpark(canvas, values, color) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const W = canvas.width = canvas.clientWidth || 300;
  const H = canvas.height = 26;
  ctx.clearRect(0, 0, W, H);
  if (!values || values.length < 2) return;
  const max = Math.max(...values, 1);
  const n = values.length;
  const xy = values.map((v, i) => [(i / (n - 1)) * W, H - 2 - (v / max) * (H - 5)]);
  ctx.beginPath();
  xy.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.lineJoin = "round"; ctx.stroke();
  ctx.lineTo(W, H); ctx.lineTo(0, H); ctx.closePath();
  ctx.globalAlpha = 0.13; ctx.fillStyle = color; ctx.fill(); ctx.globalAlpha = 1;
}

function buildCellCard(c, sevCol) {
  const isGhost = (c.misses ?? 0) > 0;
  const card = document.createElement("div");
  card.className = "cell-card" + (c.jump_detected ? " severe" : "") + (isGhost ? " ghost" : "");
  card.style.setProperty("--sev-color", sevCol);

  const prov = !!c.motion_provisional;   // ETA emprunté au consensus régional (P4)
  const tilde = prov ? "~" : "";
  let etaLab = "ETA foudre", etaVal = "—", etaCls = "safe";
  if (c.eta_strike_minutes != null) {
    etaVal = `${tilde}${Math.round(c.eta_strike_minutes)}′`;
    etaCls = (!prov && c.eta_strike_minutes <= 15) ? "urgent" : "";
  } else if (c.eta_minutes != null) {
    etaLab = "ETA centre"; etaVal = `${tilde}${Math.round(c.eta_minutes)}′`; etaCls = "";
  } else if (isGhost) {
    etaLab = "Statut"; etaVal = "perdue";
  }

  const jumpBadge = c.jump_detected ? '<span class="badge badge-jump">⚡ JUMP</span>' : "";
  const provBadge = prov ? '<span class="badge badge-prov" title="ETA provisoire : mouvement emprunté aux cellules voisines (suivi pas encore établi)">≈ prov.</span>' : "";
  const lineage = c.parent_id ? `<span class="badge badge-stable">⑂ #${c.parent_id}</span>` : "";
  const vel = c.velocity_kmh ? `${tilde}${c.velocity_kmh.toFixed(0)} km/h` : "—";
  const cap = c.heading_deg != null ? `${c.heading_deg.toFixed(0)}°` : "—";
  const flash = c.flash_rate_per_min != null ? c.flash_rate_per_min.toFixed(0) : "?";
  const prob = Math.round((c.strike_probability ?? 0) * 100);

  card.innerHTML = `
    <div class="cc-top">
      <span class="cc-id">#${c.cell_id}</span>
      <span class="sev-chip">${(c.severity ?? 0).toFixed(1)}</span>
      ${jumpBadge}${provBadge}${trendBadge(c.intensity_trend)}${lineage}
      <span class="cc-eta"><span class="lab">${etaLab}</span><br><span class="val ${etaCls}">${etaVal}</span></span>
    </div>
    <canvas class="spark"></canvas>
    <div class="cc-metrics">
      <div class="cc-m"><div class="ml">Vitesse</div><div class="mv">${vel}</div></div>
      <div class="cc-m"><div class="ml">Cap</div><div class="mv">${cap}</div></div>
      <div class="cc-m"><div class="ml">Flash/min</div><div class="mv">${flash}</div></div>
      <div class="cc-m"><div class="ml">Rayon</div><div class="mv">${c.radius_km.toFixed(0)} km</div></div>
      <div class="cc-m"><div class="ml">Impacts</div><div class="mv">${c.strikes_count}</div></div>
      <div class="cc-m"><div class="ml">Confiance</div><div class="mv">${Math.round((c.confidence ?? 0) * 100)}%</div></div>
      <div class="prob-wrap">
        <div class="ml" style="font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--text-faint)">Proba de coup (30 min) · ${prob}%</div>
        <div class="prob-track"><div class="prob-fill" style="width:${prob}%"></div></div>
      </div>
    </div>`;
  return card;
}

function updateThreatBanner(cells) {
  const banner = $("#threat-banner");
  if (!banner) return;
  const appr = cells
    .filter((c) => c.eta_strike_minutes != null && (c.misses ?? 0) === 0 && !c.motion_provisional)
    .sort((a, b) => a.eta_strike_minutes - b.eta_strike_minutes);
  const jump = cells.find((c) => c.jump_detected && (c.misses ?? 0) === 0);
  if (appr.length && appr[0].eta_strike_minutes <= 45) {
    const c = appr[0];
    $("#threat-title").textContent = (c.jump_detected ? `Orage sévère #${c.cell_id}` : `Cellule #${c.cell_id}`) + " en approche";
    $("#threat-sub").innerHTML = `Foudre dans l'anneau dans <b>${Math.round(c.eta_strike_minutes)} min</b> · proba ${Math.round((c.strike_probability ?? 0) * 100)}%`;
    banner.classList.add("show");
  } else if (jump) {
    $("#threat-title").textContent = `Orage sévère #${jump.cell_id} détecté`;
    $("#threat-sub").innerHTML = "Intensification rapide (lightning jump)";
    banner.classList.add("show");
  } else {
    banner.classList.remove("show");
  }
}

function renderCells(cells) {
  state.cellLayer.clearLayers();
  cellRefs.clear();
  setText("#cells-count", cells.length);
  const list = $("#cells-list");
  if (!cells.length) {
    list.innerHTML = '<div class="empty">Aucune cellule détectée</div>';
    updateThreatBanner([]);
    return;
  }
  const maxStrikes = Math.max(1, ...cells.map((c) => c.strikes_count));
  const score = (c) => 0.4 * (c.confidence ?? 0) + 0.3 * (c.strikes_count / maxStrikes) + 0.3 * ((c.severity ?? 0) / 5);
  cells.sort((a, b) => score(b) - score(a));
  list.innerHTML = "";

  cells.forEach((c) => {
    const isGhost = (c.misses ?? 0) > 0;
    const sevCol = severityColor(c.severity ?? 0);
    const baseWeight = c.jump_detected ? 3 : 1.5;

    const circle = L.circle([c.centroid.lat, c.centroid.lon], {
      radius: Math.max(c.radius_km, 2) * 1000,
      color: sevCol, weight: baseWeight,
      fillColor: sevCol, fillOpacity: isGhost ? 0.04 : (c.jump_detected ? 0.18 : 0.09),
      dashArray: isGhost ? "4,5" : null,
    });
    circle.bindPopup(cellPopupHtml(c));
    circle.on("click", () => selectCell(c.cell_id));
    circle.on("popupopen", () => fillGeoName(circle, c.centroid.lat, c.centroid.lon));
    circle.addTo(state.cellLayer);

    // trail passé
    if (Array.isArray(c.track) && c.track.length >= 2) {
      L.polyline(c.track, { color: sevCol, weight: 2, opacity: 0.5, dashArray: isGhost ? "2,5" : null }).addTo(state.cellLayer);
    }

    // projection future + jalons + cône
    if (c.velocity_kmh && c.heading_deg != null) {
      const pf = c.motion_provisional ? 0.5 : 1;   // projection atténuée si ETA provisoire
      const rad = (c.heading_deg * Math.PI) / 180;
      const cosLat = Math.cos((c.centroid.lat * Math.PI) / 180);
      const at = (min) => {
        const km = (c.velocity_kmh / 60) * min;
        return [c.centroid.lat + (km / 111) * Math.cos(rad), c.centroid.lon + (km / (111 * cosLat)) * Math.sin(rad)];
      };
      const projMin = 30;
      const lenKm = (c.velocity_kmh / 60) * projMin;
      const tip = at(projMin);

      // Enveloppe de menace : corridor balayé par le corps de la cellule (rayon),
      // élargi selon la tendance du rayon, + cercle projeté à T+30.
      const rkm = Math.max(c.radius_km, 3);
      const grow = c.radius_trend === "growing" ? 1.4 : (c.radius_trend === "declining" ? 0.8 : 1.0);
      const perp = rad + Math.PI / 2;
      const ex = (rkm / 111) * Math.cos(perp), ey = (rkm / (111 * cosLat)) * Math.sin(perp);
      L.polygon([
        [c.centroid.lat + ex, c.centroid.lon + ey],
        [tip[0] + ex * grow, tip[1] + ey * grow],
        [tip[0] - ex * grow, tip[1] - ey * grow],
        [c.centroid.lat - ex, c.centroid.lon - ey],
      ], { color: sevCol, weight: 0, fillColor: sevCol, fillOpacity: 0.08 * pf }).addTo(state.cellLayer);
      L.circle(tip, { radius: rkm * grow * 1000, color: sevCol, weight: 1, fill: false, dashArray: "3,5", opacity: 0.45 * pf }).addTo(state.cellLayer);

      L.polyline([[c.centroid.lat, c.centroid.lon], tip], { color: "#f85149", weight: 2, opacity: pf, dashArray: c.motion_provisional ? "2,6" : "6,5" }).addTo(state.cellLayer);
      [10, 20, 30].forEach((m) => {
        L.circleMarker(at(m), { radius: 3, color: "#f85149", fillColor: "#0a0d13", fillOpacity: 1, weight: 1.5 })
          .bindTooltip(`+${m} min`, { direction: "top" }).addTo(state.cellLayer);
      });
      if (c.eta_uncertainty_min) {
        const widthKm = Math.min(c.eta_uncertainty_min * c.velocity_kmh / 60, lenKm / 2);
        const perpRad = rad + Math.PI / 2;
        const px = (widthKm / 111) * Math.cos(perpRad);
        const py = (widthKm / (111 * cosLat)) * Math.sin(perpRad);
        L.polygon([[c.centroid.lat, c.centroid.lon], [tip[0] + px, tip[1] + py], [tip[0] - px, tip[1] - py]],
          { color: "#f85149", weight: 0, fillColor: "#f85149", fillOpacity: 0.13 }).addTo(state.cellLayer);
      }
    }

    const rules = state.alertRules;
    if (c.jump_detected && rules.jump && !notifiedJump.has(c.cell_id)) {
      notifiedJump.add(c.cell_id);
      notify("⚠ Orage sévère", `Cellule #${c.cell_id} : intensification rapide (jump)`);
    }
    if (rules.approach && !c.motion_provisional && c.eta_strike_minutes != null && c.eta_strike_minutes <= rules.etaMin && !notifiedApproach.has(c.cell_id)) {
      notifiedApproach.add(c.cell_id);
      notify("🌩 Cellule en approche", `#${c.cell_id} : foudre dans l'anneau dans ~${Math.round(c.eta_strike_minutes)} min`);
    }

    const card = buildCellCard(c, sevCol);
    card.addEventListener("click", () => focusCell(c.cell_id));
    list.appendChild(card);
    drawSpark(card.querySelector(".spark"), c.spark, sevCol);
    cellRefs.set(c.cell_id, { circle, card, data: c, baseWeight });
  });

  updateThreatBanner(cells);
  for (const id of [...notifiedJump]) if (!cellRefs.has(id)) notifiedJump.delete(id);
  for (const id of [...notifiedApproach]) if (!cellRefs.has(id)) notifiedApproach.delete(id);
}

function focusCell(id) {
  const ref = cellRefs.get(id);
  if (!ref) return;
  const liveTab = document.querySelector('.tab[data-tab="live"]');
  if (liveTab && !liveTab.classList.contains("active")) liveTab.click();
  setTimeout(() => {
    mapLive.invalidateSize();
    const c = ref.data;
    mapLive.flyTo([c.centroid.lat, c.centroid.lon], Math.max(mapLive.getZoom(), 8), { duration: 0.7 });
    selectCell(id);
    setTimeout(() => { try { ref.circle.openPopup(); } catch (e) { /* */ } }, 750);
  }, 80);
}

// ── Alerte sonore + notifications ────────────────────────────────────────────
$("#alert-sound").checked = state.alertEnabled;
$("#alert-sound").addEventListener("change", (e) => {
  state.alertEnabled = e.target.checked;
  localStorage.setItem("alert-sound", state.alertEnabled ? "1" : "0");
  if (state.alertEnabled) { ensureAudio(); ensureNotifyPermission(); }
});

const tzBtn = $("#tz-toggle");
function refreshTzBtn() { if (tzBtn) tzBtn.textContent = state.useLocal ? "Local" : "UTC"; }
refreshTzBtn();
if (tzBtn) tzBtn.addEventListener("click", () => {
  state.useLocal = !state.useLocal;
  localStorage.setItem("tz-local", state.useLocal ? "1" : "0");
  refreshTzBtn();
});

// ── Thème clair / sombre ──────────────────────────────────────────────────────
// L'init (anti-flash) est faite par le script inline du <head> ; ici on persiste
// et on bascule au clic. Sombre par défaut si rien n'est enregistré.
const themeBtn = $("#theme-toggle");
if (themeBtn) themeBtn.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("sc-theme", next); } catch { /* */ }
});

let _audioCtx = null;
function ensureAudio() {
  if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  if (_audioCtx.state === "suspended") _audioCtx.resume();
  return _audioCtx;
}
function playBeep() {
  try {
    const ctx = ensureAudio();
    const t0 = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(880, t0);
    osc.frequency.setValueAtTime(660, t0 + 0.12);
    gain.gain.setValueAtTime(0.0001, t0);
    gain.gain.exponentialRampToValueAtTime(0.4, t0 + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.35);
    osc.connect(gain).connect(ctx.destination);
    osc.start(t0); osc.stop(t0 + 0.36);
  } catch (e) { /* audio indisponible */ }
}
function ensureNotifyPermission() {
  if ("Notification" in window && Notification.permission === "default") Notification.requestPermission().catch(() => {});
}
let _lastNotify = 0;
function notify(title, body) {
  if (!state.alertEnabled) return;
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  const now = Date.now();
  if (now - _lastNotify < 15000) return;
  _lastNotify = now;
  try { new Notification(title, { body }); } catch { /* ignore */ }
}
function maybeAlert(s) {
  if (!state.alertEnabled || !state.config) return;
  if (state.alertRules.sound && s.distance_km <= state.config.alert_distance_km) {
    playBeep();
    notify("⚡ Foudre proche", `${s.distance_km.toFixed(1)} km de chez vous`);
  }
}

// ── Stats ───────────────────────────────────────────────────────────────────
async function fetchHomeName(lat, lon) {
  try {
    const g = await fetch(`/api/geocode?lat=${lat}&lon=${lon}`).then((r) => r.json());
    if (g.name) $("#home-name").textContent = g.name;
  } catch { /* ignore */ }
}

function updateStats(s) {
  if (s.home) {
    state.config = s;
    if (!state.homeMarker) {
      state.homeMarker = L.marker([s.home.lat, s.home.lon], { title: "HOME" }).addTo(mapLive);
      state.radiusCircle = L.circle([s.home.lat, s.home.lon], {
        radius: s.max_distance_km * 1000, color: "#5aa2ff", weight: 1, fillOpacity: 0.02, dashArray: "4,7",
      }).addTo(mapLive);
      state.alertCircle = L.circle([s.home.lat, s.home.lon], {
        radius: (s.alert_distance_km || 15) * 1000, color: "#ec8a2c", weight: 1.2, fill: false, dashArray: "2,5",
      }).addTo(mapLive);
      mapLive.setView([s.home.lat, s.home.lon], 8);
      fetchHomeName(s.home.lat, s.home.lon);
    }
  }
  setText("#st-world", s.total_world ?? 0);
  setText("#st-nearby", s.nearby ?? 0);
  const closest = $("#st-closest");
  if (closest) closest.innerHTML = (s.closest_km != null ? s.closest_km.toFixed(1) : "—") + ' <small>km</small>';
  setText("#st-logged", "+" + (s.logged_session ?? 0));

  // Délai du flux (médiane) — vert ≤ 12 s, orange ≤ 30 s, rouge au-delà.
  const lb = $("#lat-badge");
  if (lb) {
    if (s.latency_s != null) {
      lb.textContent = "⏱ " + (s.latency_s < 60 ? `${s.latency_s.toFixed(0)} s` : `${(s.latency_s / 60).toFixed(1)} min`);
      lb.style.color = s.latency_s <= 12 ? "var(--sev0)" : s.latency_s <= 30 ? "var(--sev2)" : "var(--sev5)";
      lb.title = `Délai du flux (médiane) · source ${s.source ?? "?"}` + (s.delay_s != null ? ` · délai réseau ${s.delay_s} s` : "");
    } else {
      lb.textContent = "—"; lb.style.color = "";
    }
  }

  // Diagnostics système (débit, tampon, file de diffusion, coût du recalcul).
  if (s.cells_compute_ms != null) setText("#sys-calc", `${s.cells_compute_ms} ms`);
  setText("#sys-wps", s.world_per_s != null ? s.world_per_s.toFixed(0) : "—");
  setText("#sys-npm", s.nearby_per_min != null ? s.nearby_per_min.toFixed(0) : "—");
  if (s.recent_buffer != null && s.recent_buffer_max) {
    const buf = $("#sys-buf");
    if (buf) {
      const pct = (s.recent_buffer / s.recent_buffer_max) * 100;
      buf.textContent = s.recent_buffer;
      buf.style.color = pct > 90 ? "var(--sev5)" : pct > 60 ? "var(--sev2)" : "";
      buf.title = `${s.recent_buffer} / ${s.recent_buffer_max} impacts (${pct.toFixed(0)} %)`;
    }
  }
  const q = $("#sys-queue");
  if (q && s.queue_depth != null) {
    const dropped = s.queue_dropped || 0;
    q.textContent = dropped ? `${s.queue_depth}·⚠${dropped}` : `${s.queue_depth}/${s.queue_max ?? "?"}`;
    q.style.color = dropped ? "var(--sev5)" : "";
  }

  const dot = $("#mqtt-dot"), text = $("#mqtt-text");
  if (s.last_message_at) {
    const age = Date.now() / 1000 - s.last_message_at;
    let cls = "dot-green", lbl = "flux actif";
    if (age > 60) { cls = "dot-red"; lbl = `silence ${age.toFixed(0)} s`; }
    else if (age > 10) { cls = "dot-orange"; lbl = `${age.toFixed(0)} s`; }
    dot.className = "dot " + cls; text.textContent = lbl;
  } else {
    dot.className = "dot dot-red";
    text.textContent = s.mqtt_connected ? "en attente" : "hors-ligne";
  }
}

function updateRate() {
  const cutoff = Date.now() / 1000 - 60;
  const n = state.recentStrikes.reduce((a, s) => a + (s.ts_unix > cutoff ? 1 : 0), 0);
  const el = $("#st-rate");
  if (el) el.innerHTML = `${n} <small>/min</small>`;
}

function tickUptime() {
  const elapsed = Math.floor((Date.now() - state.uptimeStart) / 1000);
  const h = String(Math.floor(elapsed / 3600)).padStart(2, "0");
  const m = String(Math.floor((elapsed % 3600) / 60)).padStart(2, "0");
  const s = String(elapsed % 60).padStart(2, "0");
  const el = $("#run-clock");
  if (el) el.textContent = `${h}:${m}:${s}`;
  updateRate();
}
setInterval(tickUptime, 1000);

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === "strike") addStrike(msg.data);
      else if (msg.type === "strikes_batch") addStrikesBatch(msg.data);
      else if (msg.type === "cells") renderCells(msg.data);
      else if (msg.type === "stats") updateStats(msg.data);
    } catch (err) { console.error(err); }
  };
  ws.onclose = () => setTimeout(connectWS, 2000);
  ws.onerror = () => ws.close();
  setInterval(() => { if (ws.readyState === 1) ws.send("ping"); }, 25_000);
}
async function pollStats() {
  try { const r = await fetch("/api/stats"); if (r.ok) updateStats(await r.json()); } catch { /* */ }
}
setInterval(pollStats, 3000);
pollStats();
connectWS();

// ── HOME (clic sur la pastille puis sur la carte) ────────────────────────────
let homePickMode = false;
const homeName = $("#home-name");
if (homeName) homeName.addEventListener("click", () => {
  homePickMode = !homePickMode;
  mapLive.getContainer().style.cursor = homePickMode ? "crosshair" : "";
  homeName.style.color = homePickMode ? "var(--brand-hi)" : "";
});
mapLive.on("click", async (e) => {
  if (!homePickMode) return;
  homePickMode = false;
  mapLive.getContainer().style.cursor = "";
  if (homeName) homeName.style.color = "";
  const lat = e.latlng.lat, lon = e.latlng.lng;
  try {
    const r = await fetch("/api/home", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lat, lon }) });
    if (!r.ok) return;
    if (state.homeMarker) state.homeMarker.setLatLng([lat, lon]);
    if (state.radiusCircle) state.radiusCircle.setLatLng([lat, lon]);
    if (state.config) state.config.home = { lat, lon };
    fetchHomeName(lat, lon);
  } catch (err) { console.error("set-home", err); }
});

// ── Historique ──────────────────────────────────────────────────────────────
function fmtDateInput(unix) {
  const d = new Date(unix * 1000);
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}
async function initHistDefaults() {
  try {
    const j = await fetch("/api/strikes/history?limit=1").then((r) => r.json());
    if (j.bounds && j.bounds.min_unix) {
      $("#hist-from").value = fmtDateInput(j.bounds.min_unix);
      $("#hist-to").value = fmtDateInput(j.bounds.max_unix);
    }
  } catch { /* */ }
}
initHistDefaults();

let histStrikes = [];
let histAggregated = false;
async function loadHistory() {
  const params = new URLSearchParams();
  if ($("#hist-from").value) params.set("from", new Date($("#hist-from").value).toISOString());
  if ($("#hist-to").value) params.set("to", new Date($("#hist-to").value).toISOString());
  if ($("#hist-dist").value) params.set("max_distance", $("#hist-dist").value);
  if (+$("#hist-mds").value > 0) params.set("min_mds", $("#hist-mds").value);
  const j = await fetch("/api/strikes/history?" + params).then((r) => r.json());
  histStrikes = j.strikes;
  histAggregated = !!j.aggregated;
  // En mode agrégé, le serveur a regroupé sur une grille : on affiche le total réel.
  $("#hist-count").textContent = histAggregated
    ? `${(j.total ?? 0).toLocaleString("fr-FR")} impacts · ${histStrikes.length} cellules (~${Math.round((j.grid_deg || 0) * 111)} km)`
    : `${histStrikes.length} impacts`;
  renderHistory();
  renderPerHourChart();
}
function renderHistory() {
  if (histHeat) mapHist.removeLayer(histHeat);
  if (histMarkers) mapHist.removeLayer(histMarkers);
  if (!histStrikes.length) return;
  // Heatmap : intensité fixe en brut, pondérée par le nombre d'impacts en agrégé.
  const maxN = histAggregated ? Math.max(1, ...histStrikes.map((s) => s.n || 1)) : 1;
  const pts = histStrikes.map((s) => [
    s.lat, s.lon,
    histAggregated ? Math.max(0.15, Math.sqrt((s.n || 1) / maxN)) : 0.5,
  ]);
  histHeat = L.heatLayer(pts, { radius: 18, blur: 22, maxZoom: 11 }).addTo(mapHist);
  mapHist.fitBounds(L.latLngBounds(pts.map((p) => [p[0], p[1]])));
}
async function renderPerHourChart() {
  const j = await fetch("/api/history/per_hour?days=30").then((r) => r.json());
  const counts = (j.per_hour || []).map((p) => p.n);
  drawBars("hist-chart", counts, { color: "#5aa2ff" });
  $("#hist-chart-sub").textContent = counts.length ? `max ${Math.max(...counts)}/h · 30 j` : "(aucune donnée)";
}
$("#hist-load").addEventListener("click", loadHistory);
$("#hist-replay").addEventListener("click", () => replayHistory());
function replayHistory() {
  if (!histStrikes.length) { return; }
  if (histMarkers) mapHist.removeLayer(histMarkers);
  if (histHeat) mapHist.removeLayer(histHeat);
  const replayLayer = L.layerGroup().addTo(mapHist);
  const t0 = histStrikes[0].ts_unix;
  const speed = 60;
  histStrikes.forEach((s) => {
    const delay = ((s.ts_unix - t0) * 1000) / speed;
    setTimeout(() => {
      const m = L.circleMarker([s.lat, s.lon], { radius: 4, color: colorForDistance(s.distance_km), weight: 1, fillOpacity: 0.8 }).addTo(replayLayer);
      setTimeout(() => replayLayer.removeLayer(m), 6000);
    }, delay);
  });
}

// ── Analyse ───────────────────────────────────────────────────────────────────
let catalogTrack = null;
async function loadAnalyse() {
  const days = Math.max(1, +$("#an-days").value || 30);
  const fromIso = new Date(Date.now() - days * 86400000).toISOString();
  $("#dl-csv").href = `/api/export/strikes.csv?from=${encodeURIComponent(fromIso)}`;
  $("#dl-geojson").href = `/api/export/strikes.geojson?from=${encodeURIComponent(fromIso)}`;
  $("#dl-tracks").href = `/api/export/cell_tracks.geojson`;
  try {
    const [ver, an, cat] = await Promise.all([
      fetch(`/api/verification?days=${days}`).then((r) => r.json()),
      fetch(`/api/analytics/summary?days=${days}`).then((r) => r.json()),
      fetch(`/api/cells/catalog?days=${days}`).then((r) => r.json()),
    ]);
    renderSkill(ver);
    renderPredTable(ver.recent);
    drawBars("ch-hour", an.hour_of_day, { color: "#5aa2ff", labels: an.hour_of_day.map((_, i) => i), everyLabel: 3 });
    drawBars("ch-week", an.weekday, { color: "#3fb950", labels: ["Dim", "Lun", "Mar", "Mer", "Jeu", "Ven", "Sam"], everyLabel: 1 });
    drawBars("ch-dist", an.distance_hist.map((d) => d.n), { color: "#d6a01f" });
    drawRose("ch-rose", an.rose);
    renderCatalog(cat.cells);
  } catch (e) { console.error("loadAnalyse", e); }
}

function renderSkill(rep) {
  $("#skill-sub").textContent = `${rep.days} j · anneau ${rep.ring_km} km`;
  const pct = (v) => (v == null ? "—" : Math.round(v * 100) + "%");
  const card = (val, key, sub, cls = "") =>
    `<div class="skill-card"><div class="sk-val ${cls}">${val}</div><div class="sk-key">${key}</div><div class="sk-sub">${sub}</div></div>`;
  $("#skill-cards").innerHTML = [
    card(pct(rep.pod), "POD", "détection", rep.pod != null && rep.pod >= 0.6 ? "good" : ""),
    card(pct(rep.far), "FAR", "fausses alertes", rep.far != null && rep.far >= 0.4 ? "warn" : ""),
    card(pct(rep.csi), "CSI", "score critique"),
    card(rep.total_predictions, "Prédictions", "émises"),
    card(rep.total_arrivals, "Arrivées", `foudre ≤ ${rep.ring_km} km`),
    card(rep.mean_eta_error_min != null ? `${rep.mean_eta_error_min}′` : "—", "Erreur ETA", "moyenne"),
    card(rep.mean_lead_min != null ? `${rep.mean_lead_min}′` : "—", "Préavis", "moyen"),
  ].join("");
}

function renderPredTable(recent) {
  const tb = $("#pred-table tbody");
  if (!recent || !recent.length) {
    tb.innerHTML = '<tr><td colspan="7" class="muted" style="text-align:center;padding:18px">Aucune prédiction sur la période</td></tr>';
    return;
  }
  tb.innerHTML = recent.map((p) => {
    const pill = p.outcome === "hit"
      ? '<span class="pill-res pill-hit">touché</span>'
      : '<span class="pill-res pill-fa">fausse alerte</span>';
    const err = p.eta_error_min != null ? `${p.eta_error_min > 0 ? "+" : ""}${p.eta_error_min.toFixed(1)}′` : "—";
    const lead = p.lead_min != null ? `${p.lead_min.toFixed(0)}′` : "—";
    const eta = p.eta_strike_min != null ? `${p.eta_strike_min.toFixed(0)}′` : "?";
    return `<tr><td>${fmtClock(p.ts_made)}</td><td>#${p.cell_id}</td><td>${eta}</td><td>${Math.round((p.probability || 0) * 100)}%</td><td>${pill}</td><td>${err}</td><td>${lead}</td></tr>`;
  }).join("");
}

function drawBars(canvasId, values, opts = {}) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const W = canvas.width = canvas.clientWidth || 300;
  const H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  if (!values || !values.length || Math.max(...values) === 0) {
    ctx.fillStyle = "#626d7e"; ctx.font = "11px 'Geist Mono', monospace"; ctx.fillText("(aucune donnée)", 10, 22); return;
  }
  const max = Math.max(...values);
  const bw = W / values.length;
  ctx.fillStyle = opts.color || "#5aa2ff";
  values.forEach((v, i) => {
    const h = (v / max) * (H - 18);
    ctx.fillRect(i * bw + 1, H - h - 13, Math.max(1, bw - 2), h);
  });
  if (opts.labels) {
    ctx.fillStyle = "#626d7e"; ctx.font = "9px 'Geist Mono', monospace";
    opts.labels.forEach((lb, i) => {
      if (opts.everyLabel && i % opts.everyLabel !== 0) return;
      ctx.fillText(lb, i * bw + 1, H - 2);
    });
  }
}

function drawRose(containerId, rose) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!rose || !rose.length) { el.innerHTML = '<span class="muted">(aucune donnée)</span>'; return; }
  const cx = 90, cy = 90, R = 76;
  const max = Math.max(1, ...rose.map((d) => d.n));
  let paths = "";
  rose.forEach((d, i) => {
    const a0 = ((i * 22.5 - 11.25) - 90) * Math.PI / 180;
    const a1 = ((i * 22.5 + 11.25) - 90) * Math.PI / 180;
    const r = (d.n / max) * R;
    if (r < 0.5) return;
    const x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0);
    const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
    paths += `<path d="M${cx},${cy} L${x0.toFixed(1)},${y0.toFixed(1)} A${r.toFixed(1)},${r.toFixed(1)} 0 0,1 ${x1.toFixed(1)},${y1.toFixed(1)} Z" fill="#5aa2ff" opacity="0.7"/>`;
  });
  const ring = `<circle cx="${cx}" cy="${cy}" r="${R}" fill="none" stroke="#232c3a"/><circle cx="${cx}" cy="${cy}" r="${R / 2}" fill="none" stroke="#1a212c"/>`;
  const lbls = `<text x="${cx}" y="14" text-anchor="middle" class="rose-lbl">N</text>` +
    `<text x="178" y="${cy + 4}" text-anchor="middle" class="rose-lbl">E</text>` +
    `<text x="${cx}" y="180" text-anchor="middle" class="rose-lbl">S</text>` +
    `<text x="6" y="${cy + 4}" text-anchor="middle" class="rose-lbl">O</text>`;
  el.innerHTML = `<svg viewBox="0 0 180 186" width="180" height="186">${ring}${paths}${lbls}</svg>`;
}

function fmtDuration(min) {
  if (min < 60) return `${Math.round(min)} min`;
  return `${(min / 60).toFixed(1)} h`;
}
function renderCatalog(cells) {
  const tb = $("#catalog-table tbody");
  if (!cells || !cells.length) {
    tb.innerHTML = '<tr><td colspan="7" class="muted" style="text-align:center;padding:18px">Aucune cellule persistée sur la période</td></tr>';
    return;
  }
  tb.innerHTML = "";
  cells.forEach((c) => {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    const dur = (c.last_seen - c.first_seen) / 60;
    tr.innerHTML =
      `<td>#${c.cell_id}</td><td>${fmtDateTime(c.first_seen)}</td><td>${fmtDuration(dur)}</td>` +
      `<td>${c.total_strikes ?? "?"}</td><td>${(c.max_severity ?? 0).toFixed(1)}</td>` +
      `<td>${(c.peak_flash_rate ?? 0).toFixed(0)}</td><td>—</td>`;
    tr.addEventListener("click", () => {
      document.querySelectorAll("#catalog-table tr.sel").forEach((r) => r.classList.remove("sel"));
      tr.classList.add("sel");
      showCellTrack(c.run_id, c.cell_id);
    });
    tb.appendChild(tr);
  });
}
async function showCellTrack(run_id, cell_id) {
  try {
    const j = await fetch(`/api/cells/track?run_id=${encodeURIComponent(run_id)}&cell_id=${cell_id}`).then((r) => r.json());
    if (!j.track || !j.track.length) return;
    document.querySelector('.tab[data-tab="history"]').click();
    setTimeout(() => {
      mapHist.invalidateSize();
      if (catalogTrack) mapHist.removeLayer(catalogTrack);
      catalogTrack = L.layerGroup().addTo(mapHist);
      const pts = j.track.map((t) => [t.lat, t.lon]);
      L.polyline(pts, { color: "#f85149", weight: 3, opacity: 0.85 }).addTo(catalogTrack);
      j.track.forEach((t) => {
        L.circleMarker([t.lat, t.lon], { radius: 4, color: severityColor(t.severity || 0), weight: 1, fillOpacity: 0.85 })
          .bindPopup(`<b>Cellule #${cell_id}</b><br>${fmtDateTime(t.ts_unix)}<br>${t.strikes_count} impacts · sév ${(t.severity || 0).toFixed(1)}` +
            (t.velocity_kmh ? `<br>${t.velocity_kmh.toFixed(0)} km/h` : ""))
          .addTo(catalogTrack);
      });
      if (pts.length === 1) mapHist.setView(pts[0], 9);
      else mapHist.fitBounds(L.latLngBounds(pts), { maxZoom: 10, padding: [30, 30] });
    }, 90);
  } catch (e) { console.error("showCellTrack", e); }
}
$("#an-load").addEventListener("click", loadAnalyse);

// ── Radar de précipitations (RainViewer) ─────────────────────────────────────
const RADAR_PLAY = "M8 5v14l11-7z";
const RADAR_PAUSE = "M6 5h4v14H6zM14 5h4v14h-4z";
function setRadarIcon(playing) {
  const ic = $("#radar-play-ic");
  if (ic) ic.querySelector("path").setAttribute("d", playing ? RADAR_PAUSE : RADAR_PLAY);
}
const radar = { host: "", frames: [], idx: 0, layer: null, timer: null };
async function radarInit() {
  try {
    const j = await fetch("https://api.rainviewer.com/public/weather-maps.json").then((r) => r.json());
    radar.host = j.host;
    radar.frames = [...(j.radar?.past || []), ...(j.radar?.nowcast || [])];
    if (!radar.frames.length) return false;
    radar.idx = Math.max(0, (j.radar?.past?.length || 1) - 1);
    $("#radar-slider").max = radar.frames.length - 1;
    $("#radar-slider").value = radar.idx;
    return true;
  } catch (e) { console.warn("radar indisponible", e); return false; }
}
function radarShow(i) {
  if (!radar.frames.length) return;
  radar.idx = Math.max(0, Math.min(i, radar.frames.length - 1));
  const f = radar.frames[radar.idx];
  // RainViewer ne sert le radar que jusqu'au zoom 7 → maxNativeZoom évite « Zoom Level Not Supported ».
  const layer = L.tileLayer(`${radar.host}${f.path}/256/{z}/{x}/{y}/2/1_1.png`, { opacity: 0.6, zIndex: 250, maxNativeZoom: 7, maxZoom: 18 });
  layer.addTo(mapLive);
  if (radar.layer) { const old = radar.layer; setTimeout(() => mapLive.removeLayer(old), 150); }
  radar.layer = layer;
  $("#radar-slider").value = radar.idx;
  const d = new Date(f.time * 1000);
  $("#radar-time").textContent = state.useLocal ? d.toTimeString().slice(0, 5) : d.toISOString().substr(11, 5) + "Z";
}
function radarPlay() {
  if (radar.timer || !radar.frames.length) return;
  radar.timer = setInterval(() => radarShow((radar.idx + 1) % radar.frames.length), 650);
  setRadarIcon(true);
}
function radarStop() { clearInterval(radar.timer); radar.timer = null; setRadarIcon(false); }
function radarOff() {
  radarStop();
  if (radar.layer) { mapLive.removeLayer(radar.layer); radar.layer = null; }
  $("#radar-time").textContent = "—";
}
$("#radar-on").addEventListener("change", async (e) => {
  if (e.target.checked) {
    if (!radar.frames.length) {
      const ok = await radarInit();
      if (!ok) { e.target.checked = false; $("#radar-time").textContent = "indispo"; return; }
    }
    radarShow(radar.idx);
    radarPlay();
  } else {
    radarOff();
  }
});
$("#radar-play").addEventListener("click", () => { if (radar.timer) radarStop(); else radarPlay(); });
$("#radar-slider").addEventListener("input", (e) => { radarStop(); radarShow(+e.target.value); });
