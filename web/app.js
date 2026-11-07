"use strict";
// NIM chess tournament frontend. Routes:
//   #/setup  (providers, models, settings, start)
//   #/<run>/bracket | #/<run>/games | #/<run>/standings | #/<run>/game/<n>[/<frame>]

const $ = (s, el = document) => el.querySelector(s);
const app = $("#app");
const GLYPH = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };
const LET = { king: "k", queen: "q", rook: "r", bishop: "b", knight: "n", pawn: "p" };
const VS = "︎"; // text presentation, keeps ♟ from turning into an emoji
const POLL_MS = 2500;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const short = (m) => (m ? m.split("/").pop() : "—");
const nm = (m) => `<span class="name" title="${esc(m)}">${esc(short(m))}</span>`;
const glyph = (piece, color) => `<span class="pc ${color === "white" ? "w" : "b"}">${GLYPH[LET[piece] || piece]}${VS}</span>`;
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
async function api(p, method = "GET", body) {
  const opt = { method, cache: "no-store" };
  if (body !== undefined) { opt.headers = { "Content-Type": "application/json" }; opt.body = JSON.stringify(body); }
  const r = await fetch("/api/" + p, opt);
  const d = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw new Error(d.error || r.status);
  return d;
}

let runs = [];
let runCache = {}; // id -> {info, bracket, games}
let timer = null;
let view = null;   // per-route cleanup / state

function route() {
  const [, run, tab = "bracket", n, frame] = location.hash.split("/");
  return { run: run && decodeURIComponent(run), tab, n: n && +n, frame: frame !== undefined ? +frame : undefined };
}
function go(run, tab, ...rest) { location.hash = ["#", encodeURIComponent(run), tab, ...rest].join("/"); }

async function loadRuns() {
  runs = await api("runs");
  const sel = $("#run-select");
  sel.innerHTML = (runs.length ? "" : `<option value="">no runs yet</option>`) +
    runs.map((r) => `<option value="${esc(r.id)}">${esc(r.id)}${r.live ? " ● live" : r.state === "done" ? "" : ` (${esc(r.state)})`}</option>`).join("");
}

async function render() {
  clearTimeout(timer); timer = null;
  if (view?.cleanup) view.cleanup();
  view = null;
  const setup = location.hash.startsWith("#/setup");
  $("#setup-link").classList.toggle("active", setup);
  if (setup) {
    document.querySelectorAll("#tabs a").forEach((a) => a.classList.remove("active"));
    $("#live-badge").hidden = true;
    loadRuns().catch(() => {});
    return renderSetup();
  }
  if (!runs.length) await loadRuns();
  if (!runs.length) { app.innerHTML = `<div class="pad"><h2>No results yet</h2><p class="muted">Start one from <a href="#/setup">New tournament</a>, or run <code>python3 -I chess_tourney.py --mock</code>.</p></div>`; return; }
  let { run, tab, n, frame } = route();
  if (run && !runs.find((r) => r.id === run)) await loadRuns();
  if (!run || !runs.find((r) => r.id === run)) { go(runs[0].id, "bracket"); return; }
  $("#run-select").value = run;
  document.querySelectorAll("#tabs a").forEach((a) => {
    a.classList.toggle("active", a.dataset.tab === tab || (tab === "game" && a.dataset.tab === "games"));
    a.href = `#/${encodeURIComponent(run)}/${a.dataset.tab}`;
  });
  const data = await api("runs/" + encodeURIComponent(run));
  runCache[run] = data;
  $("#live-badge").hidden = !data.info.live;
  if (tab === "game") return renderGame(run, n, frame);
  if (tab === "games") renderGames(run, data);
  else if (tab === "standings") renderStandings(run, data);
  else renderBracket(run, data);
  if (data.info.live) timer = setTimeout(refreshList, POLL_MS);
}

async function refreshList() {
  const { run, tab } = route();
  if (tab === "game" || location.hash.startsWith("#/setup")) return;
  try {
    const data = await api("runs/" + encodeURIComponent(run));
    runCache[run] = data;
    $("#live-badge").hidden = !data.info.live;
    const y = scrollY;
    if (tab === "games") renderGames(run, data, true);
    else if (tab === "standings") renderStandings(run, data);
    else renderBracket(run, data);
    scrollTo(0, y);
    if (data.info.live) timer = setTimeout(refreshList, POLL_MS);
  } catch { timer = setTimeout(refreshList, POLL_MS * 2); }
}

