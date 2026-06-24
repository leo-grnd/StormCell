/* ============================================================================
   ops.js — page « Mode 24/7 » : supervision live + contrôles.
   ========================================================================== */
"use strict";
const $ = (s) => document.querySelector(s);

// ── Thème ─────────────────────────────────────────────────────────────────────
$("#theme-toggle")?.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("sc-theme", next); } catch (e) { /* */ }
});

// ── Formatage ─────────────────────────────────────────────────────────────────
function fmtBytes(b) {
  if (b == null) return "—";
  const u = ["o", "Ko", "Mo", "Go", "To"]; let i = 0, n = b;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}
function fmtDur(s) {
  if (s == null) return "—";
  s = Math.max(0, Math.floor(s));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}j ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m ${s % 60}s`;
}
function fmtNum(n) { return n == null ? "—" : n.toLocaleString("fr-FR"); }

// ── Rendu du statut ───────────────────────────────────────────────────────────
function render(s) {
  // Mode 24/7
  const mt = $("#mode-toggle"); mt.checked = !!s.continuous_mode;
  $("#mode-lab").textContent = s.continuous_mode ? "ACTIF" : "inactif";
  $("#master").classList.toggle("on", !!s.continuous_mode);

  // Archive 24/7 dédiée + bannière d'enregistrement (effet « ressenti »)
  const arch = s.archive || {};
  const active = !!arch.active;
  document.body.classList.toggle("rec-on", active);
  const banner = $("#rec-banner"); banner.hidden = !active;
  $("#card-archive").classList.toggle("live", active);
  $("#arch-dot").style.animationPlayState = active ? "running" : "paused";
  if (active) {
    $("#c-arch").textContent = fmtBytes(arch.db_bytes);
    const since = arch.started_at ? fmtDur((s.server_time || Date.now() / 1000) - arch.started_at) : "—";
    $("#c-arch-sub").innerHTML = `${fmtNum(arch.total)} impacts · depuis ${since}`;
    $("#rec-count").textContent = fmtNum(arch.total);
    $("#rec-sub").textContent = arch.path || "base d'archive dédiée";
  } else {
    $("#c-arch").textContent = "inactive";
    $("#c-arch-sub").textContent = "activez le mode 24/7 pour archiver à part";
  }

  // Flux + watchdog (âge du dernier message)
  const age = s.last_message_at ? Date.now() / 1000 - s.last_message_at : null;
  const dot = $("#flux-dot");
  let cls = "dot-red", state = "hors-ligne";
  if (age != null) {
    if (age <= 15) { cls = "dot-green"; state = "actif"; }
    else if (age <= 120) { cls = "dot-orange"; state = `silence ${Math.round(age)} s`; }
    else { cls = "dot-red"; state = `silence ${Math.round(age / 60)} min`; }
  }
  dot.className = "dot " + cls;
  $("#c-flux").textContent = s.endpoint || s.source || "—";
  $("#c-flux-sub").innerHTML = `${state} · latence ${s.latency_s != null ? s.latency_s + " s" : "—"}`;

  // Capture
  $("#c-cap24").textContent = fmtNum(s.capture_24h);
  $("#c-captot").textContent = `${fmtNum(s.logged_total)} au total · +${fmtNum(s.logged_session)} session`;

  // DB
  const st = s.storage || {};
  $("#c-db").textContent = fmtBytes(st.db_bytes);
  $("#c-wal").textContent = `WAL ${fmtBytes(st.wal_bytes)}`;

  // Disque
  const dk = st.disk || {};
  if (dk.total) {
    const usedPct = (dk.used / dk.total) * 100, freePct = 100 - usedPct;
    $("#c-disk").innerHTML = `${fmtBytes(dk.free)} <small>libres</small>`;
    const bar = $("#disk-bar");
    bar.style.width = usedPct.toFixed(1) + "%";
    bar.style.background = freePct < 5 ? "var(--sev5)" : freePct < 12 ? "var(--sev2)" : "var(--sev0)";
    const v = $("#c-disk"); v.classList.toggle("crit", freePct < 5); v.classList.toggle("warn", freePct >= 5 && freePct < 12);
    $("#c-disk-sub").textContent = `${fmtBytes(dk.used)} / ${fmtBytes(dk.total)} (${usedPct.toFixed(0)} %)`;
  } else { $("#c-disk").textContent = "—"; }

  // Débit
  $("#c-rate").innerHTML = `${s.world_per_s != null ? s.world_per_s.toFixed(0) : "—"} <small>msg/s</small>`;
  const drops = s.queue_dropped || 0;
  $("#c-rate-sub").innerHTML = `${s.nearby_per_min != null ? s.nearby_per_min.toFixed(0) : "—"}/min zone · file ${s.queue_depth ?? "?"}/${s.queue_max ?? "?"}`
    + (drops ? ` · <span style="color:var(--sev5)">⚠ ${drops} perdus</span>` : "");

  // Cellules
  $("#c-cells").textContent = s.cells_count ?? "—";
  $("#c-calc").textContent = s.cells_compute_ms != null ? `recalcul ${s.cells_compute_ms} ms` : "—";

  // Uptime
  $("#c-uptime").textContent = fmtDur(s.started_at ? s.server_time - s.started_at : null);
  $("#c-run").textContent = `tampon ${fmtNum(s.recent_buffer)}/${fmtNum(s.recent_buffer_max)}`;

  // Rétention
  $("#c-ret").textContent = s.retention_days ? `${s.retention_days} j` : "illimitée";
  if (document.activeElement !== $("#ret-days")) $("#ret-days").value = s.retention_days || 0;

  $("#btn-reprobe").disabled = !s.can_reprobe;
}

// ── Réseau ────────────────────────────────────────────────────────────────────
async function refresh() {
  try { const s = await fetch("/api/ops/status").then((r) => r.json()); render(s); }
  catch (e) { /* serveur indisponible : on réessaiera au prochain tick */ }
}
function result(msg, ok = true) {
  const el = $("#ops-result"); el.textContent = msg; el.className = "ops-result " + (ok ? "ok" : "err");
}
async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

$("#mode-toggle").addEventListener("change", async (e) => {
  try {
    const j = await post("/api/ops/mode", { enabled: e.target.checked });
    result(e.target.checked
      ? `● Enregistrement 24/7 ACTIVÉ — archive dédiée : ${j.archive_path}`
      : "Mode 24/7 désactivé — l'archive est fermée et conservée.");
    refresh();
  } catch (err) { result("Échec : " + err.message, false); }
});
$("#btn-ret").addEventListener("click", async () => {
  const days = Math.max(0, +$("#ret-days").value || 0);
  try { const j = await post("/api/ops/retention", { days }); result(`Rétention réglée à ${j.retention_days ? j.retention_days + " jours" : "illimitée"}.`); refresh(); }
  catch (err) { result("Échec : " + err.message, false); }
});
$("#btn-reprobe").addEventListener("click", async () => {
  try { await post("/api/ops/reprobe"); result("Re-sélection de serveur demandée — reconnexion en cours…"); }
  catch (err) { result("Échec : " + err.message, false); }
});
$("#btn-maintain").addEventListener("click", async (e) => {
  e.target.disabled = true; result("Maintenance en cours…");
  try { const j = await post("/api/ops/maintain"); result(`Maintenance OK · ${fmtNum(j.deleted)} purgés · checkpoint ${j.checkpointed ? "✓" : "✗"}${j.vacuumed ? " · VACUUM ✓" : ""}.`); refresh(); }
  catch (err) { result("Échec : " + err.message, false); }
  finally { e.target.disabled = false; }
});
$("#btn-backup").addEventListener("click", async (e) => {
  e.target.disabled = true; result("Sauvegarde en cours (VACUUM INTO)…");
  try { const j = await post("/api/ops/backup"); result(`Sauvegarde écrite : ${j.path} (${fmtBytes(j.bytes)}).`); }
  catch (err) { result("Échec : " + err.message, false); }
  finally { e.target.disabled = false; }
});

$("#btn-purge").addEventListener("click", async (e) => {
  const withArchive = $("#purge-archive").checked;
  const msg = withArchive
    ? "PURGER TOUTES LES BASES, Y COMPRIS L'ARCHIVE 24/7 ?\n\nCette action est irréversible et supprime toutes les données capturées."
    : "Purger la base normale (impacts, cellules, prédictions…) ?\n\nIrréversible. L'archive 24/7 sera préservée.";
  if (!window.confirm(msg)) return;
  e.target.disabled = true; result("Purge en cours…");
  try {
    const j = await post("/api/ops/purge", { include_archive: withArchive });
    const arch = j.purged_archive != null ? ` · archive : ${fmtNum(j.purged_archive)}` : "";
    result(`Bases purgées · base normale : ${fmtNum(j.purged_main)} impacts${arch}.`);
    refresh();
  } catch (err) { result("Échec : " + err.message, false); }
  finally { e.target.disabled = false; }
});

// onglets de supervision
document.querySelectorAll(".tabs2 button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tabs2 button").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  ["docker", "nssm", "watchdog"].forEach((k) => { $("#sup-" + k).style.display = k === b.dataset.sup ? "block" : "none"; });
}));

refresh();
setInterval(refresh, 5000);
