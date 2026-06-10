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
  useLocal: localStorage.getItem("tz-local") !== "0",   // affichage heure locale par défaut
};

// ── Temps : affichage local (défaut) ou UTC, togglable ───────────────────────
function fmtClock(unix) {
  const d = new Date(unix * 1000);
  return state.useLocal ? d.toTimeString().slice(0, 8) : d.toISOString().substr(11, 8);
}
function fmtDateTime(unix) {
  const d = new Date(unix * 1000);
  return state.useLocal ? d.toLocaleString() : d.toUTCString();
}

// ── Tabs ────────────────────────────────────────────────────────────────────
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.toggle("active", x === t));
  const tab = t.dataset.tab;
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
  if (tab === "history") {
    setTimeout(() => mapHist.invalidateSize(), 60);
  } else if (tab === "analyse") {
    loadAnalyse();
  } else {
    setTimeout(() => mapLive.invalidateSize(), 60);
  }
}));

// ── Maps ────────────────────────────────────────────────────────────────────
const TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_OPTS = { attribution: "© OpenStreetMap", maxZoom: 18 };

const mapLive = L.map("map-live", { zoomControl: true, preferCanvas: true }).setView([44.24, 4.72], 8);
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
// Couleur d'une cellule selon son indice de sévérité (0..5).
function severityColor(sev) {
  if (sev >= 4) return "#f85149";    // rouge — sévère
  if (sev >= 2.5) return "#ea8c2c";  // orange
  if (sev >= 1) return "#d29922";    // jaune
  return "#3fb950";                   // vert — faible
}

// ── Affichage live (canvas circleMarker — tient des milliers d'impacts) ──────
// Style d'un impact : taille selon proximité, opacité décroissante avec l'âge.
function strikeStyle(distance, ageMin) {
  const opacity = Math.max(0.15, 1 - ageMin / 30);
  const c = colorForDistance(distance);
  return {
    radius: distance < 15 ? 6 : 4,
    color: c,
    fillColor: c,
    weight: 1,
    opacity,
    fillOpacity: opacity * 0.7,
  };
}

function strikePopupHtml(s) {
  return `<b>${fmtDateTime(s.ts_unix)}</b><br>` +
    `${s.distance_km.toFixed(1)} km, azimut ${s.bearing_deg.toFixed(0)}°<br>` +
    `${s.mds ?? "?"} stations`;
}

// Trace un impact (sans toucher tableau/alerte) — réutilisé par le backfill.
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
  // Flash-to-bang : délai approximatif avant le tonnerre pour un impact proche.
  if (s.distance_km < 60) {
    const sec = (s.distance_km * 1000) / 343;
    const t = $("#thunder");
    if (t) t.textContent = `${s.distance_km.toFixed(1)} km → ~${sec < 90 ? sec.toFixed(0) + " s" : (sec / 60).toFixed(1) + " min"}`;
  }
}

// Backfill : à la (re)connexion WS, on reçoit la fenêtre récente d'un coup.
function addStrikesBatch(arr) {
  state.strikeLayer.clearLayers();
  $("#strikes-table tbody").innerHTML = "";
  state.recentStrikes = [];
  arr.forEach((s) => { state.recentStrikes.push(s); plotStrike(s); });
  if (state.recentStrikes.length > 5000) {
    state.recentStrikes = state.recentStrikes.slice(-5000);
  }
  arr.slice(-30).forEach((s) => prependStrikeRow(s));
}

function prependStrikeRow(s) {
  const tbody = $("#strikes-table tbody");
  const tr = document.createElement("tr");
  const t = fmtClock(s.ts_unix);
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
      m.setStyle(strikeStyle(m._dist ?? 50, age / 60));
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
// Cellules déjà notifiées pour un lightning jump (évite les doublons).
const notifiedJump = new Set();

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
    ? `ETA centroïde ${c.eta_minutes.toFixed(0)}${c.eta_uncertainty_min ? `±${c.eta_uncertainty_min.toFixed(0)}` : ""} min · ${c.closest_approach_km.toFixed(1)} km`
    : ((c.misses ?? 0) > 0 ? `cellule fantôme (${c.misses}×)` : "ne s'approche pas");
  const etaEdge = c.eta_strike_minutes != null
    ? `<br><i>⚡ foudre dans l'anneau dans ~${c.eta_strike_minutes.toFixed(0)} min</i>` : "";
  const prob = c.strike_probability
    ? `<br>proba de coup ${Math.round(c.strike_probability * 100)}%` : "";
  const jump = c.jump_detected
    ? '<br><b style="color:#f85149">⚠ intensification rapide (jump)</b>' : "";
  const lineage = c.parent_id ? ` <small>(split #${c.parent_id})</small>` : "";
  const trends = `intensité ${c.intensity_trend ?? "?"} · rayon ${c.radius_trend ?? "?"}`;
  return `<b>Cellule #${c.cell_id}</b>${lineage}<br>
    ${c.strikes_count} impacts · ${c.flash_rate_per_min != null ? c.flash_rate_per_min.toFixed(0) : "?"}/min · rayon ${c.radius_km.toFixed(1)} km<br>
    sévérité ${(c.severity ?? 0).toFixed(1)}/5 · confiance ${Math.round((c.confidence ?? 0) * 100)}%<br>
    ${vel}<br>
    ${trends}<br>
    <i>${eta}</i>${etaEdge}${prob}${jump}<br>
    <small class="geo-name muted"></small>
    <small>${c.centroid.lat.toFixed(3)}°, ${c.centroid.lon.toFixed(3)}°</small>`;
}

