// ─── Blitzortung dashboard — Leaflet + WebSocket ────────────────────────────
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
  cells: new Map(),
  uptimeStart: Date.now(),
  alertEnabled: localStorage.getItem("alert-sound") === "1",
};

// ── Tabs ────────────────────────────────────────────────────────────────────
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.toggle("active", x === t));
  const tab = t.dataset.tab;
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
  if (tab === "history") {
    setTimeout(() => mapHist.invalidateSize(), 60);
  } else {
    setTimeout(() => mapLive.invalidateSize(), 60);
  }
}));

// ── Maps ────────────────────────────────────────────────────────────────────
const TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_OPTS = { attribution: "© OpenStreetMap", maxZoom: 18 };

const mapLive = L.map("map-live", { zoomControl: true }).setView([44.24, 4.72], 8);
L.tileLayer(TILE_URL, TILE_OPTS).addTo(mapLive);
state.strikeLayer = L.layerGroup().addTo(mapLive);
state.cellLayer = L.layerGroup().addTo(mapLive);

const mapHist = L.map("map-hist", { zoomControl: true }).setView([44.24, 4.72], 7);
L.tileLayer(TILE_URL, TILE_OPTS).addTo(mapHist);
let histHeat = null;
let histMarkers = null;

// ── Couleurs ────────────────────────────────────────────────────────────────
function colorForDistance(d) {
  if (d < 15) return "#f85149";
  if (d < 30) return "#ea8c2c";
  if (d < 250) return "#d29922";
  return "#2ea043";
}
function colorClass(d) {
  if (d < 15) return "color-red";
  if (d < 30) return "color-orange";
  if (d < 250) return "color-yellow";
  return "color-green";
}

