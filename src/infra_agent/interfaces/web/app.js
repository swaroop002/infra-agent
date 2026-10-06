"use strict";

// ------------------------------------------------------------------ helpers
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};

function headers() {
  const h = { "Content-Type": "application/json" };
  const key = store.get("infra-agent-api-key");
  if (key) h["X-API-Key"] = key;
  return h;
}

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { ...headers(), ...(opts.headers || {}) } });
  if (res.status === 401) {
    const key = prompt("API key for Infra Agent:");
    if (key) { store.set("infra-agent-api-key", key); return api(path, opts); }
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res;
}

function ago(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m ago`;
  return `${Math.floor(s / 86400)}d ${Math.round((s % 86400) / 3600)}h ago`;
}
const utc = (iso) => iso ? iso.slice(0, 16).replace("T", " ") + " UTC" : "";
const safeUrl = (u) => /^https?:\/\//i.test(u || "") ? u : null;

// Minimal, escape-first Markdown renderer (headings, lists, tables, code, bold, italics, links).
function md(src) {
  const lines = String(src || "").replace(/\r/g, "").split("\n");
  const out = [];
  let i = 0;
  const inline = (t) => esc(t)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])_([^_]+)_(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
    .replace(/(^|[\s(])\*([^*]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_, text, url) => `<a href="${url}" target="_blank" rel="noopener">${text}</a>`);
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`);
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)/);
    if (h) { out.push(`<h${Math.min(3, h[1].length)}>${inline(h[2])}</h${Math.min(3, h[1].length)}>`); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      out.push(`<table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${
        rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      continue;
    }
    if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*•]|\d+[.)])\s+/, ""));
        i++;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>${items.map((t) => `<li>${inline(t)}</li>`).join("")}</${tag}>`);
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const para = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s|\s*([-*•]|\d+[.)])\s+|\s*\|)/.test(lines[i])) para.push(lines[i++]);
    out.push(`<p>${inline(para.join(" "))}</p>`);
  }
  return out.join("");
}

function pill(text, tone) { return `<span class="pill ${tone}">${esc(text)}</span>`; }
const stateTone = (s) => ({ firing: "bad", down: "bad", DOWN: "bad", UNREACHABLE: "bad", GONE: "bad", acknowledged: "warn",
  pending: "warn", DEGRADED: "warn", silenced: "neutral", stopped: "neutral", STOPPED: "neutral", resolved: "good",
  up: "good", HEALTHY: "good", UP: "good", running: "good" }[s] || "neutral");

// ---------------------------------------------------------------------- tabs
const panels = { ask: $("#panel-ask"), alerts: $("#panel-alerts"), status: $("#panel-status") };
let activeTab = "ask";
document.querySelectorAll(".tab").forEach((btn) => btn.addEventListener("click", () => showTab(btn.dataset.tab)));
function showTab(name) {
  activeTab = name;
  document.querySelectorAll(".tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === name);
    b.setAttribute("aria-selected", b.dataset.tab === name);
  });
  Object.entries(panels).forEach(([k, el]) => { el.hidden = k !== name; });
  if (name === "alerts") loadAlerts();
  if (name === "status") loadStatus();
  store.set("infra-agent-tab", name);
}

// ---------------------------------------------------------------------- meta
async function loadMeta() {
  try {
    const m = await (await api("/api/meta")).json();
    const agent = m.agent_ready ? `<span class="dot ok"></span> ${esc(m.model)}` : `<span class="dot bad"></span> agent unavailable`;
    $("#meta").innerHTML = `${agent}<span>·</span><span>${esc(m.user)}</span>`;
    if (!m.agent_ready && m.agent_error) $("#meta").title = m.agent_error;
  } catch (e) {
    $("#meta").textContent = e.message;
  }
}

// ---------------------------------------------------------------------- chat
const chat = $("#chat");
const input = $("#input");
let threadId = store.get("infra-agent-thread") || newThreadId();
let busy = false;

function newThreadId() {
  const id = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).replace(/-/g, "");
  store.set("infra-agent-thread", id);
  return id;
}