// Géocodage à la demande : remplit "près de X" quand le popup d'une cellule s'ouvre.
async function fillGeoName(layer, lat, lon) {
  try {
    const g = await fetch(`/api/geocode?lat=${lat}&lon=${lon}`).then((r) => r.json());
    if (!g.name) return;
    const el = layer.getPopup()?.getElement()?.querySelector(".geo-name");
    if (el) el.textContent = "📍 près de " + g.name;
  } catch { /* géocodage indisponible */ }
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
  // Tri composite : confiance + taille + sévérité → les cellules dangereuses en haut.
  const score = (c) => 0.4 * (c.confidence ?? 0) + 0.3 * (c.strikes_count / maxStrikes) + 0.3 * ((c.severity ?? 0) / 5);
  cells.sort((a, b) => score(b) - score(a));
  list.innerHTML = "";
  cells.forEach((c) => {
    const isGhost = (c.misses ?? 0) > 0;
    // Cercle de la cellule
    const sevCol = severityColor(c.severity ?? 0);
    const circle = L.circle([c.centroid.lat, c.centroid.lon], {
      radius: Math.max(c.radius_km, 2) * 1000,
      color: sevCol,
      weight: c.jump_detected ? 4 : 2,
      fillOpacity: isGhost ? 0.03 : (c.jump_detected ? 0.2 : 0.1),
      dashArray: isGhost ? "4,4" : null,
    });
    circle.bindPopup(cellPopupHtml(c));
    circle.on("click", () => selectCell(c.cell_id));
    circle.on("popupopen", () => fillGeoName(circle, c.centroid.lat, c.centroid.lon));
    circle.addTo(state.cellLayer);

    // Trail passé : la trajectoire déjà parcourue (couleur de sévérité).
    if (Array.isArray(c.track) && c.track.length >= 2) {
      L.polyline(c.track, {
        color: sevCol, weight: 2, opacity: 0.5,
        dashArray: isGhost ? "2,5" : null,
      }).addTo(state.cellLayer);
    }

    // Projection future (pointillé) + jalons T+10/20/30 min + cône d'incertitude
    if (c.velocity_kmh && c.heading_deg != null) {
      const rad = (c.heading_deg * Math.PI) / 180;
      const cosLat = Math.cos((c.centroid.lat * Math.PI) / 180);
      const at = (min) => {
        const km = (c.velocity_kmh / 60) * min;
        return [c.centroid.lat + (km / 111) * Math.cos(rad),
                c.centroid.lon + (km / (111 * cosLat)) * Math.sin(rad)];
      };
      const projMin = 30;
      const lenKm = (c.velocity_kmh / 60) * projMin;
      const tip = at(projMin);
      L.polyline([[c.centroid.lat, c.centroid.lon], tip],
        { color: "#f85149", weight: 2, dashArray: "6,5" }).addTo(state.cellLayer);
      [10, 20, 30].forEach((m) => {
        L.circleMarker(at(m), { radius: 3, color: "#f85149", fillColor: "#14181f", fillOpacity: 1, weight: 1.5 })
          .bindTooltip(`+${m} min`, { direction: "top" }).addTo(state.cellLayer);
      });

      // Cône : marge latérale ≈ uncertainty_min × vitesse
      if (c.eta_uncertainty_min) {
        const widthKm = Math.min(c.eta_uncertainty_min * c.velocity_kmh / 60, lenKm / 2);
        const perpRad = rad + Math.PI / 2;
        const px = (widthKm / 111) * Math.cos(perpRad);
        const py = (widthKm / (111 * cosLat)) * Math.sin(perpRad);
        L.polygon([
          [c.centroid.lat, c.centroid.lon],
          [tip[0] + px, tip[1] + py],
          [tip[0] - px, tip[1] - py],
        ], { color: "#f85149", weight: 0, fillColor: "#f85149", fillOpacity: 0.15 }).addTo(state.cellLayer);
      }
    }

    // Notification d'orage sévère (lightning jump) — une fois par cellule.
    if (c.jump_detected && !notifiedJump.has(c.cell_id)) {
      notifiedJump.add(c.cell_id);
      notify("⚠ Orage sévère", `Cellule #${c.cell_id} : intensification rapide (jump)`);
    }

    // Carte sidebar
    const card = document.createElement("div");
    card.className = "cell-card"
      + (c.eta_minutes != null ? " approaching" : "")
      + (c.jump_detected ? " severe" : "")
      + (isGhost ? " ghost" : "");
    const vel = c.velocity_kmh ? `${c.velocity_kmh.toFixed(0)} km/h` : "—";
    const headingStr = c.heading_deg != null ? c.heading_deg.toFixed(0) + "°" : "—";
    const prob = c.strike_probability ? ` · ${Math.round(c.strike_probability * 100)}%` : "";
    const etaStr = c.eta_strike_minutes != null
      ? `⚡ ~${c.eta_strike_minutes.toFixed(0)} min${prob}`
      : (c.eta_minutes != null
          ? `ETA ${c.eta_minutes.toFixed(0)}${c.eta_uncertainty_min ? `±${c.eta_uncertainty_min.toFixed(0)}` : ""} min`
          : (isGhost ? `perdue (${c.misses}×)` : "s'éloigne / inconnue"));
    const sev = (c.severity ?? 0).toFixed(1);
    const sevBadge = `<span class="sev" style="background:${severityColor(c.severity ?? 0)}">${sev}</span>`;
    const jumpBadge = c.jump_detected ? '<span class="badge badge-jump">⚠</span>' : "";
    const lineage = c.parent_id ? ` <span class="muted">⑂${c.parent_id}</span>` : "";
    card.innerHTML = `
      <div class="row"><b>Cellule #${c.cell_id}${lineage}</b><span>${c.strikes_count} impacts ${trendBadge(c.intensity_trend)}</span></div>
      <div class="row small"><span>${vel} cap ${headingStr}</span><span>sév ${sevBadge} ${jumpBadge}</span></div>
      <div class="row small"><span>rayon ${c.radius_km.toFixed(1)} km ${trendBadge(c.radius_trend)}</span><span>${etaStr}</span></div>`;
    card.addEventListener("click", () => focusCell(c.cell_id));
    list.appendChild(card);
    cellRefs.set(c.cell_id, { circle, card, data: c });
  });

  // Oublier les jumps des cellules disparues (pour pouvoir re-notifier au retour).
  for (const id of [...notifiedJump]) if (!cellRefs.has(id)) notifiedJump.delete(id);
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
  if (state.alertEnabled) { ensureAudio(); ensureNotifyPermission(); }  // geste utilisateur
});

