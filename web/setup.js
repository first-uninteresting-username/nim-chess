"use strict";
// Tournament setup (providers, models, settings, start/stop) and the run status panel.
// Uses helpers from app.js ($, api, esc, nm, go, loadRuns, runs); both files share the global scope.

const PRESETS = [
  { label: "NVIDIA NIM", name: "nvidia", base_url: "https://integrate.api.nvidia.com/v1", key_env: "NVIDIA_API_KEY" },
  { label: "OpenRouter", name: "openrouter", base_url: "https://openrouter.ai/api/v1", key_env: "OPENROUTER_API_KEY" },
  { label: "OpenAI", name: "openai", base_url: "https://api.openai.com/v1", key_env: "OPENAI_API_KEY" },
  { label: "Groq", name: "groq", base_url: "https://api.groq.com/openai/v1", key_env: "GROQ_API_KEY" },
  { label: "Together", name: "together", base_url: "https://api.together.xyz/v1", key_env: "TOGETHER_API_KEY" },
  { label: "Mistral", name: "mistral", base_url: "https://api.mistral.ai/v1", key_env: "MISTRAL_API_KEY" },
  { label: "DeepSeek", name: "deepseek", base_url: "https://api.deepseek.com/v1", key_env: "DEEPSEEK_API_KEY" },
  { label: "Ollama (local)", name: "ollama", base_url: "http://127.0.0.1:11434/v1", key_env: "" },
  { label: "LM Studio (local)", name: "lmstudio", base_url: "http://127.0.0.1:1234/v1", key_env: "" },
  { label: "Mock (random player)", name: "mock", type: "mock" },
  { label: "Custom OpenAI-compatible", name: "custom", base_url: "https://", key_env: "" },
];

let S = null; // setup state

const human = (k) => k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const jsonStr = (o) => (o && Object.keys(o).length ? JSON.stringify(o) : "");

async function renderSetup() {
  app.innerHTML = `<p class="muted pad">Loading config…</p>`;
  let d;
  try { d = await api("config"); } catch (e) { app.innerHTML = `<p class="pad">Could not load config: ${esc(e.message)}</p>`; return; }
  S = {
    path: d.path, exists: d.exists, schema: d.schema, lists: S?.lists || {}, dirty: false, mock: S?.mock || false,
    providers: Object.entries(d.config.providers).map(([name, p]) => ({
      name, _orig: name, type: p.type || "openai", base_url: p.base_url || "", key_env: p.key_env || "", rpm: p.rpm || "",
      headers: jsonStr(p.headers), extra: jsonStr(p.extra), has_key: p.has_key, env_present: p.env_present, newKey: "", clear_key: false,
    })),
    models: d.config.models.map((m) => ({ ...m })),
    settings: { ...d.config.settings },
  };
  draw();
  const t = setInterval(() => { if (!location.hash.startsWith("#/setup")) return clearInterval(t); drawActive(); }, 3000);
  view = { cleanup: () => clearInterval(t) };
}

function markDirty() { S.dirty = true; const el = $("#dirty"); if (el) el.hidden = false; }