$("#new-thread").addEventListener("click", () => {
  threadId = newThreadId();
  chat.querySelectorAll(".msg").forEach((m) => m.remove());
  $("#chat-empty").hidden = false;
  input.focus();
});
$("#suggestions").addEventListener("click", (e) => {
  if (e.target.classList.contains("chip")) { input.value = e.target.textContent; send(); }
});
input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(180, input.scrollHeight) + "px"; });
input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
$("#composer").addEventListener("submit", (e) => { e.preventDefault(); send(); });

function addMessage(role, html) {
  $("#chat-empty").hidden = true;
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = html;
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
  return el;
}

function renderEvidence(evidence) {
  if (!evidence || !evidence.length) return "";
  const items = evidence.map((e) => {
    const url = safeUrl(e.url);
    const range = e.start ? `<span class="muted">${utc(e.start)} → ${utc(e.end)}</span>` : "";
    return `<div class="ev"><div class="ev-head"><span class="tag">${esc(e.kind)}</span><span>${esc(e.source)}</span>
      <span class="muted">via ${esc(e.tool || "")}</span>${range}${url ? `<a href="${esc(url)}" target="_blank" rel="noopener">Open in Grafana ↗</a>` : ""}</div>
      ${e.query ? `<code>${esc(e.query)}</code>` : ""}${e.note ? `<div class="muted">${esc(e.note)}</div>` : ""}</div>`;
  }).join("");
  return `<details class="evidence"><summary>Evidence (${evidence.length})</summary>${items}</details>`;
}

async function send() {
  const text = input.value.trim();
  if (!text || busy) return;
  busy = true;
  $("#send").disabled = true;
  input.value = "";
  input.style.height = "auto";
  addMessage("user", `<div class="bubble">${esc(text)}</div>`);
  const el = addMessage("assistant", `<div class="steps"></div><div class="bubble"><span class="muted">Thinking…</span></div>`);
  const steps = $(".steps", el);
  const bubble = $(".bubble", el);
  let draft = "";
  try {
    const res = await api("/api/chat/stream", { method: "POST", body: JSON.stringify({ message: text, thread_id: threadId }) });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (!chunk.startsWith("data: ")) continue;
        const ev = JSON.parse(chunk.slice(6));
        if (ev.type === "tool_start") {
          draft = "";
          const s = document.createElement("div");
          s.className = "step running";
          s.id = `step-${ev.id}`;
          const arg = Object.values(ev.args || {}).filter((v) => typeof v === "string").join(", ");
          s.textContent = `${ev.label}${arg ? ` (${arg})` : ""}`;
          steps.appendChild(s);
          bubble.innerHTML = `<span class="muted">Investigating…</span>`;
        } else if (ev.type === "tool_end") {
          const s = document.getElementById(`step-${ev.id}`);
          if (s) s.className = `step ${ev.status === "error" ? "error" : "done"}`;
        } else if (ev.type === "token") {
          draft += ev.text;
          bubble.innerHTML = md(draft);
        } else if (ev.type === "final") {
          bubble.innerHTML = md(ev.answer || "_No answer returned._");
          el.insertAdjacentHTML("beforeend", `<div class="answer-foot"><span>${ev.duration_s}s · ${ev.tools_used.length} tool call(s)</span></div>${renderEvidence(ev.evidence)}`);
        } else if (ev.type === "error") {
          bubble.innerHTML = `<span style="color:var(--bad)">${esc(ev.message)}</span>`;
        }
        chat.scrollTop = chat.scrollHeight;
      }
    }
  } catch (e) {
    bubble.innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`;
  } finally {
    busy = false;
    $("#send").disabled = false;
    input.focus();
  }
}

// -------------------------------------------------------------------- alerts
const alertState = { window: "24h", state: "", search: "" };
function segmented(root, key, target, onChange) {
  root.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    root.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    target[key] = b.dataset.v;
    onChange();
  });
}
segmented($("#alert-window"), "window", alertState, loadAlerts);
segmented($("#alert-state"), "state", alertState, loadAlerts);
let searchTimer;
$("#alert-search").addEventListener("input", (e) => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { alertState.search = e.target.value; loadAlerts(); }, 250); });
$("#alert-refresh").addEventListener("click", loadAlerts);

async function loadAlerts() {
  const params = new URLSearchParams({ window: alertState.window });
  if (alertState.state) params.set("state", alertState.state);
  if (alertState.search) params.set("search", alertState.search);
  const list = $("#alert-list");
  try {
    const data = await (await api(`/api/alerts?${params}`)).json();
    const counts = data.counts_by_state || {};
    $("#alert-summary").innerHTML = Object.entries(counts).map(([s, n]) => pill(`${n} ${s}`, stateTone(s))).join("") +
      (data.data_errors ? `<span class="muted">⚠ ${esc(data.data_errors.join("; "))}</span>` : "") +
      `<span class="muted small">sources: ${esc((data.sources || []).join(", ") || "none")}</span>`;
    const active = (counts.firing || 0) + (counts.acknowledged || 0);
    const badge = $("#alerts-count");
    badge.hidden = !active;
    badge.textContent = active;
    if (!data.alerts.length) { list.innerHTML = `<div class="muted pad">No alerts in this window.</div>`; return; }
    list.innerHTML = data.alerts.map((a) => {
      const url = safeUrl(a.url);
      const title = url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(a.title)}</a>` : esc(a.title);
      const labels = Object.entries(a.labels || {}).slice(0, 8).map(([k, v]) => `<span class="label">${esc(k)}=${esc(v)}</span>`).join("");
      const ended = a.ended_at ? ` · resolved ${ago(a.ended_at)}` : "";
      return `<div class="alert-row"><div>${pill(a.state, stateTone(a.state))}</div>
        <div><div class="title">${title}</div>
        <div class="sub">${a.severity ? esc(a.severity) + " · " : ""}${esc(a.source)}${a.summary ? " · " + esc(a.summary) : ""}</div>
        <div class="labels">${labels}</div></div>
        <div class="when">${esc(utc(a.started_at))}<br>${ago(a.started_at)}${ended}</div></div>`;
    }).join("");
  } catch (e) {
    list.innerHTML = `<div class="pad" style="color:var(--bad)">${esc(e.message)}</div>`;
  }
}