// ── Toggle fuseau (local ⇄ UTC) ──────────────────────────────────────────────
const tzBtn = $("#tz-toggle");
function refreshTzBtn() { if (tzBtn) tzBtn.textContent = state.useLocal ? "Local" : "UTC"; }
refreshTzBtn();
if (tzBtn) tzBtn.addEventListener("click", () => {
  state.useLocal = !state.useLocal;
  localStorage.setItem("tz-local", state.useLocal ? "1" : "0");
  refreshTzBtn();
});

// Bip d'alerte synthétisé via WebAudio (aucun fichier audio à charger).
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
    osc.start(t0);
    osc.stop(t0 + 0.36);
  } catch (e) { /* audio indisponible */ }
}

// ── Notifications navigateur (throttlées) ────────────────────────────────────
function ensureNotifyPermission() {
  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission().catch(() => {});
  }
}
let _lastNotify = 0;
function notify(title, body) {
  if (!state.alertEnabled) return;
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  const now = Date.now();
  if (now - _lastNotify < 15000) return;   // anti-spam 15 s
  _lastNotify = now;
  try { new Notification(title, { body }); } catch { /* ignore */ }
}

function maybeAlert(s) {
  if (!state.alertEnabled || !state.config) return;
  if (s.distance_km <= state.config.alert_distance_km) {
    playBeep();
    notify("⚡ Foudre proche", `${s.distance_km.toFixed(1)} km de chez vous`);
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
      else if (msg.type === "strikes_batch") addStrikesBatch(msg.data);
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
  // Valeur pour <input datetime-local> : heure LOCALE (ce que le widget attend).
  const d = new Date(unix * 1000);
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
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
  // Les <input datetime-local> sont en heure locale → on convertit en ISO UTC.
  if ($("#hist-from").value) params.set("from", new Date($("#hist-from").value).toISOString());
  if ($("#hist-to").value) params.set("to", new Date($("#hist-to").value).toISOString());
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

// ── Analyse (Vague 3 : vérification, analytics, catalogue) ───────────────────
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
    drawBars("ch-hour", an.hour_of_day, { color: "#1f6feb", labels: an.hour_of_day.map((_, i) => i), everyLabel: 3 });
    drawBars("ch-week", an.weekday, { color: "#3fb950", labels: ["Dim", "Lun", "Mar", "Mer", "Jeu", "Ven", "Sam"], everyLabel: 1 });
    drawBars("ch-dist", an.distance_hist.map((d) => d.n), { color: "#d29922" });
    drawRose("ch-rose", an.rose);
    renderCatalog(cat.cells);
  } catch (e) { console.error("loadAnalyse", e); }
}