function draw() {
  const enabled = S.models.filter((m) => m.enabled).length;
  const provOpts = (sel) => S.providers.map((p) => `<option ${p.name === sel ? "selected" : ""}>${esc(p.name)}</option>`).join("");
  let seed = 0;
  app.innerHTML = `
  <div class="toolbar"><h2 style="margin:0">New tournament</h2>
    <span class="badge warn" id="dirty" ${S.dirty ? "" : "hidden"}>unsaved changes</span>
    <span style="margin-left:auto"></span>
    <button id="s-reload" title="Discard edits and reload the config from disk">Reload</button>
    <button id="s-save">Save</button>
  </div>
  <div class="setup">
    <div class="panel start">
      <div><b>${enabled}</b> model${enabled === 1 ? "" : "s"} enabled · ${enabled > 1 ? `${Math.ceil(Math.log2(enabled))} rounds` : "need at least 2"}</div>
      <label class="chk"><input type="checkbox" id="s-mock" ${S.mock ? "checked" : ""}> Mock players (random moves, no API calls)</label>
      <button id="s-start" class="primary" ${enabled < 2 ? "disabled" : ""}>▶ Save &amp; start tournament</button>
      <div id="s-msg" class="msg"></div>
      <div id="active"></div>
      <details class="hint"><summary>Headless equivalent</summary>
        <p class="muted">The command line uses the same config file:</p>
        <pre>python3 -I chess_tourney.py --config ${esc(S.path)}${S.mock ? " --mock" : ""}</pre>
        <p class="muted">Edit it from the shell with <code>--provider NAME=URL</code>, <code>--key-env NAME=VAR</code>, <code>--models</code> / <code>--add-model</code> / <code>--remove-model PROVIDER:MODEL</code>, any setting flag, plus <code>--save-config</code>. <code>--show-config</code> and <code>--list-models PROVIDER</code> to inspect.</p>
      </details>
    </div>

    <div class="panel">
      <div class="phead"><h3>Providers</h3>
        <select id="p-preset"><option value="">＋ Add provider…</option>${PRESETS.map((p, i) => `<option value="${i}">${esc(p.label)}</option>`).join("")}</select>
      </div>
      <p class="muted small">Any OpenAI-compatible <code>/chat/completions</code> endpoint. Keys come from an environment variable of the server/CLI process, or can be stored in the config file (mode 600, never sent back to the browser).</p>
      <div class="providers">${S.providers.map((p, i) => providerCard(p, i)).join("")}</div>
    </div>

    <div class="panel">
      <div class="phead"><h3>Models</h3><span class="muted small">Seed order = list order (unless shuffled). Test sends one tool-calling prompt.</span></div>
      <table class="models">
        <thead><tr><th class="num">Seed</th><th></th><th>Provider</th><th>Model</th><th></th><th></th></tr></thead>
        <tbody>${S.models.map((m, i) => {
          const t = m.test;
          const res = !t ? "" : t === "pending" ? `<span class="muted">testing…</span>` : `<span class="badge ${t.ok ? "win" : "warn"}" title="${esc(t.info)}">${t.ok ? "ok" : "fail"}</span> <span class="muted small trunc" title="${esc(t.info)}">${esc(t.info)}</span>`;
          return `<tr class="${m.enabled ? "" : "off"}">
            <td class="num">${m.enabled ? ++seed : ""}</td>
            <td><input type="checkbox" data-m="${i}" data-k="enabled" ${m.enabled ? "checked" : ""} title="Enabled"></td>
            <td><select data-m="${i}" data-k="provider">${provOpts(m.provider)}</select></td>
            <td><input class="wide" data-m="${i}" data-k="model" value="${esc(m.model)}" list="dl-${esc(m.provider)}" placeholder="model id" spellcheck="false"></td>
            <td class="res">${res}</td>
            <td class="acts"><button data-act="test" data-i="${i}" title="Test tool calling">Test</button><button data-act="up" data-i="${i}" title="Move up" ${i ? "" : "disabled"}>↑</button><button data-act="down" data-i="${i}" title="Move down" ${i < S.models.length - 1 ? "" : "disabled"}>↓</button><button data-act="del" data-i="${i}" title="Remove">✕</button></td></tr>`;
        }).join("")}</tbody>
      </table>
      ${S.providers.map((p) => `<datalist id="dl-${esc(p.name)}">${(S.lists[p.name] || []).map((m) => `<option value="${esc(m)}">`).join("")}</datalist>`).join("")}
      <div class="row gap">
        <button id="m-add">＋ Add model</button>
        <button id="m-testall">Test all enabled</button>
        <button id="m-enable">Enable all</button>
        <button id="m-disable">Disable all</button>
      </div>
      <details class="bulk"><summary>Add several at once</summary>
        <textarea id="m-bulk" rows="4" placeholder="one per line: provider:model (or just a model id for the provider below)" spellcheck="false"></textarea>
        <div class="row gap"><select id="m-bulk-prov">${provOpts(S.providers[0]?.name)}</select><button id="m-bulk-add">Add</button></div>
      </details>
    </div>

    <div class="panel">
      <div class="phead"><h3>Settings</h3><span class="muted small">Empty = default</span></div>
      <div class="settings">${Object.entries(S.schema).map(([k, sc]) => {
        const v = S.settings[k];
        const input = sc.type === "bool"
          ? `<select data-s="${k}"><option value="">default (${sc.default ? "yes" : "no"})</option><option value="true" ${v === true ? "selected" : ""}>yes</option><option value="false" ${v === false ? "selected" : ""}>no</option></select>`
          : `<input type="number" step="${sc.type === "int" ? 1 : "any"}" data-s="${k}" value="${v ?? ""}" placeholder="${sc.default}">`;
        return `<label class="setting"><span>${esc(human(k))}</span>${input}<small class="muted">${esc(sc.help)}</small></label>`;
      }).join("")}</div>
    </div>
  </div>`;
  wireSetup();
  drawActive();
}