// ------------------------------------------------------------ bracket

function champBanner(data) {
  const c = data.info.champion;
  if (!c) return "";
  return `<div class="champ"><span class="trophy">🏆</span><div><div class="muted">Champion</div><b title="${esc(c)}">${esc(c)}</b></div></div>`;
}

function gameLinks(run, games) {
  return games.map((g) => {
    const res = g.winner ? (g.winner === g.white ? "1-0" : "0-1") : g.live ? "…" : "½";
    return `<a href="#/${encodeURIComponent(run)}/game/${g.n ?? g.game}" title="${esc(short(g.white))} vs ${esc(short(g.black))}: ${esc(g.reason || "in progress")}">g${g.n ?? g.game} ${res}</a>`;
  }).join(" · ");
}

function renderBracket(run, data) {
  const rounds = data.bracket?.rounds || [];
  const total = (data.bracket?.models || data.info.models || []).length;
  const recorded = new Set(rounds.flat().flatMap((m) => (m.games || []).map((g) => g.game)));
  // Games not yet in bracket.json belong to the round currently being played.
  const pending = data.games.filter((g) => !recorded.has(g.n));
  const live = new Map();
  for (const g of pending) {
    const key = [g.white, g.black].sort().join("|");
    if (!live.has(key)) live.set(key, { players: [g.white, g.black], games: [] });
    live.get(key).games.push(g);
  }
  let size = 1; while (size < total) size *= 2;
  const nameFor = (i, cnt) => (cnt === 1 ? "Final" : cnt === 2 ? "Semifinals" : cnt === 4 ? "Quarterfinals" : `Round ${i + 1}`);

  const cols = rounds.map((rnd, i) => `<div class="round"><h3>${nameFor(i, rnd.length)}</h3><div class="ms">${rnd.map((m) => matchCard(run, m)).join("")}</div></div>`);
  if (live.size) {
    const i = rounds.length;
    const cards = [...live.values()].map((m) => {
      const score = {}; m.players.forEach((p) => (score[p] = 0));
      m.games.forEach((g) => { if (g.live) return; if (g.winner) score[g.winner] += 1; else m.players.forEach((p) => (score[p] += 0.5)); });
      return matchCard(run, { players: m.players, score, winner: null, decided_by: "in progress", games: m.games });
    });
    cols.push(`<div class="round"><h3>${nameFor(i, Math.max(live.size, (rounds[i - 1]?.length || size) / 2 | 0))} <span class="badge live">live</span></h3><div class="ms">${cards.join("")}</div></div>`);
  }
  app.innerHTML = `${runPanel(run, data)}${champBanner(data)}${cols.length ? `<div class="bracket">${cols.join("")}</div>` : `<p class="muted pad">No games yet.</p>`}`;
  wireRunPanel(run, data);
}

function matchCard(run, m) {
  if (m.decided_by === "bye") {
    return `<div class="match bye"><div class="p w">${nm(m.winner)}<span class="score">bye</span></div></div>`;
  }
  const [a, b] = m.players;
  const row = (p) => `<div class="p ${m.winner ? (m.winner === p ? "w" : "l") : ""}">${nm(p)}<span class="score">${m.score?.[p] ?? ""}</span></div>`;
  const how = m.decided_by && m.decided_by !== "score" ? `<span class="badge ${m.decided_by === "in progress" ? "live" : "warn"}">${esc(m.decided_by)}</span>` : "";
  return `<div class="match">${row(a)}${row(b)}<div class="foot">${gameLinks(run, m.games || [])} ${how}</div></div>`;
}

// ------------------------------------------------------------ games list