// -------------------------------------------------------------------- status
const statusState = { uptime: "7d" };
segmented($("#uptime-window"), "uptime", statusState, loadUptime);
$("#status-refresh").addEventListener("click", loadStatus);

function card(k, v, tone = "") { return `<div class="card"><div class="k">${esc(k)}</div><div class="v ${tone}">${esc(v)}</div></div>`; }

async function loadStatus() {
  try {
    const data = await (await api("/api/status")).json();
    const fleet = data.fleet || {};
    const t = fleet.targets || { total: 0, down: 0 };
    const svcDown = data.services.filter((s) => s.status === "down").length;
    const vibe = data.vibe;
    const unhealthyVibe = vibe ? (vibe.vms || []).filter((v) => v.cloud_status_checks && Object.values(v.cloud_status_checks).includes("impaired")).length : 0;
    $("#status-cards").innerHTML = [
      card("Targets up", `${t.total - t.down}/${t.total}`, t.down ? "warn" : "good"),
      card("Servers down", t.down, t.down ? "bad" : "good"),
      card("Open outages", (fleet.open_outages || []).length, (fleet.open_outages || []).length ? "bad" : "good"),
      card("Services down", `${svcDown}/${data.services.length}`, svcDown ? "bad" : "good"),
      vibe ? card("Vibe VMs running", `${(vibe.by_state || {}).running || 0}/${vibe.count}`, unhealthyVibe ? "warn" : "") : "",
    ].join("");
    $("#status-down").innerHTML = (fleet.down_targets || []).map((d) =>
      `<div class="item">${pill("down", "bad")} <strong>${esc(d.name)}</strong> <span class="muted">${esc(d.environment || "not in catalog")} · ${esc(d.instance || "")}</span></div>`
    ).join("") || `<div class="muted">All targets are up.</div>`;
    $("#status-outages").innerHTML = (fleet.open_outages || []).map((o) =>
      `<div class="item"><strong>${esc(o.name)}</strong> <span class="muted">${esc(o.duration)} · since ${esc(utc(o.started_at))}</span><div class="muted small">${esc(o.detail)}</div></div>`
    ).join("") || `<div class="muted">No open outages${data.services.length ? "" : " (ledger has no data yet)"}.</div>`;
    $("#status-services").innerHTML = data.services.length ? `<table class="data"><thead><tr><th>Service</th><th>Status</th><th>Since</th><th>Env</th><th class="num">24h uptime</th><th>Detail</th></tr></thead><tbody>${
      data.services.map((s) => `<tr><td><strong>${esc(s.name)}</strong></td><td>${pill(s.status, stateTone(s.status))}</td><td>${ago(s.since)}</td>
        <td>${esc(s.environment || "")}</td><td class="num">${s.uptime_24h_pct ?? "—"}${s.uptime_24h_pct != null ? "%" : ""}</td><td class="muted small">${esc(s.detail || "")}</td></tr>`).join("")}</tbody></table>`
      : `<div class="muted">No service history yet. The collector records it once it runs.</div>`;
    const vb = $("#vibe-block");
    vb.hidden = !vibe;
    if (vibe) {
      $("#status-vibe").innerHTML = `<table class="data"><thead><tr><th>VM</th><th>Owner</th><th>State</th><th>Running for</th><th>Checks</th><th>Provision</th></tr></thead><tbody>${
        (vibe.vms || []).map((v) => `<tr><td><strong>${esc(v.name || v.id)}</strong><div class="muted small">${esc(v.id)}</div></td><td>${esc(v.owner || "")}</td>
          <td>${pill(v.state, stateTone(v.state))}</td><td>${esc(v.running_for || "—")}</td>
          <td class="small">${esc(Object.entries(v.cloud_status_checks || {}).map(([k, x]) => `${k}: ${x}`).join(", "))}</td><td>${esc(v.provision_status || "")}</td></tr>`).join("")}</tbody></table>`;
    }
    $("#status-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    $("#status-cards").innerHTML = `<div style="color:var(--bad)">${esc(e.message)}</div>`;
  }
  loadUptime();
}

async function loadUptime() {
  $("#uptime-label").textContent = `last ${statusState.uptime}`;
  try {
    const data = await (await api(`/api/uptime?kind=service&window=${statusState.uptime}`)).json();
    const rows = data.entities || [];
    $("#status-uptime").innerHTML = rows.length ? `<table class="data"><thead><tr><th>Entity</th><th class="num">Uptime</th><th></th><th class="num">Outages</th><th class="num">Downtime</th><th class="num">Coverage</th></tr></thead><tbody>${
      rows.map((r) => {
        const pct = r.uptime_pct ?? 0;
        const tone = pct >= 99.9 ? "" : pct >= 99 ? "warn" : "bad";
        return `<tr><td>${esc(r.name || r.entity)}</td><td class="num">${r.uptime_pct ?? "—"}%</td><td><div class="bar ${tone}"><span style="width:${Math.max(0, Math.min(100, pct))}%"></span></div></td>
          <td class="num">${r.outages}</td><td class="num">${esc(r.downtime)}</td><td class="num">${r.coverage_pct}%</td></tr>`;
      }).join("")}</tbody></table>` : `<div class="muted">${esc(data.note || data.data_errors?.join("; ") || "No data.")}</div>`;
  } catch (e) {
    $("#status-uptime").innerHTML = `<div style="color:var(--bad)">${esc(e.message)}</div>`;
  }
}

// ------------------------------------------------------------------ startup
loadMeta();
showTab(store.get("infra-agent-tab") || "ask");
loadAlerts();
setInterval(() => {
  if (document.hidden) return;
  if (activeTab === "alerts") loadAlerts();
  if (activeTab === "status") loadStatus();
}, 60000);