function providerCard(p, i) {
  const used = S.models.filter((m) => m.provider === p.name).length;
  const keyState = p.type === "mock" ? "" : p.clear_key ? `<span class="badge warn">stored key will be removed</span>`
    : p.newKey ? `<span class="badge">new key, unsaved</span>`
    : p.has_key ? `<span class="badge win">key stored</span>`
    : p.key_env ? (p.env_present ? `<span class="badge win">$${esc(p.key_env)} set</span>` : `<span class="badge warn">$${esc(p.key_env)} not set</span>`)
    : `<span class="badge">no key</span>`;
  const n = S.lists[p.name]?.length;
  return `<div class="prov">
    <div class="row gap">
      <input class="pname" data-p="${i}" data-k="name" value="${esc(p.name)}" title="Provider name (used as prefix: name:model)" spellcheck="false">
      <select data-p="${i}" data-k="type"><option value="openai" ${p.type === "openai" ? "selected" : ""}>OpenAI-compatible</option><option value="mock" ${p.type === "mock" ? "selected" : ""}>Mock</option></select>
      ${keyState}
      <span class="muted small">${used} model${used === 1 ? "" : "s"}</span>
      <span style="margin-left:auto"></span>
      <button data-pact="list" data-i="${i}" title="Fetch the provider's model list (for autocomplete)">${n ? `Models (${n})` : "Fetch models"}</button>
      <button data-pact="del" data-i="${i}" title="Remove provider and its models">✕</button>
    </div>
    ${p.type === "mock" ? `<p class="muted small" style="margin:6px 0 0">Plays random (mostly illegal) moves offline. Any model id works.</p>` : `
    <div class="pgrid">
      <label>Base URL<input data-p="${i}" data-k="base_url" value="${esc(p.base_url)}" placeholder="https://host/v1" spellcheck="false"></label>
      <label>Key env var<input data-p="${i}" data-k="key_env" value="${esc(p.key_env)}" placeholder="(none)" spellcheck="false"></label>
      <label>API key <span class="muted">(stored in config)</span><input type="password" data-p="${i}" data-k="newKey" value="${esc(p.newKey)}" placeholder="${p.has_key ? "•••••• stored, type to replace" : "optional"}" autocomplete="off"></label>
      <label>Requests / min<input type="number" step="any" data-p="${i}" data-k="rpm" value="${esc(p.rpm)}" placeholder="setting rpm"></label>
    </div>
    <details class="adv"><summary>Advanced</summary>
      <div class="pgrid">
        <label>Extra headers (JSON)<input data-p="${i}" data-k="headers" value="${esc(p.headers)}" placeholder='{"HTTP-Referer": "..."}' spellcheck="false"></label>
        <label>Extra body fields (JSON, null removes a field)<input data-p="${i}" data-k="extra" value="${esc(p.extra)}" placeholder='{"max_tokens": null, "max_completion_tokens": 4096}' spellcheck="false"></label>
      </div>
      ${p.has_key ? `<label class="chk"><input type="checkbox" data-p="${i}" data-k="clear_key" ${p.clear_key ? "checked" : ""}> Remove stored key</label>` : ""}
    </details>`}
  </div>`;
}