function renderGames(run, data, keepFilter) {
  const q = keepFilter ? ($("#gfilter")?.value || "") : "";
  const rows = data.games.map((g) => {
    const res = g.live ? `<span class="badge live">live</span>` : g.winner ? (g.winner === g.white ? "1–0" : "0–1") : "½–½";
    return `<tr class="click" data-n="${g.n}" data-s="${esc((g.white + " " + g.black + " " + (g.reason || "")).toLowerCase())}">
      <td class="num">${g.n}</td><td>${nm(g.white)}</td><td>${nm(g.black)}</td><td>${res}</td>
      <td>${g.winner ? nm(g.winner) : g.live ? "" : "draw"}</td><td class="muted">${esc(g.reason || "")}</td></tr>`;
  }).join("");
  app.innerHTML = `<div class="toolbar"><h2 style="margin:0">Games</h2><input id="gfilter" type="search" placeholder="Filter by model or result…" value="${esc(q)}"></div>
    <table><thead><tr><th class="num">#</th><th>White</th><th>Black</th><th>Result</th><th>Winner</th><th>Reason</th></tr></thead><tbody>${rows}</tbody></table>`;
  const f = $("#gfilter");
  const apply = () => { const v = f.value.toLowerCase(); app.querySelectorAll("tbody tr").forEach((tr) => (tr.hidden = v && !tr.dataset.s.includes(v))); };
  f.oninput = apply; apply();
  app.querySelectorAll("tr.click").forEach((tr) => (tr.onclick = () => go(run, "game", tr.dataset.n)));
}

// ------------------------------------------------------------ standings

function renderStandings(run, data) {
  const t = {};
  const get = (m) => (t[m] ??= { games: 0, w: 0, d: 0, l: 0, illegal: data.bracket?.totals?.[m]?.illegal, out: null });
  (data.bracket?.models || data.info.models || []).forEach(get);
  for (const g of data.games) {
    if (g.live || !g.white) continue;
    for (const p of [g.white, g.black]) {
      const s = get(p); s.games++;
      if (!g.winner) s.d++; else if (g.winner === p) s.w++; else s.l++;
    }
  }
  (data.bracket?.rounds || []).forEach((rnd, i) => rnd.forEach((m) => m.players.forEach((p) => { if (p && p !== m.winner) get(p).out = i + 1; })));
  const champ = data.info.champion;
  const list = Object.entries(t).sort(([a, x], [b, y]) => (b === champ) - (a === champ) || (y.out ?? 99) - (x.out ?? 99) || y.w - x.w || x.l - y.l);
  const rows = list.map(([m, s], i) => {
    const pct = s.games ? (s.w + s.d / 2) / s.games : 0;
    const status = m === champ ? `<span class="badge win">champion</span>` : s.out ? `<span class="muted">out in round ${s.out}</span>` : `<span class="muted">alive</span>`;
    const ipm = s.illegal != null && s.games ? (s.illegal / s.games).toFixed(1) : "";
    return `<tr><td class="num">${i + 1}</td><td title="${esc(m)}">${esc(m)}</td><td>${status}</td><td class="num">${s.games}</td>
      <td class="num">${s.w}</td><td class="num">${s.d}</td><td class="num">${s.l}</td>
      <td><div class="bar" title="${(pct * 100).toFixed(0)}%"><i style="width:${pct * 100}%"></i></div></td>
      <td class="num">${s.illegal ?? "—"}</td><td class="num">${ipm}</td></tr>`;
  }).join("");
  app.innerHTML = `<h2>Standings</h2><table><thead><tr><th class="num">#</th><th>Model</th><th>Status</th><th class="num">Games</th>
    <th class="num">W</th><th class="num">D</th><th class="num">L</th><th>Score</th><th class="num">Odd moves</th><th class="num">per game</th></tr></thead><tbody>${rows}</tbody></table>
    <p class="muted">“Odd moves” are moves the harness flagged (wrong piece, empty square, own-piece capture, …). Updated after each completed round.</p>`;
}

// ------------------------------------------------------------ game viewer

const NOTE_LABEL = {
  from_empty: "from empty square", moved_opponent_piece: "moved opponent's piece", null_move: "null move",
  captured_own_piece: "captured own piece", en_passant: "en passant", castled: "castled",
};
const noteText = (n) => NOTE_LABEL[n] || (n.startsWith("piece_mismatch:") ? `square held a ${n.split(":")[1]}` : n.startsWith("promoted:") ? `promoted to ${n.split(":")[1]}` : n);
const isOdd = (f) => (f.notes || []).some((n) => !["castled", "en_passant"].includes(n) && !n.startsWith("promoted"));
const ACTION_LABEL = { resign: "resigns", declare_checkmate: "declares checkmate", oppose_checkmate: "opposes checkmate", accept_checkmate: "accepts checkmate" };