// ── Affichage live ──────────────────────────────────────────────────────────
function strikeIcon(distance, ageMin) {
  const opacity = Math.max(0.15, 1 - ageMin / 30);
  const size = distance < 15 ? 12 : 8;
  return L.divIcon({
    className: "",
    html: `<div class="strike-marker" style="width:${size}px;height:${size}px;background:${colorForDistance(distance)};opacity:${opacity}"></div>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  });
}

function addStrike(s) {
  state.recentStrikes.push(s);
  if (state.recentStrikes.length > 2000) state.recentStrikes.shift();
  const m = L.marker([s.lat, s.lon], { icon: strikeIcon(s.distance_km, 0) });
  m.bindPopup(
    `<b>${new Date(s.ts_unix * 1000).toUTCString()}</b><br>` +
    `${s.distance_km.toFixed(1)} km, azimut ${s.bearing_deg.toFixed(0)}°<br>` +
    `${s.mds ?? "?"} stations`
  );
  m.addTo(state.strikeLayer);
  m._ts = s.ts_unix;
  prependStrikeRow(s);
  maybeAlert(s);
}

function prependStrikeRow(s) {
  const tbody = $("#strikes-table tbody");
  const tr = document.createElement("tr");
  const t = new Date(s.ts_unix * 1000).toISOString().substr(11, 8);
  const delay = (s.distance_km * 1000) / 340;
  const delayStr = delay < 120 ? `${delay.toFixed(0)} s` : `${(delay / 60).toFixed(1)} min`;
  tr.innerHTML = `
    <td>${t}</td>
    <td class="${colorClass(s.distance_km)}">${s.distance_km.toFixed(1)}</td>
    <td>${cardinal(s.bearing_deg)}</td>
    <td>${delayStr}</td>
    <td>${s.mds ?? "?"}</td>`;
  tbody.insertBefore(tr, tbody.firstChild);
  while (tbody.children.length > 30) tbody.removeChild(tbody.lastChild);
}

function cardinal(deg) {
  const dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSO","SO","OSO","O","ONO","NO","NNO"];
  return dirs[Math.floor((deg + 11.25) / 22.5) % 16];
}

function fadeOldMarkers() {
  const now = Date.now() / 1000;
  state.strikeLayer.eachLayer((m) => {
    const age = now - (m._ts || now);
    if (age > 30 * 60) {
      state.strikeLayer.removeLayer(m);
    } else {
      m.setIcon(strikeIcon(m.options?.distance_km ?? 50, age / 60));
    }
  });
}
setInterval(fadeOldMarkers, 30_000);

// ── Cellules ────────────────────────────────────────────────────────────────
function trendBadge(t) {
  if (!t) return "";
  if (t === "growing")   return '<span class="badge badge-up">↑</span>';
  if (t === "declining") return '<span class="badge badge-down">↓</span>';
  return '<span class="badge badge-stable">→</span>';
}

// Index des cercles et cartes par cell_id pour le double-binding carte <-> sidebar
const cellRefs = new Map();

function selectCell(id) {
  // Retire l'état sélectionné de tous les éléments
  document.querySelectorAll(".cell-card.selected").forEach((el) => el.classList.remove("selected"));
  cellRefs.forEach((ref) => {
    if (ref.circle) ref.circle.setStyle({ weight: 2 });
  });
  // Applique la sélection
  const ref = cellRefs.get(id);
  if (!ref) return;
  if (ref.card) {
    ref.card.classList.add("selected");
    ref.card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  if (ref.circle) {
    ref.circle.setStyle({ weight: 4 });
    ref.circle.bringToFront();
  }
}

function cellPopupHtml(c) {
  const vel = c.velocity_kmh ? `${c.velocity_kmh.toFixed(0)} km/h cap ${c.heading_deg?.toFixed(0) ?? "?"}°` : "immobile / inconnu";
  const eta = c.eta_minutes != null
    ? `ETA ${c.eta_minutes.toFixed(0)}${c.eta_uncertainty_min ? `±${c.eta_uncertainty_min.toFixed(0)}` : ""} min · ${c.closest_approach_km.toFixed(1)} km`
    : ((c.misses ?? 0) > 0 ? `cellule fantôme (${c.misses}×)` : "ne s'approche pas");
  const trends = `intensité ${c.intensity_trend ?? "?"} · rayon ${c.radius_trend ?? "?"}`;
  return `<b>Cellule #${c.cell_id}</b><br>
    ${c.strikes_count} impacts · rayon ${c.radius_km.toFixed(1)} km<br>
    ${vel}<br>
    confiance ${Math.round((c.confidence ?? 0) * 100)}%<br>
    ${trends}<br>
    <i>${eta}</i><br>
    <small>${c.centroid.lat.toFixed(3)}°, ${c.centroid.lon.toFixed(3)}°</small>`;
}

function renderCells(cells) {
  state.cellLayer.clearLayers();
  cellRefs.clear();
  const list = $("#cells-list");
  if (!cells.length) {
    list.innerHTML = "<em>aucune cellule détectée</em>";
    return;
  }
  // Tri par score composite : confiance + taille (normalisée sur la plus grosse).
  // Pondération 60% confiance / 40% taille → "le plus haut et le plus gros en premier".
  // Les #cell_id restent inchangés (assignés à la détection initiale dans analysis.py).
  const maxStrikes = Math.max(1, ...cells.map((c) => c.strikes_count));
  const score = (c) => 0.6 * (c.confidence ?? 0) + 0.4 * (c.strikes_count / maxStrikes);
  cells.sort((a, b) => score(b) - score(a));
  list.innerHTML = "";
  cells.forEach((c) => {
    const isGhost = (c.misses ?? 0) > 0;
    // Cercle de la cellule
    const circle = L.circle([c.centroid.lat, c.centroid.lon], {
      radius: Math.max(c.radius_km, 2) * 1000,
      color: c.eta_minutes != null ? "#f85149" : "#d29922",
      weight: 2,
      fillOpacity: isGhost ? 0.03 : 0.1,
      dashArray: isGhost ? "4,4" : null,
    });
    circle.bindPopup(cellPopupHtml(c));
    circle.on("click", () => selectCell(c.cell_id));
    circle.addTo(state.cellLayer);

    // Flèche du vecteur déplacement + cône d'incertitude
    if (c.velocity_kmh && c.heading_deg != null) {
      const rad = (c.heading_deg * Math.PI) / 180;
      const projMin = 30;
      const lenKm = Math.min((c.velocity_kmh / 60) * projMin, 80);
      const cosLat = Math.cos((c.centroid.lat * Math.PI) / 180);
      const dLat = (lenKm / 111) * Math.cos(rad);
      const dLon = (lenKm / (111 * cosLat)) * Math.sin(rad);
      const tip = [c.centroid.lat + dLat, c.centroid.lon + dLon];
      L.polyline([[c.centroid.lat, c.centroid.lon], tip],
        { color: "#f85149", weight: 3 }).addTo(state.cellLayer);

      // Cône : marge latérale ≈ uncertainty_min × vitesse
      if (c.eta_uncertainty_min) {
        const widthKm = Math.min(c.eta_uncertainty_min * c.velocity_kmh / 60, lenKm / 2);
        const perpRad = rad + Math.PI / 2;
        const px = (widthKm / 111) * Math.cos(perpRad);
        const py = (widthKm / (111 * cosLat)) * Math.sin(perpRad);
        const cone = [
          [c.centroid.lat, c.centroid.lon],
          [tip[0] + px, tip[1] + py],
          [tip[0] - px, tip[1] - py],
        ];
        L.polygon(cone, {
          color: "#f85149", weight: 0, fillColor: "#f85149", fillOpacity: 0.15,
        }).addTo(state.cellLayer);
      }
    }

    // Carte sidebar
    const card = document.createElement("div");
    card.className = "cell-card"
      + (c.eta_minutes != null ? " approaching" : "")
      + (isGhost ? " ghost" : "");
    const vel = c.velocity_kmh ? `${c.velocity_kmh.toFixed(0)} km/h` : "—";
    const headingStr = c.heading_deg != null ? c.heading_deg.toFixed(0) + "°" : "—";
    const etaStr = c.eta_minutes != null
      ? `ETA ${c.eta_minutes.toFixed(0)}${c.eta_uncertainty_min ? `±${c.eta_uncertainty_min.toFixed(0)}` : ""} min · ${c.closest_approach_km.toFixed(1)} km`
      : (isGhost ? `perdue (${c.misses}×)` : "s'éloigne / inconnue");
    const conf = c.confidence != null ? `${Math.round(c.confidence * 100)}%` : "—";
    card.innerHTML = `
      <div class="row"><b>Cellule #${c.cell_id}</b><span>${c.strikes_count} impacts ${trendBadge(c.intensity_trend)}</span></div>
      <div class="row small"><span>${vel} cap ${headingStr}</span><span>conf. ${conf}</span></div>
      <div class="row small"><span>rayon ${c.radius_km.toFixed(1)} km ${trendBadge(c.radius_trend)}</span><span>${etaStr}</span></div>`;
    card.addEventListener("click", () => focusCell(c.cell_id));
    list.appendChild(card);
    cellRefs.set(c.cell_id, { circle, card, data: c });
  });
}

// Bascule sur l'onglet Live et zoome/anime jusqu'à la cellule, puis ouvre le popup.
function focusCell(id) {
  const ref = cellRefs.get(id);
  if (!ref) return;
  // 1) Force l'onglet Live (sinon la carte est cachée et tu ne vois rien bouger)
  const liveTab = document.querySelector('.tab[data-tab="live"]');
  if (liveTab && !liveTab.classList.contains("active")) liveTab.click();
  // 2) Recalcule la taille de la map (au cas où on viendrait juste de switcher d'onglet)
  setTimeout(() => {
    mapLive.invalidateSize();
    const c = ref.data;
    const targetZoom = Math.max(mapLive.getZoom(), 8);
    mapLive.flyTo([c.centroid.lat, c.centroid.lon], targetZoom, { duration: 0.7 });
    // 3) Sélection visuelle immédiate
    selectCell(id);
    // 4) Popup après la fin de l'animation
    setTimeout(() => { try { ref.circle.openPopup(); } catch (e) {} }, 750);
  }, 80);
}

// ── Alerte sonore ───────────────────────────────────────────────────────────
$("#alert-sound").checked = state.alertEnabled;
$("#alert-sound").addEventListener("change", (e) => {
  state.alertEnabled = e.target.checked;
  localStorage.setItem("alert-sound", state.alertEnabled ? "1" : "0");
});

function maybeAlert(s) {
  if (!state.alertEnabled || !state.config) return;
  if (s.distance_km <= state.config.alert_distance_km) {
    const a = $("#alert-audio");
    a.currentTime = 0;
    a.play().catch(() => {});
  }
}

// ── Stats ───────────────────────────────────────────────────────────────────
function updateStats(s) {
  if (s.home) {
    $("#home-pos").textContent = `${s.home.lat.toFixed(4)}°, ${s.home.lon.toFixed(4)}°`;
    state.config = s;
    if (!state.homeMarker) {
      state.homeMarker = L.marker([s.home.lat, s.home.lon], { title: "HOME" }).addTo(mapLive);
      state.radiusCircle = L.circle([s.home.lat, s.home.lon], {
        radius: s.max_distance_km * 1000,
        color: "#1f6feb",
        weight: 1,
        fillOpacity: 0.03,
      }).addTo(mapLive);
      mapLive.setView([s.home.lat, s.home.lon], 8);
    }
  }
  if (s.max_distance_km) $("#radius").textContent = `${s.max_distance_km} km`;
  $("#total-world").textContent = s.total_world ?? 0;
  $("#nearby").textContent = s.nearby ?? 0;
  $("#closest").textContent = s.closest_km != null ? `${s.closest_km.toFixed(1)} km` : "—";
  $("#logged-session").textContent = s.logged_session ?? 0;
  $("#logged-total").textContent = s.logged_total ?? 0;

  // État MQTT : âge du dernier message
  const dot = $("#mqtt-dot");
  const text = $("#mqtt-text");
  if (s.last_message_at) {
    const age = Date.now() / 1000 - s.last_message_at;
    let cls = "dot-green", lbl;
    if (age > 60) { cls = "dot-red"; lbl = `aucun msg depuis ${age.toFixed(0)} s`; }
    else if (age > 10) { cls = "dot-orange"; lbl = `dernier msg ${age.toFixed(0)} s`; }
    else { lbl = `actif (${age.toFixed(0)} s)`; }
    dot.className = "dot " + cls;
    text.textContent = lbl;
  } else {
    dot.className = "dot dot-red";
    text.textContent = s.mqtt_connected ? "connecté, en attente" : "déconnecté";
  }
}

function tickUptime() {
  const elapsed = Math.floor((Date.now() - state.uptimeStart) / 1000);
  const h = String(Math.floor(elapsed / 3600)).padStart(2, "0");
  const m = String(Math.floor((elapsed % 3600) / 60)).padStart(2, "0");
  const s = String(elapsed % 60).padStart(2, "0");
  $("#uptime").textContent = `${h}:${m}:${s}`;
}
setInterval(tickUptime, 1000);

// ── WebSocket ───────────────────────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === "strike") addStrike(msg.data);
      else if (msg.type === "cells") renderCells(msg.data);
      else if (msg.type === "stats") updateStats(msg.data);
    } catch (err) { console.error(err); }
  };
  ws.onclose = () => setTimeout(connectWS, 2000);
  ws.onerror = () => ws.close();
  // Heartbeat client → serveur
  setInterval(() => { if (ws.readyState === 1) ws.send("ping"); }, 25_000);
}

// Stats polling (fallback + état MQTT live)
async function pollStats() {
  try {
    const r = await fetch("/api/stats");
    if (r.ok) updateStats(await r.json());
  } catch {}
}
setInterval(pollStats, 3000);
pollStats();
connectWS();

// ── Historique ──────────────────────────────────────────────────────────────
function fmtDateInput(unix) {
  const d = new Date(unix * 1000);
  return d.toISOString().slice(0, 16);
}

async function initHistDefaults() {
  try {
    const r = await fetch("/api/strikes/history?limit=1");
    const j = await r.json();
    if (j.bounds && j.bounds.min_unix) {
      $("#hist-from").value = fmtDateInput(j.bounds.min_unix);
      $("#hist-to").value = fmtDateInput(j.bounds.max_unix);
    }
  } catch {}
}
initHistDefaults();

let histStrikes = [];

async function loadHistory() {
  const params = new URLSearchParams();
  if ($("#hist-from").value) params.set("from", $("#hist-from").value);
  if ($("#hist-to").value) params.set("to", $("#hist-to").value);
  if ($("#hist-dist").value) params.set("max_distance", $("#hist-dist").value);
  if ($("#hist-mds").value) params.set("min_mds", $("#hist-mds").value);
  const r = await fetch("/api/strikes/history?" + params);
  const j = await r.json();
  histStrikes = j.strikes;
  $("#hist-count").textContent = `${histStrikes.length} impacts`;
  renderHistory();
  renderPerHourChart();
}

function renderHistory() {
  if (histHeat) mapHist.removeLayer(histHeat);
  if (histMarkers) mapHist.removeLayer(histMarkers);
  if (!histStrikes.length) return;
  const pts = histStrikes.map((s) => [s.lat, s.lon, 0.5]);
  histHeat = L.heatLayer(pts, { radius: 18, blur: 22, maxZoom: 11 }).addTo(mapHist);
  histMarkers = L.layerGroup();
  histStrikes.forEach((s) => {
    L.circleMarker([s.lat, s.lon], {
      radius: 3, color: colorForDistance(s.distance_km), weight: 1, fillOpacity: 0.6
    }).addTo(histMarkers);
  });
  if (mapHist.hasLayer(histHeat)) mapHist.fitBounds(L.latLngBounds(pts.map((p) => [p[0], p[1]])));
}

async function renderPerHourChart() {
  const r = await fetch("/api/history/per_hour?days=30");
  const j = await r.json();
  const canvas = $("#hist-chart");
  const ctx = canvas.getContext("2d");
  const W = canvas.width = canvas.clientWidth;
  const H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  if (!j.per_hour.length) {
    ctx.fillStyle = "#8b95a7"; ctx.fillText("(aucune donnée sur 30 j)", 10, 20); return;
  }
  const counts = j.per_hour.map((p) => p.n);
  const max = Math.max(...counts);
  const bw = Math.max(1, W / counts.length);
  ctx.fillStyle = "#1f6feb";
  counts.forEach((n, i) => {
    const h = (n / max) * (H - 4);
    ctx.fillRect(i * bw, H - h, bw - 1, h);
  });
  ctx.fillStyle = "#8b95a7"; ctx.font = "10px sans-serif";
  ctx.fillText(`max ${max}/h sur 30 j`, 8, 12);
}

$("#hist-load").addEventListener("click", loadHistory);
$("#hist-replay").addEventListener("click", () => replayHistory());

function replayHistory() {
  if (!histStrikes.length) { alert("Charge d'abord les données."); return; }
  if (histMarkers) mapHist.removeLayer(histMarkers);
  if (histHeat) mapHist.removeLayer(histHeat);
  const replayLayer = L.layerGroup().addTo(mapHist);
  const t0 = histStrikes[0].ts_unix;
  const speed = 60;
  histStrikes.forEach((s) => {
    const delay = ((s.ts_unix - t0) * 1000) / speed;
    setTimeout(() => {
      const m = L.circleMarker([s.lat, s.lon], {
        radius: 4, color: colorForDistance(s.distance_km), weight: 1, fillOpacity: 0.8,
      }).addTo(replayLayer);
      setTimeout(() => replayLayer.removeLayer(m), 6000);
    }, delay);
  });
}