function providerPayload(p) {
  const parse = (s, what) => { if (!s.trim()) return undefined; try { return JSON.parse(s); } catch { throw new Error(`${p.name}: ${what} is not valid JSON`); } };
  const o = { type: p.type };
  if (p.type !== "mock") {
    Object.assign(o, { base_url: p.base_url.trim(), key_env: p.key_env.trim() || undefined, rpm: p.rpm === "" ? undefined : +p.rpm,
      headers: parse(p.headers, "headers"), extra: parse(p.extra, "extra body") });
    if (p.newKey.trim()) o.api_key = p.newKey.trim();
    if (p.clear_key) o.clear_key = true;
  }
  return o;
}

function payload() {
  const names = S.providers.map((p) => p.name.trim());
  if (new Set(names).size !== names.length) throw new Error("provider names must be unique");
  const providers = {};
  S.providers.forEach((p) => (providers[p.name.trim()] = providerPayload(p)));
  const settings = {};
  for (const [k, v] of Object.entries(S.settings)) if (v !== "" && v !== undefined && v !== null) settings[k] = v;
  return { providers, models: S.models.map(({ provider, model, enabled }) => ({ provider, model: model.trim(), enabled })), settings };
}

function msg(text, kind = "") { const el = $("#s-msg"); if (el) { el.className = "msg " + kind; el.textContent = text; } }

async function save() {
  const body = payload();
  const empty = S.models.find((m) => !m.model.trim());
  if (empty) throw new Error("a model row has no model id");
  await api("config", "PUT", body);
  const keepMock = S.mock, lists = S.lists, tests = S.models.map((m) => m.test);
  await renderSetup();
  S.mock = keepMock; S.lists = lists; S.models.forEach((m, i) => (m.test = tests[i])); draw();
}