function frameLabel(f) {
  if (f.kind === "start") return "Start";
  if (f.kind === "move") {
    let s = `${glyph(f.piece, f.color)} ${f.frm}–${f.to}`;
    if (f.captured) s += ` ×${glyph(f.captured[1], f.captured[0])}`;
    if (f.actions?.includes("declare_checkmate")) s += " #?";
    return s;
  }
  const a = f.actions?.[0];
  return `<span class="act">${a ? ACTION_LABEL[a] : f.log.some((x) => x.type === "api_error") ? "API error" : "no move"}</span>`;
}

function capturedBy(frames, idx) {
  const out = { white: [], black: [] };
  for (let i = 1; i <= idx; i++) {
    const f = frames[i];
    if (f.kind === "move" && f.captured) out[f.color].push(f.captured);
  }
  return out;
}

async function renderGame(run, n, frameArg) {
  let data;
  try { data = await api(`runs/${encodeURIComponent(run)}/games/${n}`); }
  catch (e) { app.innerHTML = `<p class="pad">Could not load game ${n}: ${esc(e.message)}</p>`; return; }
  const st = { idx: frameArg ?? data.frames.length - 1, playing: false, flip: false, speed: 700 };
  st.idx = Math.max(0, Math.min(st.idx, data.frames.length - 1));
  let playTimer = null, pollTimer = null;
  const runData = runCache[run];
  const order = runData?.games.map((g) => g.n) || [];
  const pos = order.indexOf(n);
  const prevG = order[pos - 1], nextG = order[pos + 1];

  app.innerHTML = `
    <div class="toolbar">
      <a href="#/${encodeURIComponent(run)}/games">← Games</a>
      <h2 style="margin:0">Game ${n}</h2>
      <span id="g-live"></span>
      <span style="margin-left:auto"></span>
      ${prevG ? `<a href="#/${encodeURIComponent(run)}/game/${prevG}">‹ game ${prevG}</a>` : ""}
      ${nextG ? `<a href="#/${encodeURIComponent(run)}/game/${nextG}">game ${nextG} ›</a>` : ""}
    </div>
    <div class="game">
      <div>
        <div class="player" id="p-top"></div>
        <div class="board-wrap"><div class="board" id="board"></div></div>
        <div class="player" id="p-bot"></div>
        <div class="matbar" id="matbar" title="Material balance"><i></i></div>
        <input type="range" id="scrub" min="0" value="0">
        <div class="controls">
          <button id="b-first" title="Start (Home)">⏮</button>
          <button id="b-prev" title="Previous (←)">◀</button>
          <button id="b-play" title="Play / pause (Space)">▶</button>
          <button id="b-next" title="Next (→)">▶|</button>
          <button id="b-last" title="End (End)">⏭</button>
          <span class="pos" id="pos"></span>
          <button id="b-flip" title="Flip board (F)">⇅</button>
          <select id="speed" title="Playback speed"><option value="1500">0.5×</option><option value="700" selected>1×</option><option value="300">2×</option><option value="100">5×</option></select>
        </div>
        <div class="kbd">← → step · Space play · Home/End · F flip</div>
      </div>
      <div class="side">
        <div class="panel result" id="result"></div>
        <div class="panel"><h3>Moves</h3><div class="moves" id="moves"></div></div>
        <div class="panel"><h3 id="log-title">Model output</h3><div class="log" id="log"></div></div>
      </div>
    </div>`;

  const boardEl = $("#board");
  const scrub = $("#scrub");

  function drawBoard() {
    const f = data.frames[st.idx];
    const odd = f.kind === "move" && isOdd(f);
    let html = "";
    for (let vr = 0; vr < 8; vr++) {
      for (let vf = 0; vf < 8; vf++) {
        const r = st.flip ? vr : 7 - vr;      // r: 0 = rank 1
        const fi = st.flip ? 7 - vf : vf;
        const sq = "abcdefgh"[fi] + (r + 1);
        const ch = f.board[(7 - r) * 8 + fi];
        const cls = ["sq", (r + fi) % 2 ? "l" : "d"];
        if (f.kind === "move" && (sq === f.frm || sq === f.to)) cls.push(sq === f.frm ? "from" : "to", odd ? "odd" : "");
        let inner = ch === "." ? "" : `<span class="pc ${ch === ch.toUpperCase() ? "w" : "b"}">${GLYPH[ch.toLowerCase()]}${VS}</span>`;
        if (vf === 0) inner += `<span class="coord r">${r + 1}</span>`;
        if (vr === 7) inner += `<span class="coord f">${"abcdefgh"[fi]}</span>`;
        html += `<div class="${cls.join(" ")}" title="${sq}">${inner}</div>`;
      }
    }
    boardEl.innerHTML = html;
  }

  function drawPlayers() {
    const f = data.frames[st.idx];
    const caps = capturedBy(data.frames, st.idx);
    const next = f.kind === "start" ? "white" : f.color === "white" ? "black" : "white";
    const p = (color) => {
      const mat = f.material[color];
      const diff = mat - f.material[color === "white" ? "black" : "white"];
      return `<div class="who"><span class="dot ${color}"></span>${nm(data[color])}${data.result?.winner === color ? ` <span class="badge win">winner</span>` : ""}</div>
        <div><span class="caps">${caps[color].map(([c, pc]) => glyph(pc, c)).join("")}</span>
        <span class="muted">${diff > 0 ? "+" + diff : ""}</span></div>`;
    };
    const [top, bot] = st.flip ? ["white", "black"] : ["black", "white"];
    $("#p-top").innerHTML = p(top); $("#p-top").classList.toggle("to-move", !data.result && st.idx === data.frames.length - 1 && next === top);
    $("#p-bot").innerHTML = p(bot); $("#p-bot").classList.toggle("to-move", !data.result && st.idx === data.frames.length - 1 && next === bot);
    const w = f.material.white, b = f.material.black;
    $("#matbar i").style.width = (w + b ? (w / (w + b)) * 100 : 50) + "%";
  }

  function drawMoves() {
    $("#moves").innerHTML = data.frames.map((f, i) => {
      const cls = ["m", i === st.idx ? "cur" : "", f.kind === "move" && isOdd(f) ? "odd" : "", f.kind === "turn" ? "act" : ""].join(" ");
      const dot = f.color ? `<span class="dot ${f.color}" style="display:inline-block;width:8px;height:8px;border-radius:50%;border:1px solid #888;background:${f.color === "white" ? "#eee" : "#111"};margin-right:6px"></span>` : "";
      return `<div class="n">${i || ""}</div><div class="m ${cls}" data-i="${i}" style="grid-column: span 2">${dot}${frameLabel(f)}</div>`;
    }).join("");
    $("#moves").querySelectorAll(".m").forEach((el) => (el.onclick = () => setIdx(+el.dataset.i)));
    $("#moves .cur")?.scrollIntoView({ block: "nearest" });
  }

  function drawLog() {
    const f = data.frames[st.idx];
    $("#log-title").textContent = f.color ? `${cap(f.color)} · ${short(data[f.color])}` : "Model output";
    let notes = "";
    if (f.kind === "move" && f.notes?.length) notes = `<div class="notes">${f.notes.map((x) => `<span class="badge ${isOdd({ notes: [x] }) ? "warn" : ""}">${esc(noteText(x))}</span>`).join("")}</div>`;
    const items = f.log.map((x) => {
      if (x.type === "api_error") return `<div class="item err"><div class="meta">API error</div><pre>${esc(x.error)}</pre></div>`;
      const calls = (x.tool_calls || []).map(([name, a]) => `<div class="call">${esc(name)}(${esc(typeof a === "string" ? a : Object.entries(a || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", "))})</div>`).join("");
      return `<div class="item"><div class="meta"><span>step ${x.step + 1}</span>${x.latency ? `<span>${x.latency.toFixed(1)}s</span>` : ""}</div>
        ${x.content ? `<pre>${esc(x.content)}</pre>` : ""}${calls || (x.content ? "" : `<span class="muted">(empty response)</span>`)}</div>`;
    }).join("");
    $("#log").innerHTML = (notes ? `<div>${notes}</div>` : "") + (items || `<p class="muted" style="margin:0">${f.kind === "start" ? "Initial position." : "No model output recorded."}</p>`);
  }

  function drawResult() {
    const r = data.result;
    $("#g-live").innerHTML = r ? "" : `<span class="badge live">● live</span>`;
    if (!r) { $("#result").innerHTML = `<div class="muted">In progress — ${data.frames.length - 1} actions so far.</div>`; return; }
    const head = r.winner ? `<b>${esc(cap(r.winner))} wins</b> — ${nm(data[r.winner])}` : `<b>Draw</b>`;
    const mat = r.material ? ` · material ${r.material.white}–${r.material.black}` : "";
    $("#result").innerHTML = `${head}<div class="muted">${esc(r.reason)}${mat} · ${data.frames.length - 1} actions</div>`;
  }

  function draw() {
    scrub.max = data.frames.length - 1; scrub.value = st.idx;
    $("#pos").textContent = `${st.idx} / ${data.frames.length - 1}`;
    drawBoard(); drawPlayers(); drawMoves(); drawLog(); drawResult();
  }

  function setIdx(i, fromUrl) {
    st.idx = Math.max(0, Math.min(i, data.frames.length - 1));
    if (!fromUrl) history.replaceState(null, "", `#/${encodeURIComponent(run)}/game/${n}/${st.idx}`);
    draw();
  }

  function play(on) {
    st.playing = on; clearInterval(playTimer);
    $("#b-play").textContent = on ? "⏸" : "▶"; $("#b-play").classList.toggle("on", on);
    if (on) {
      if (st.idx >= data.frames.length - 1) setIdx(0);
      playTimer = setInterval(() => {
        if (st.idx >= data.frames.length - 1) { if (data.result) play(false); return; }
        setIdx(st.idx + 1);
      }, st.speed);
    }
  }

  $("#b-first").onclick = () => setIdx(0);
  $("#b-prev").onclick = () => setIdx(st.idx - 1);
  $("#b-next").onclick = () => setIdx(st.idx + 1);
  $("#b-last").onclick = () => setIdx(data.frames.length - 1);
  $("#b-play").onclick = () => play(!st.playing);
  $("#b-flip").onclick = () => { st.flip = !st.flip; draw(); };
  $("#speed").onchange = (e) => { st.speed = +e.target.value; if (st.playing) play(true); };
  scrub.oninput = () => setIdx(+scrub.value);

  const onKey = (e) => {
    if (e.target.matches("input, select, textarea")) return;
    const k = { ArrowLeft: () => setIdx(st.idx - 1), ArrowRight: () => setIdx(st.idx + 1), Home: () => setIdx(0),
      End: () => setIdx(data.frames.length - 1), " ": () => play(!st.playing), f: () => { st.flip = !st.flip; draw(); } }[e.key];
    if (k) { e.preventDefault(); k(); }
  };
  document.addEventListener("keydown", onKey);

  async function poll() {
    if (data.result) return;
    try {
      const atEnd = st.idx === data.frames.length - 1;
      data = await api(`runs/${encodeURIComponent(run)}/games/${n}`);
      if (atEnd && !st.playing) st.idx = data.frames.length - 1;
      draw();
    } catch { /* keep trying */ }
    if (!data.result) pollTimer = setTimeout(poll, POLL_MS);
  }
  if (!data.result) pollTimer = setTimeout(poll, POLL_MS);

  view = { cleanup() { clearInterval(playTimer); clearTimeout(pollTimer); document.removeEventListener("keydown", onKey); } };
  draw();
}

// ------------------------------------------------------------ boot

$("#run-select").onchange = (e) => e.target.value && go(e.target.value, ["game", undefined].includes(route().tab) || location.hash.startsWith("#/setup") ? "bracket" : route().tab);
window.addEventListener("hashchange", () => render().catch(showErr));
function showErr(e) { app.innerHTML = `<p class="pad">Error: ${esc(e.message)}</p>`; console.error(e); }
render().catch(showErr);
setInterval(() => loadRuns().then(() => { const { run } = route(); if (run) $("#run-select").value = run; }).catch(() => {}), 15000);