function renderSkill(rep) {
  $("#skill-sub").textContent = `— ${rep.days} j, anneau ${rep.ring_km} km`;
  const pct = (v) => (v == null ? "—" : Math.round(v * 100) + "%");
  const cards = [
    ["POD", pct(rep.pod), "détection"],
    ["FAR", pct(rep.far), "fausses alertes"],
    ["CSI", pct(rep.csi), "score critique"],
    ["Prédictions", rep.total_predictions, ""],
    ["Arrivées", rep.total_arrivals, `foudre ≤ ${rep.ring_km} km`],
    ["Erreur ETA", rep.mean_eta_error_min != null ? rep.mean_eta_error_min + " min" : "—", "moyenne"],
    ["Préavis", rep.mean_lead_min != null ? rep.mean_lead_min + " min" : "—", "moyenne"],
  ];
  $("#skill-cards").innerHTML = cards.map(([k, v, sub]) =>
    `<div class="skill-card"><div class="sk-val">${v}</div><div class="sk-key">${k}</div><div class="sk-sub">${sub}</div></div>`
  ).join("");
}

function renderPredTable(recent) {
  const tb = $("#pred-table tbody");
  if (!recent || !recent.length) {
    tb.innerHTML = `<tr><td colspan="6" class="muted">aucune prédiction sur la période</td></tr>`;
    return;
  }
  tb.innerHTML = recent.map((p) => {
    const out = p.outcome === "hit"
      ? '<span class="color-green">✓ touché</span>'
      : '<span class="color-orange">✗ fausse alerte</span>';
    const err = p.eta_error_min != null ? `${p.eta_error_min > 0 ? "+" : ""}${p.eta_error_min.toFixed(1)} min` : "—";
    const eta = p.eta_strike_min != null ? `${p.eta_strike_min.toFixed(0)} min` : "?";
    return `<tr><td>${fmtClock(p.ts_made)}</td><td>#${p.cell_id}</td><td>${eta}</td><td>${Math.round((p.probability || 0) * 100)}%</td><td>${out}</td><td>${err}</td></tr>`;
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
    ctx.fillStyle = "#8b95a7"; ctx.font = "11px sans-serif"; ctx.fillText("(aucune donnée)", 10, 20); return;
  }
  const max = Math.max(...values);
  const bw = W / values.length;
  ctx.fillStyle = opts.color || "#1f6feb";
  values.forEach((v, i) => {
    const h = (v / max) * (H - 16);
    ctx.fillRect(i * bw + 1, H - h - 12, Math.max(1, bw - 2), h);
  });
  if (opts.labels) {
    ctx.fillStyle = "#8b95a7"; ctx.font = "9px sans-serif";
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
  const cx = 85, cy = 85, R = 72;
  const max = Math.max(1, ...rose.map((d) => d.n));
  let paths = "";
  rose.forEach((d, i) => {
    const a0 = ((i * 22.5 - 11.25) - 90) * Math.PI / 180;
    const a1 = ((i * 22.5 + 11.25) - 90) * Math.PI / 180;
    const r = (d.n / max) * R;
    if (r < 0.5) return;
    const x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0);
    const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
    paths += `<path d="M${cx},${cy} L${x0.toFixed(1)},${y0.toFixed(1)} A${r.toFixed(1)},${r.toFixed(1)} 0 0,1 ${x1.toFixed(1)},${y1.toFixed(1)} Z" fill="#1f6feb" opacity="0.75"/>`;
  });
  const ring = `<circle cx="${cx}" cy="${cy}" r="${R}" fill="none" stroke="#2a2f38"/>`;
  const lbls = `<text x="${cx - 4}" y="11" class="rose-lbl">N</text><text x="164" y="${cy + 4}" class="rose-lbl">E</text>` +
    `<text x="${cx - 4}" y="167" class="rose-lbl">S</text><text x="2" y="${cy + 4}" class="rose-lbl">O</text>`;
  el.innerHTML = `<svg viewBox="0 0 170 170" width="170" height="170">${ring}${paths}${lbls}</svg>`;
}

function fmtDuration(min) {
  if (min < 60) return `${Math.round(min)} min`;
  return `${(min / 60).toFixed(1)} h`;
}

function renderCatalog(cells) {
  const tb = $("#catalog-table tbody");
  if (!cells || !cells.length) {
    tb.innerHTML = `<tr><td colspan="6" class="muted">aucune cellule persistée sur la période</td></tr>`;
    return;
  }
  tb.innerHTML = "";
  cells.forEach((c) => {
    const tr = document.createElement("tr");
    tr.className = "catalog-row";
    const dur = (c.last_seen - c.first_seen) / 60;
    tr.innerHTML = `<td>#${c.cell_id}</td><td>${fmtDateTime(c.first_seen)}</td><td>${fmtDuration(dur)}</td>` +
      `<td>${c.total_strikes ?? "?"}</td><td>${(c.max_severity ?? 0).toFixed(1)}</td><td>${(c.peak_flash_rate ?? 0).toFixed(0)}</td>`;
    tr.addEventListener("click", () => showCellTrack(c.run_id, c.cell_id));
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

// ── Déplacement de HOME (clic sur la carte) ──────────────────────────────────
let homePickMode = false;
const setHomeBtn = $("#set-home");
if (setHomeBtn) setHomeBtn.addEventListener("click", () => {
  homePickMode = !homePickMode;
  setHomeBtn.classList.toggle("active", homePickMode);
  mapLive.getContainer().style.cursor = homePickMode ? "crosshair" : "";
});
mapLive.on("click", async (e) => {
  if (!homePickMode) return;
  homePickMode = false;
  if (setHomeBtn) setHomeBtn.classList.remove("active");
  mapLive.getContainer().style.cursor = "";
  const lat = e.latlng.lat, lon = e.latlng.lng;
  try {
    const r = await fetch("/api/home", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lat, lon }),
    });
    if (!r.ok) return;
    if (state.homeMarker) state.homeMarker.setLatLng([lat, lon]);
    if (state.radiusCircle) state.radiusCircle.setLatLng([lat, lon]);
    if (state.config) state.config.home = { lat, lon };
    $("#home-pos").textContent = `${lat.toFixed(4)}°, ${lon.toFixed(4)}°`;
  } catch (err) { console.error("set-home", err); }
});

// ── Overlay radar de précipitations (RainViewer, API publique gratuite) ──────
const radar = { host: "", frames: [], idx: 0, layer: null, timer: null };

async function radarInit() {
  try {
    const j = await fetch("https://api.rainviewer.com/public/weather-maps.json").then((r) => r.json());
    radar.host = j.host;
    radar.frames = [...(j.radar?.past || []), ...(j.radar?.nowcast || [])];
    if (!radar.frames.length) return false;
    radar.idx = Math.max(0, (j.radar?.past?.length || 1) - 1);  // dernière frame "passée"
    $("#radar-slider").max = radar.frames.length - 1;
    $("#radar-slider").value = radar.idx;
    return true;
  } catch (e) { console.warn("radar indisponible", e); return false; }
}

function radarShow(i) {
  if (!radar.frames.length) return;
  radar.idx = Math.max(0, Math.min(i, radar.frames.length - 1));
  const f = radar.frames[radar.idx];
  // RainViewer ne sert le radar que jusqu'au zoom 7 ; au-delà on ré-échelonne
  // les tuiles z7 (maxNativeZoom) au lieu d'afficher « Zoom Level Not Supported ».
  const layer = L.tileLayer(`${radar.host}${f.path}/256/{z}/{x}/{y}/2/1_1.png`, {
    opacity: 0.6, zIndex: 250, maxNativeZoom: 7, maxZoom: 18,
  });
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
  $("#radar-play").textContent = "⏸";
}
function radarStop() { clearInterval(radar.timer); radar.timer = null; $("#radar-play").textContent = "▶"; }
function radarOff() {
  radarStop();
  if (radar.layer) { mapLive.removeLayer(radar.layer); radar.layer = null; }
  $("#radar-time").textContent = "";
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