function wireSetup() {
  app.querySelectorAll("[data-p]").forEach((el) => {
    const ev = el.tagName === "SELECT" || el.type === "checkbox" ? "change" : "input";
    el.addEventListener(ev, () => {
      const p = S.providers[+el.dataset.p], k = el.dataset.k;
      const v = el.type === "checkbox" ? el.checked : el.value;
      if (k === "name") {
        const old = p.name; p.name = v.trim();
        S.models.forEach((m) => { if (m.provider === old) m.provider = p.name; });
        if (S.lists[old]) { S.lists[p.name] = S.lists[old]; }
      } else p[k] = v;
      markDirty();
      if (["type", "clear_key"].includes(k)) draw();
    });
    if (el.dataset.k === "name") el.addEventListener("change", draw);
  });
  app.querySelectorAll("[data-m]").forEach((el) => {
    const ev = el.tagName === "SELECT" || el.type === "checkbox" ? "change" : "input";
    el.addEventListener(ev, () => {
      const m = S.models[+el.dataset.m];
      m[el.dataset.k] = el.type === "checkbox" ? el.checked : el.value;
      delete m.test; markDirty();
      if (el.dataset.k !== "model") draw();
    });
  });
  app.querySelectorAll("[data-s]").forEach((el) => el.addEventListener("change", () => {
    const k = el.dataset.s, sc = S.schema[k];
    S.settings[k] = el.value === "" ? undefined : sc.type === "bool" ? el.value === "true" : +el.value;
    markDirty();
  }));
  app.querySelectorAll("[data-act]").forEach((b) => (b.onclick = () => {
    const i = +b.dataset.i, M = S.models;
    if (b.dataset.act === "test") return testModel(i);
    if (b.dataset.act === "up") [M[i - 1], M[i]] = [M[i], M[i - 1]];
    if (b.dataset.act === "down") [M[i + 1], M[i]] = [M[i], M[i + 1]];
    if (b.dataset.act === "del") M.splice(i, 1);
    markDirty(); draw();
  }));
  app.querySelectorAll("[data-pact]").forEach((b) => (b.onclick = async () => {
    const p = S.providers[+b.dataset.i];
    if (b.dataset.pact === "del") {
      const used = S.models.filter((m) => m.provider === p.name).length;
      if (used && !confirm(`Remove provider "${p.name}" and its ${used} model(s)?`)) return;
      S.providers.splice(+b.dataset.i, 1); S.models = S.models.filter((m) => m.provider !== p.name); markDirty(); return draw();
    }
    b.disabled = true; b.textContent = "Fetching…";
    try {
      const r = await api(`providers/${encodeURIComponent(p.name)}/models`, "POST", { draft: providerPayload(p) });
      S.lists[p.name] = r.models; draw(); msg(`${p.name}: ${r.models.length} models available (autocomplete in the model field).`, "ok");
    } catch (e) { draw(); msg(`${p.name}: ${e.message}`, "err"); }
  }));
  $("#p-preset").onchange = (e) => {
    if (e.target.value === "") return;
    const pr = PRESETS[+e.target.value];
    let name = pr.name, k = 2;
    while (S.providers.some((p) => p.name === name)) name = `${pr.name}${k++}`;
    S.providers.push({ name, _orig: null, type: pr.type || "openai", base_url: pr.base_url || "", key_env: pr.key_env || "", rpm: "",
      headers: "", extra: "", has_key: false, env_present: false, newKey: "", clear_key: false });
    markDirty(); draw();
  };
  $("#m-add").onclick = () => {
    const last = S.models[S.models.length - 1];
    S.models.push({ provider: last?.provider || S.providers[0]?.name, model: "", enabled: true });
    markDirty(); draw();
    const inputs = app.querySelectorAll('input[data-k="model"]'); inputs[inputs.length - 1]?.focus();
  };
  $("#m-bulk-add").onclick = () => {
    const prov = $("#m-bulk-prov").value;
    const names = new Set(S.providers.map((p) => p.name));
    for (const line of $("#m-bulk").value.split("\n").map((l) => l.trim()).filter(Boolean)) {
      const i = line.indexOf(":"), pre = line.slice(0, i);
      const [p, m] = i > 0 && names.has(pre) ? [pre, line.slice(i + 1)] : [prov, line];
      if (!S.models.some((x) => x.provider === p && x.model === m)) S.models.push({ provider: p, model: m, enabled: true });
    }
    markDirty(); draw();
  };
  $("#m-testall").onclick = () => S.models.forEach((m, i) => m.enabled && testModel(i));
  $("#m-enable").onclick = () => { S.models.forEach((m) => (m.enabled = true)); markDirty(); draw(); };
  $("#m-disable").onclick = () => { S.models.forEach((m) => (m.enabled = false)); markDirty(); draw(); };
  $("#s-mock").onchange = (e) => { S.mock = e.target.checked; draw(); };
  $("#s-reload").onclick = () => { if (!S.dirty || confirm("Discard unsaved changes?")) { S.dirty = false; renderSetup(); } };
  $("#s-save").onclick = async () => { try { await save(); msg("Saved.", "ok"); } catch (e) { msg(e.message, "err"); } };
  $("#s-start").onclick = async (e) => {
    e.target.disabled = true;
    try {
      await save();
      msg("Starting…");
      const r = await api("runs", "POST", { mock: S.mock });
      await loadRuns();
      go(r.id, "bracket");
    } catch (err) { msg(err.message, "err"); const b = $("#s-start"); if (b) b.disabled = false; }
  };
}

async function testModel(i) {
  const m = S.models[i];
  const p = S.providers.find((x) => x.name === m.provider);
  if (!m.model.trim() || !p) return;
  m.test = "pending"; draw();
  try { m.test = await api("test", "POST", { provider: p.name, model: m.model.trim(), draft: providerPayload(p) }); }
  catch (e) { m.test = { ok: false, info: e.message }; }
  if (location.hash.startsWith("#/setup")) draw();
}

async function drawActive() {
  const el = $("#active"); if (!el) return;
  let list;
  try { list = (await api("runs")).filter((r) => r.live); } catch { return; }
  el.innerHTML = list.length ? `<h3 style="margin-top:14px">Running</h3>` + list.map((r) =>
    `<div class="row gap"><a href="#/${encodeURIComponent(r.id)}/bracket">${esc(r.id)}</a><span class="badge live">${esc(r.state)}${r.status?.round ? ` · round ${r.status.round}` : ""}</span><span class="muted small">${r.games} games</span><button data-stop="${esc(r.id)}">Stop</button></div>`).join("") : "";
  el.querySelectorAll("[data-stop]").forEach((b) => (b.onclick = () => stopRun(b.dataset.stop).then(drawActive)));
}

async function stopRun(id) {
  if (!confirm(`Stop tournament ${id}? Games in progress are abandoned.`)) return;
  try { await api(`runs/${encodeURIComponent(id)}/stop`, "POST", {}); } catch (e) { alert(e.message); }
}

// ------------------------------------------------------------ run status panel (bracket page)

let logOpen = false;

function runPanel(run, data) {
  const st = data.info.status, state = data.info.state;
  if (!st && !data.info.has_log) return "";
  const pf = st?.preflight ? Object.entries(st.preflight) : [];
  const dropped = pf.filter(([, r]) => !r.ok);
  const elapsed = st ? Math.round(((st.finished || Date.now() / 1000) - st.started) / 60) : null;
  const cls = data.info.live ? "live" : state === "done" ? "win" : "warn";
  const head = `<span class="badge ${cls}">${esc(state)}${st?.round && data.info.live ? ` · round ${st.round}` : ""}</span>
    ${st ? `<span class="muted small">started ${new Date(st.started * 1000).toLocaleString()} · ${elapsed} min${st.mock ? " · mock" : ""}</span>` : ""}
    <span style="margin-left:auto"></span>${data.info.live ? `<button id="rp-stop">Stop</button>` : ""}`;
  const err = st?.error ? `<div class="msg err">${esc(st.error)}</div>` : "";
  const pfTable = pf.length && (dropped.length || state === "preflight") ? `<details ${state === "preflight" || dropped.length ? "open" : ""}><summary>Preflight: ${pf.length - dropped.length} ok, ${dropped.length} dropped</summary>
    <table class="small"><tbody>${pf.map(([m, r]) => `<tr><td>${nm(m)}</td><td><span class="badge ${r.ok ? "win" : "warn"}">${r.ok ? "ok" : "dropped"}</span></td><td class="muted">${esc(r.info)}</td></tr>`).join("")}</tbody></table></details>`
    : state === "preflight" ? `<p class="muted">Checking which models can make tool calls…</p>` : "";
  const log = data.info.has_log ? `<details id="rp-log" ${logOpen || ["failed", "crashed"].includes(state) ? "open" : ""}><summary>Process log</summary><pre class="runlog" id="rp-logtext">…</pre></details>` : "";
  return `<div class="panel runpanel"><div class="row gap">${head}</div>${err}${pfTable}${log}</div>`;
}

function wireRunPanel(run, data) {
  const stop = $("#rp-stop");
  if (stop) stop.onclick = () => stopRun(run).then(() => setTimeout(() => render(), 800));
  const det = $("#rp-log");
  if (!det) return;
  const load = async () => {
    try { const r = await api(`runs/${encodeURIComponent(run)}/log?n=300`); const pre = $("#rp-logtext"); if (pre) { const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4; pre.textContent = r.log || "(empty)"; if (atEnd) pre.scrollTop = pre.scrollHeight; } } catch {}
  };
  det.addEventListener("toggle", () => { logOpen = det.open; if (det.open) load(); });
  if (det.open) load();
}
