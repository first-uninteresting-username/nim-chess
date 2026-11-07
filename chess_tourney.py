#!/usr/bin/env python3
"""Single-elimination chess tournament between LLMs, played via tool calls.

Models are served by "providers": any OpenAI-compatible /chat/completions endpoint
(NVIDIA NIM by default, OpenRouter, OpenAI, a local vLLM/Ollama, ...) or the
built-in "mock" provider (random offline player). A player is "provider:model";
a bare model id uses the first provider in the config.

House rules (enforced by the harness, never explained to the models):
  * Moves are applied as stated, no legality checks. The declared piece is placed
    on the target square and the source square is cleared. Mismatches are logged only.
  * Castling (king moving two files) also moves the rook, en passant removes the
    passed pawn, and a pawn reaching the last rank promotes (queen by default).
  * Checkmate: one side calls declare_checkmate. The other side then gets a turn
    with oppose_checkmate / accept_checkmate. Anything other than opposing loses.
  * Nothing else ends a game (a captured king is just a captured piece).
  * Ply cap: adjudicated on material, equal material = draw.
  * A player with N consecutive turns without a move (no tool call, API errors)
    forfeits.

Config (providers, model list, settings) lives in tourney.json next to this script
and is shared with the web frontend (serve.py). Command-line flags override it.

Usage:
  NVIDIA_API_KEY=nvapi-... python3 -I chess_tourney.py            # preflight + tournament
  NVIDIA_API_KEY=nvapi-... python3 -I chess_tourney.py --preflight-only
  python3 -I chess_tourney.py --mock                               # offline test, random players

  # providers and models (add --save-config to store them in tourney.json)
  python3 -I chess_tourney.py --provider openrouter=https://openrouter.ai/api/v1 \\
      --models nvidia:google/gemma-4-31b-it openrouter:qwen/qwen3-32b
  python3 -I chess_tourney.py --provider local=http://127.0.0.1:11434/v1 --key-env local= --save-config
  python3 -I chess_tourney.py --list-models openrouter
  python3 -I chess_tourney.py --show-config
"""
import argparse, copy, json, os, random, re, signal, sys, threading, time, traceback
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://integrate.api.nvidia.com/v1"
DEFAULT_CONFIG = os.path.join(HERE, "tourney.json")
DEFAULT_RESULTS = os.path.join(HERE, "results")

DEFAULT_PROVIDERS = {
    "nvidia": {"type": "openai", "base_url": BASE, "key_env": "NVIDIA_API_KEY"},
    "mock": {"type": "mock"},
}

# name: (default, type, help). Precedence: command line > config "settings" > default.
SETTINGS = {
    "games_per_match": (2, int, "games per match, colors alternate"),
    "max_deciders": (1, int, "extra games if a match is tied"),
    "max_plies": (200, int, "turns per game before material adjudication"),
    "max_steps": (5, int, "model calls per turn (view_board, nudges)"),
    "forfeit_after": (3, int, "consecutive turns without a move before forfeit"),
    "parallel": (2, int, "matches played concurrently per round"),
    "rpm": (35, float, "request rate limit per provider (a provider's own rpm wins)"),
    "timeout": (180, float, "HTTP timeout in seconds"),
    "max_tokens": (4096, int, "max completion tokens per call"),
    "temperature": (0.6, float, "sampling temperature"),
    "seed": (42, int, "seed for --shuffle and the mock players"),
    "shuffle": (False, bool, "random seeding instead of list order"),
    "preflight": (True, bool, "drop models that can't make a tool call before starting"),
}

# Models that responded decently in the earlier benchmark (chat-capable, TTFT < ~3 s).
# Ordered by measured speed. Preflight drops any that can't make tool calls.
DEFAULT_MODELS = [
    "nvidia/nemotron-3.5-super-vl-120b-a12b",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "poolside/laguna-xs-2.1",
    "nvidia/nemotron-3.5-bolt-e4b",
    "stepfun-ai/step-5-preview",
    "google/gemma-4-31b-it",
    "mistralai/mistral-large-4-1t-a52b",
    "meta/muse-glimmer-30b",
]

PIECES = ["king", "queen", "rook", "bishop", "knight", "pawn"]
LETTER = {"king": "k", "queen": "q", "rook": "r", "bishop": "b", "knight": "n", "pawn": "p"}
VALUE = {"king": 0, "queen": 9, "rook": 5, "bishop": 3, "knight": 3, "pawn": 1}
SQ = re.compile(r"^[a-h][1-8]$")

# ---------------------------------------------------------------- tools

T_VIEW = {"type": "function", "function": {
    "name": "view_board", "description": "Show the current board.",
    "parameters": {"type": "object", "properties": {}}}}
T_MOVE = {"type": "function", "function": {
    "name": "move",
    "description": "Move one of your pieces. State which piece, the square it moves from and the square it moves to.",
    "parameters": {"type": "object", "properties": {
        "piece": {"type": "string", "enum": PIECES},
        "from_square": {"type": "string", "description": "e.g. e2"},
        "to_square": {"type": "string", "description": "e.g. e4"},
        "promotion": {"type": "string", "enum": ["queen", "rook", "bishop", "knight"],
                      "description": "Optional. Piece to promote to."}},
        "required": ["piece", "from_square", "to_square"]}}}
T_RESIGN = {"type": "function", "function": {
    "name": "resign", "description": "Resign the game.",
    "parameters": {"type": "object", "properties": {}}}}
T_DECLARE = {"type": "function", "function": {
    "name": "declare_checkmate",
    "description": "Declare that you have checkmated your opponent. May be called right after your move in the same turn.",
    "parameters": {"type": "object", "properties": {}}}}
T_OPPOSE = {"type": "function", "function": {
    "name": "oppose_checkmate", "description": "Dispute your opponent's checkmate declaration.",
    "parameters": {"type": "object", "properties": {}}}}
T_ACCEPT = {"type": "function", "function": {
    "name": "accept_checkmate", "description": "Accept your opponent's checkmate declaration.",
    "parameters": {"type": "object", "properties": {}}}}

NORMAL_TOOLS = [T_VIEW, T_MOVE, T_RESIGN, T_DECLARE]
RESPONSE_TOOLS = [T_VIEW, T_OPPOSE, T_ACCEPT, T_RESIGN]

SYSTEM = ("You are playing a game of chess as {color} against another opponent. "
          "You act only through the tools. On your turn, make one move with the move tool.")

# ---------------------------------------------------------------- board

class Board:
    def __init__(self):
        self.sq = {}
        back = ["rook", "knight", "bishop", "queen", "king", "bishop", "knight", "rook"]
        for i, f in enumerate("abcdefgh"):
            self.sq[f"{f}1"] = ("white", back[i]); self.sq[f"{f}2"] = ("white", "pawn")
            self.sq[f"{f}8"] = ("black", back[i]); self.sq[f"{f}7"] = ("black", "pawn")
        self.last = None  # (color, piece, frm, to)

    def render(self, to_move):
        rows = ["  a b c d e f g h"]
        for r in range(8, 0, -1):
            cells = []
            for f in "abcdefgh":
                p = self.sq.get(f"{f}{r}")
                cells.append("." if not p else (LETTER[p[1]].upper() if p[0] == "white" else LETTER[p[1]]))
            rows.append(f"{r} {' '.join(cells)} {r}")
        rows.append("  a b c d e f g h")
        rows.append("Uppercase = White, lowercase = Black.")
        rows.append(f"{to_move.capitalize()} to move.")
        return "\n".join(rows)

    def apply(self, color, piece, frm, to, promotion=None):
        """Apply a move without legality checks. Returns (captured, notes)."""
        notes = []
        src = self.sq.get(frm)
        if src is None: notes.append("from_empty")
        elif src[0] != color: notes.append("moved_opponent_piece")
        elif src[1] != piece: notes.append(f"piece_mismatch:{src[1]}")
        if frm == to: notes.append("null_move")
        dst = self.sq.get(to)
        if dst and dst[0] == color: notes.append("captured_own_piece")
        captured = dst
        self.sq.pop(frm, None)
        placed = piece
        fr, tr = int(frm[1]), int(to[1])
        # en passant
        if piece == "pawn" and frm[0] != to[0] and dst is None and self.last:
            lc, lp, lf, lt = self.last
            if lp == "pawn" and lc != color and lt == f"{to[0]}{fr}" and abs(int(lf[1]) - int(lt[1])) == 2:
                captured = self.sq.pop(lt, None); notes.append("en_passant")
        # promotion
        if piece == "pawn" and tr == (8 if color == "white" else 1):
            placed = promotion or "queen"; notes.append(f"promoted:{placed}")
        self.sq[to] = (color, placed)
        # castling
        if piece == "king" and frm[0] == "e" and fr == tr and fr in (1, 8) and to[0] in "gc":
            rf, rt = (f"h{fr}", f"f{fr}") if to[0] == "g" else (f"a{fr}", f"d{fr}")
            if self.sq.get(rf) == (color, "rook"):
                self.sq[rt] = self.sq.pop(rf); notes.append("castled")
        self.last = (color, piece, frm, to)
        return captured, notes

    def material(self, color):
        return sum(VALUE[p] for c, p in self.sq.values() if c == color)

# ---------------------------------------------------------------- API client

class RateLimiter:
    def __init__(self, rpm):
        self.interval = 60.0 / rpm; self.lock = threading.Lock(); self.next = 0.0
    def wait(self):
        with self.lock:
            now = time.time(); t = max(now, self.next); self.next = t + self.interval
        if t > now: time.sleep(t - now)

def http_headers(key, extra=None):
    h = {"Content-Type": "application/json", "User-Agent": "nim-chess/1.0"}
    if key: h["Authorization"] = f"Bearer {key}"
    h.update(extra or {})
    return h

class OpenAIClient:
    """Any OpenAI-compatible /chat/completions endpoint."""
    def __init__(self, base_url, key, rpm, timeout, max_tokens, temperature, headers=None, extra=None):
        self.base, self.key = base_url.rstrip("/"), key
        self.timeout, self.max_tokens, self.temp = timeout, max_tokens, temperature
        self.headers, self.extra = headers or {}, extra or {}
        self.rl = RateLimiter(rpm)

    def chat(self, model, messages, tools, retries=4):
        body = {"model": model, "messages": messages, "tools": tools, "tool_choice": "auto",
                "max_tokens": self.max_tokens, "temperature": self.temp, "stream": False, **self.extra}
        body = {k: v for k, v in body.items() if v is not None}  # extra: {"field": null} removes a field
        last_err = None
        for attempt in range(retries + 1):
            self.rl.wait()
            t0 = time.time()
            try:
                req = urllib.request.Request(self.base + "/chat/completions", data=json.dumps(body).encode(),
                    headers=http_headers(self.key, self.headers))
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    d = json.load(r)
                return d["choices"][0]["message"], d.get("usage") or {}, time.time() - t0
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}: {e.read()[:200].decode(errors='replace')}"
                if e.code not in (429, 500, 502, 503, 504): break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            if attempt < retries: time.sleep(min(60, 5 * 2 ** attempt))
        raise RuntimeError(last_err)

class MockClient:
    """Offline random player for testing the harness."""
    def __init__(self, seed): self.rng = random.Random(seed); self.lock = threading.Lock()

    def chat(self, model, messages, tools, retries=0):
        with self.lock:
            r = self.rng.random()
            names = {t["function"]["name"] for t in tools}
            def call(name, args=None):
                return {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{self.rng.randrange(1 << 30)}",
                        "type": "function", "function": {"name": name, "arguments": json.dumps(args or {})}}]}, {}, 0.0
            if "oppose_checkmate" in names:
                return call("oppose_checkmate" if r < 0.8 else "accept_checkmate")
            if r < 0.05: return {"role": "assistant", "content": "I think e4.", "tool_calls": None}, {}, 0.0
            if r < 0.15 and messages[-1]["role"] != "tool": return call("view_board")
            if r < 0.16: return call("resign")
            if r < 0.18: return call("declare_checkmate")
            f = lambda: f"{self.rng.choice('abcdefgh')}{self.rng.randint(1, 8)}"
            return call("move", {"piece": self.rng.choice(PIECES), "from_square": f(), "to_square": f()})

# ---------------------------------------------------------------- providers & config

def provider_key(p):
    return p.get("api_key") or (os.environ.get(p["key_env"]) if p.get("key_env") else None)

def split_player(player, providers):
    """'provider:model' -> (provider, model). Bare ids (or unknown prefixes, e.g. 'llama3:8b')
    go to the first provider."""
    pre, sep, rest = player.partition(":")
    if sep and pre in providers: return pre, rest
    return next(iter(providers)), player

def list_models(p, timeout=30):
    if p.get("type") == "mock": return ["random-a", "random-b", "random-c", "random-d"]
    req = urllib.request.Request(p["base_url"].rstrip("/") + "/models",
                                 headers=http_headers(provider_key(p), p.get("headers")))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    items = d.get("data", d) if isinstance(d, dict) else d
    return sorted(m["id"] if isinstance(m, dict) else str(m) for m in items)

class Router:
    """Dispatches 'provider:model' players to one client per provider (own rate limit each)."""
    def __init__(self, providers, settings, force_mock=False):
        self.providers, self.s, self.force_mock = providers, settings, force_mock
        self.clients = {}; self.lock = threading.Lock()

    def client(self, name):
        with self.lock:
            if name not in self.clients:
                p = self.providers[name]
                if self.force_mock or p.get("type") == "mock":
                    self.clients[name] = MockClient(self.s.seed + len(self.clients))
                else:
                    self.clients[name] = OpenAIClient(p["base_url"], provider_key(p), p.get("rpm") or self.s.rpm,
                        self.s.timeout, self.s.max_tokens, self.s.temperature, p.get("headers"), p.get("extra"))
            return self.clients[name]

    def chat(self, player, messages, tools, retries=4):
        prov, model = split_player(player, self.providers)
        return self.client(prov).chat(model, messages, tools, retries)

def default_config():
    return {"providers": copy.deepcopy(DEFAULT_PROVIDERS),
            "models": [{"provider": "nvidia", "model": m, "enabled": True} for m in DEFAULT_MODELS],
            "settings": {}}

def load_config(path):
    cfg = default_config()
    if path and os.path.exists(path):
        with open(path) as f: user = json.load(f)
        for k in ("providers", "models", "settings"):
            if k in user: cfg[k] = user[k]
    cfg["providers"].setdefault("mock", {"type": "mock"})
    for name, p in cfg["providers"].items():
        p.setdefault("type", "openai")
        if p["type"] != "mock" and not p.get("base_url"): raise ValueError(f"provider {name}: base_url missing")
    for m in cfg["models"]: m.setdefault("enabled", True)
    return cfg

def save_config(path, cfg):
    """Atomic write, mode 0600 (stored API keys live here)."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f: json.dump(cfg, f, indent=2); f.write("\n")
    os.replace(tmp, path)

def public_config(cfg):
    """Config without secrets."""
    c = copy.deepcopy(cfg)
    for p in c["providers"].values():
        p["has_key"] = bool(p.pop("api_key", None))
    return c

def effective_settings(cfg, overrides=None):
    s = {k: d for k, (d, _, _) in SETTINGS.items()}
    for k, v in {**cfg.get("settings", {}), **(overrides or {})}.items():
        if k in SETTINGS and v is not None: s[k] = SETTINGS[k][1](v)
    return s

def players(cfg):
    return [f"{m['provider']}:{m['model']}" for m in cfg["models"] if m.get("enabled", True)]

class Status:
    """status.json in the run dir, read by the web frontend."""
    def __init__(self, outdir, **kw):
        self.path = os.path.join(outdir, "status.json"); self.lock = threading.Lock()
        self.d = {"state": "starting", "pid": os.getpid(), "started": time.time(), **kw}
        self.write()

    def update(self, **kw):
        with self.lock: self.d.update(kw, updated=time.time()); self.write()

    def write(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f: json.dump(self.d, f, indent=2)
        os.replace(tmp, self.path)

# ---------------------------------------------------------------- game

def other(c): return "black" if c == "white" else "white"

class Game:
    def __init__(self, client, white, black, args, log_path):
        self.client, self.args = client, args
        self.players = {"white": white, "black": black}
        self.board = Board()
        self.history = []          # human-readable action lines per ply
        self.no_move_streak = {"white": 0, "black": 0}
        self.stats = {c: {"calls": 0, "errors": 0, "latency": 0.0, "tokens": 0, "notes": 0,
                          "moves": 0, "no_tool": 0} for c in ("white", "black")}
        self.log = open(log_path, "w")
        self.result = None         # (winner_color or None, reason)

    def ev(self, **kw):
        kw["t"] = time.time(); self.log.write(json.dumps(kw) + "\n"); self.log.flush()

    def history_text(self):
        if not self.history: return "No moves yet."
        return "\n".join(f"{i + 1}. {h}" for i, h in enumerate(self.history))

    def run_turn(self, color, responding=False):
        """Returns dict: {moved, declared, resigned, opposed, accepted}."""
        out = {"moved": False, "declared": False, "resigned": False, "opposed": False, "accepted": False}
        tools = RESPONSE_TOOLS if responding else NORMAL_TOOLS
        if responding:
            task = "Your opponent has declared checkmate. Respond using the tools."
        else:
            task = "It is your turn."
        user = (f"You are {color.capitalize()}.\nGame so far:\n{self.history_text()}\n\n{task}")
        messages = [{"role": "system", "content": SYSTEM.format(color=color.capitalize())},
                    {"role": "user", "content": user}]
        st = self.stats[color]
        for step in range(self.args.max_steps):
            try:
                msg, usage, dt = self.client.chat(self.players[color], messages, tools)
                st["calls"] += 1; st["latency"] += dt; st["tokens"] += usage.get("completion_tokens", 0)
            except Exception as e:
                st["errors"] += 1
                self.ev(type="api_error", color=color, error=str(e)); return out
            calls = msg.get("tool_calls") or []
            self.ev(type="response", color=color, step=step, content=msg.get("content"),
                    tool_calls=[(c["function"]["name"], c["function"].get("arguments")) for c in calls], latency=dt)
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls} if calls
                            else {"role": "assistant", "content": msg.get("content") or ""})
            if not calls:
                st["no_tool"] += 1
                messages.append({"role": "user", "content": "Use the tools to act."}); continue
            done = False
            for c in calls:
                name = c["function"]["name"]
                try: a = json.loads(c["function"].get("arguments") or "{}") or {}
                except Exception: a = {}
                res = self.handle(color, name, a, out, done, responding)
                if res is None: done = True; res = "OK."
                elif res.startswith("__end__"): done = True; res = res[7:] or "OK."
                messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": res})
            if done: return out
        return out

    def handle(self, color, name, a, out, done, responding):
        """Returns tool result string; None or '__end__...' ends the turn."""
        if done and not (name == "declare_checkmate" and out["moved"] and not out["declared"]):
            return "Ignored: your turn is over."
        if name == "view_board":
            return self.board.render(color)
        if name == "resign":
            out["resigned"] = True; return None
        if responding:
            if name == "oppose_checkmate": out["opposed"] = True; return None
            if name == "accept_checkmate": out["accepted"] = True; return None
            return f"Unknown tool {name}."
        if name == "declare_checkmate":
            out["declared"] = True; return None
        if name == "move":
            piece = str(a.get("piece", "")).lower().strip()
            frm = str(a.get("from_square", "")).lower().strip()
            to = str(a.get("to_square", "")).lower().strip()
            promo = a.get("promotion"); promo = str(promo).lower() if promo else None
            if promo not in (None, "queen", "rook", "bishop", "knight"): promo = None
            if piece not in PIECES or not SQ.match(frm) or not SQ.match(to):
                return "Invalid arguments: piece must be one of " + ", ".join(PIECES) + "; squares like e2."
            captured, notes = self.board.apply(color, piece, frm, to, promo)
            self.stats[color]["notes"] += sum(1 for n in notes if n not in ("castled", "en_passant") and not n.startswith("promoted"))
            self.stats[color]["moves"] += 1
            line = f"{color.capitalize()}: {piece} {frm}-{to}"
            if captured and captured[0] != color: line += f", captures {captured[1]}"
            if any(n.startswith("promoted") for n in notes): line += f", promotes to {self.board.sq[to][1]}"
            if "castled" in notes: line += " (castles)"
            self.pending_line = line
            self.ev(type="move", color=color, piece=piece, frm=frm, to=to, promotion=promo,
                    captured=captured, notes=notes)
            out["moved"] = True
            return "__end__Move played."
        return f"Unknown tool {name}."

    def finish(self, winner, reason):
        self.result = (winner, reason)
        self.ev(type="result", winner=winner, reason=reason,
                material={"white": self.board.material("white"), "black": self.board.material("black")})

    def play(self):
        color = "white"
        self.ev(type="start", white=self.players["white"], black=self.players["black"])
        for ply in range(self.args.max_plies):
            self.pending_line = None
            out = self.run_turn(color)
            if out["resigned"]:
                self.history.append(f"{color.capitalize()} resigns.")
                return self.finish(other(color), "resignation")
            if out["moved"]:
                self.history.append(self.pending_line); self.no_move_streak[color] = 0
            else:
                self.no_move_streak[color] += 1
                if not out["declared"]:
                    self.history.append(f"{color.capitalize()}: (no move)")
                if self.no_move_streak[color] >= self.args.forfeit_after:
                    return self.finish(other(color), "forfeit (no moves)")
            if out["declared"]:
                self.history.append(f"{color.capitalize()} declares checkmate.")
                resp = self.run_turn(other(color), responding=True)
                if resp["opposed"]:
                    self.history.append(f"{other(color).capitalize()} opposes the checkmate declaration.")
                else:
                    how = "accepted" if resp["accepted"] else "resigned" if resp["resigned"] else "unopposed"
                    self.history.append(f"{other(color).capitalize()} does not oppose ({how}).")
                    return self.finish(color, f"checkmate ({how})")
            color = other(color)
        mw, mb = self.board.material("white"), self.board.material("black")
        if mw == mb: return self.finish(None, f"ply cap, equal material {mw}-{mb}")
        return self.finish("white" if mw > mb else "black", f"ply cap, material {mw}-{mb}")

    def write_summary(self, path):
        w, r = self.result
        with open(path, "w") as f:
            f.write(f"White: {self.players['white']}\nBlack: {self.players['black']}\n")
            f.write(f"Result: {(self.players[w] + ' (' + w + ')') if w else 'draw'} - {r}\n\n")
            f.write(self.history_text() + "\n\nFinal position:\n" + self.board.render("white").rsplit("\n", 1)[0] + "\n")
            f.write("\nStats: " + json.dumps(self.stats) + "\n")
        self.log.close()

# ---------------------------------------------------------------- tournament

def seed_order(n):
    """Standard bracket order for n (power of two) seeds, 1-indexed."""
    order = [1]
    while len(order) < n:
        m = len(order) * 2; order = [x for s in order for x in (s, m + 1 - s)]
    return order

class Tournament:
    def __init__(self, client, models, args, outdir, status=None):
        self.client, self.models, self.args, self.out, self.status = client, models, args, outdir, status
        self.game_no = 0; self.lock = threading.Lock()
        self.record = {"models": models, "rounds": [], "args": {k: v for k, v in vars(args).items()}}
        self.totals = {m: {"illegal": 0, "games": 0, "wins": 0} for m in models}

    def next_game(self):
        with self.lock: self.game_no += 1; return self.game_no

    def play_game(self, white, black):
        n = self.next_game()
        base = os.path.join(self.out, f"game_{n:03d}")
        print(f"  game {n}: {white} (W) vs {black} (B) ...", flush=True)
        g = Game(self.client, white, black, self.args, base + ".jsonl")
        try: g.play()
        except Exception as e:
            traceback.print_exc(); g.finish(None, f"harness error: {e}")
        g.write_summary(base + ".txt")
        w, reason = g.result
        winner = g.players[w] if w else None
        with self.lock:
            for c in ("white", "black"):
                t = self.totals[g.players[c]]; t["games"] += 1; t["illegal"] += g.stats[c]["notes"]
            if winner: self.totals[winner]["wins"] += 1
        print(f"  game {n}: {winner or 'draw'} — {reason} ({len(g.history)} actions)", flush=True)
        return {"game": n, "white": white, "black": black, "winner": winner, "reason": reason,
                "plies": len(g.history), "illegal": {g.players[c]: g.stats[c]["notes"] for c in g.players}}

    def play_match(self, hi, lo):
        """hi = higher seed. Returns (winner, match record)."""
        score = {hi: 0.0, lo: 0.0}; games = []; illegal = {hi: 0, lo: 0}
        colors = [(hi, lo), (lo, hi)]
        def one(i):
            g = self.play_game(*colors[i % 2]); games.append(g)
            for m in (hi, lo): illegal[m] += g["illegal"][m]
            if g["winner"]: score[g["winner"]] += 1
            else: score[hi] += 0.5; score[lo] += 0.5
        gpm = self.args.games_per_match
        for i in range(gpm):
            one(i)
            if abs(score[hi] - score[lo]) > gpm - (i + 1): break  # already decided
        d = 0
        while score[hi] == score[lo] and d < self.args.max_deciders:
            one(gpm + d); d += 1
        if score[hi] != score[lo]: winner, how = max(score, key=score.get), "score"
        elif illegal[hi] != illegal[lo]: winner, how = min(illegal, key=illegal.get), "tiebreak: fewer illegal moves"
        else: winner, how = hi, "tiebreak: higher seed"
        return winner, {"players": [hi, lo], "score": score, "winner": winner, "decided_by": how, "games": games}

    def run(self):
        n = len(self.models); size = 1
        while size < n: size *= 2
        slots = [self.models[s - 1] if s <= n else None for s in seed_order(size)]
        seed = {m: i + 1 for i, m in enumerate(self.models)}
        rnd = 1
        while len(slots) > 1:
            print(f"\n=== Round {rnd} ({len(slots)} slots) ===", flush=True)
            if self.status: self.status.update(state="running", round=rnd)
            pairs = [(slots[i], slots[i + 1]) for i in range(0, len(slots), 2)]
            results = [None] * len(pairs); jobs = []
            with ThreadPoolExecutor(self.args.parallel) as ex:
                for k, (a, b) in enumerate(pairs):
                    if a is None or b is None:
                        results[k] = (a or b, {"players": [a, b], "winner": a or b, "decided_by": "bye"})
                        print(f"  bye: {a or b}")
                    else:
                        hi, lo = (a, b) if seed[a] < seed[b] else (b, a)
                        jobs.append((k, ex.submit(self.play_match, hi, lo)))
                for k, fut in jobs: results[k] = fut.result()
            for w, m in results:
                if m["decided_by"] != "bye":
                    print(f"  >> {m['players'][0]} vs {m['players'][1]}: {w} advances ({m['decided_by']}, {m['score']})")
            self.record["rounds"].append([m for _, m in results])
            slots = [w for w, _ in results]; rnd += 1
            self.save()
        self.record["champion"] = slots[0]
        self.save()
        print(f"\nChampion: {slots[0]}")
        return slots[0]

    def save(self):
        self.record["totals"] = self.totals
        with open(os.path.join(self.out, "bracket.json"), "w") as f: json.dump(self.record, f, indent=2)
        lines = ["# NIM chess tournament", ""]
        for i, rnd in enumerate(self.record["rounds"], 1):
            lines.append(f"## Round {i}")
            for m in rnd:
                if m["decided_by"] == "bye": lines.append(f"- {m['winner']}: bye"); continue
                a, b = m["players"]
                lines.append(f"- **{m['winner']}** def. {b if m['winner'] == a else a} — "
                             f"{m['score'][a]}–{m['score'][b]} ({m['decided_by']})")
                for g in m["games"]:
                    lines.append(f"  - game {g['game']}: {g['white']} (W) vs {g['black']} (B): "
                                 f"{g['winner'] or 'draw'}, {g['reason']}, {g['plies']} actions")
            lines.append("")
        if "champion" in self.record: lines.append(f"**Champion: {self.record['champion']}**\n")
        lines.append("| Model | Games | Wins | Illegal/odd moves |\n|---|---|---|---|")
        for m, t in self.totals.items(): lines.append(f"| {m} | {t['games']} | {t['wins']} | {t['illegal']} |")
        with open(os.path.join(self.out, "summary.md"), "w") as f: f.write("\n".join(lines) + "\n")

# ---------------------------------------------------------------- preflight

PREFLIGHT_MSGS = [{"role": "system", "content": SYSTEM.format(color="White")},
                  {"role": "user", "content": "You are White.\nGame so far:\nNo moves yet.\n\nIt is your turn."}]

def check_model(client, player, retries=2):
    """One tool-only prompt. Returns (ok, info)."""
    try:
        msg, _, dt = client.chat(player, PREFLIGHT_MSGS, NORMAL_TOOLS, retries=retries)
        names = [c["function"]["name"] for c in (msg.get("tool_calls") or [])]
        return bool(names), f"{names or 'no tool call'} in {dt:.1f}s"
    except Exception as e:
        return False, str(e)[:200]

def preflight(client, models, skip=()):
    """Keep models that answer a tool-only prompt with a tool call. Returns (kept, report)."""
    ok, report = [], {}
    def check(m):
        return (m, True, "skipped (mock)") if m in skip else (m, *check_model(client, m))
    with ThreadPoolExecutor(4) as ex:
        for m, good, info in ex.map(check, models):
            print(f"  {'OK  ' if good else 'DROP'} {m}: {info}", flush=True)
            report[m] = {"ok": good, "info": info}
            if good: ok.append(m)
    return ok, report

# ---------------------------------------------------------------- CLI

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=DEFAULT_CONFIG, help="config file (default: %(default)s)")
    p.add_argument("--models", nargs="*", metavar="PROVIDER:MODEL",
                   help="replace the model list; seed order = list order (unless --shuffle)")
    p.add_argument("--add-model", nargs="*", default=[], metavar="PROVIDER:MODEL", help="append to the model list")
    p.add_argument("--remove-model", nargs="*", default=[], metavar="PROVIDER:MODEL", help="remove from the model list")
    p.add_argument("--provider", action="append", default=[], metavar="NAME=BASE_URL",
                   help="add or update an OpenAI-compatible provider; key read from $NAME_API_KEY unless --key-env")
    p.add_argument("--key-env", action="append", default=[], metavar="NAME=VAR",
                   help="env var holding the provider's API key (empty VAR = no key)")
    p.add_argument("--provider-rpm", action="append", default=[], metavar="NAME=RPM", help="per-provider rate limit")
    p.add_argument("--remove-provider", action="append", default=[], metavar="NAME")
    g = p.add_argument_group("settings (override the config file)")
    for k, (d, t, h) in SETTINGS.items():
        flag = "--" + k.replace("_", "-")
        if t is bool: g.add_argument(flag, action=argparse.BooleanOptionalAction, default=None, help=f"{h} (default {d})")
        else: g.add_argument(flag, type=t, default=None, help=f"{h} (default {d})")
    p.add_argument("--mock", action="store_true", help="use random offline players for every model, no API calls")
    p.add_argument("--preflight-only", action="store_true")
    p.add_argument("--save-config", action="store_true", help="write providers/models/settings to --config and exit")
    p.add_argument("--show-config", action="store_true", help="print the effective config (no secrets) and exit")
    p.add_argument("--list-models", metavar="PROVIDER", help="list models offered by a provider and exit")
    p.add_argument("--out", default=DEFAULT_RESULTS, help="results directory")
    p.add_argument("--run-dir", help="exact output directory for this run (default: OUT/<timestamp>)")
    return p.parse_args(argv)

def kv(items, what):
    out = {}
    for it in items:
        k, sep, v = it.partition("=")
        if not sep or not k: sys.exit(f"{what}: expected NAME=VALUE, got {it!r}")
        out[k] = v
    return out

def apply_cli(cfg, args):
    provs = cfg["providers"]
    for name, url in kv(args.provider, "--provider").items():
        p = provs.setdefault(name, {})
        p.update(type="openai", base_url=url)
        p.setdefault("key_env", re.sub(r"\W", "_", name).upper() + "_API_KEY")
    for name, var in kv(args.key_env, "--key-env").items():
        if name not in provs: sys.exit(f"--key-env: unknown provider {name}")
        provs[name]["key_env"] = var or None
    for name, rpm in kv(args.provider_rpm, "--provider-rpm").items():
        if name not in provs: sys.exit(f"--provider-rpm: unknown provider {name}")
        provs[name]["rpm"] = float(rpm)
    for name in args.remove_provider:
        provs.pop(name, None)
        cfg["models"] = [m for m in cfg["models"] if m["provider"] != name]
    if not provs: sys.exit("no providers left")
    def entry(s):
        prov, model = split_player(s, provs)
        return {"provider": prov, "model": model, "enabled": True}
    if args.models is not None: cfg["models"] = [entry(s) for s in args.models]
    cfg["models"] += [entry(s) for s in args.add_model]
    drop = {(e["provider"], e["model"]) for e in map(entry, args.remove_model)}
    cfg["models"] = [m for m in cfg["models"] if (m["provider"], m["model"]) not in drop]
    for m in cfg["models"]:
        if m["provider"] not in provs: sys.exit(f"model {m['model']}: unknown provider {m['provider']}")
    overrides = {k: getattr(args, k) for k in SETTINGS if getattr(args, k) is not None}
    if args.save_config: cfg["settings"] = {**cfg.get("settings", {}), **overrides}
    return overrides

def main(argv=None):
    args = parse_args(argv)
    try: cfg = load_config(args.config)
    except (ValueError, json.JSONDecodeError) as e: sys.exit(f"{args.config}: {e}")
    overrides = apply_cli(cfg, args)

    if args.save_config:
        save_config(args.config, cfg); print(f"Saved {args.config}"); return
    if args.show_config:
        print(json.dumps({**public_config(cfg), "effective_settings": effective_settings(cfg, overrides)}, indent=2)); return
    if args.list_models:
        if args.list_models not in cfg["providers"]: sys.exit(f"unknown provider {args.list_models}")
        try: print("\n".join(list_models(cfg["providers"][args.list_models])))
        except Exception as e: sys.exit(f"listing models failed: {e}")
        return

    s = SimpleNamespace(**effective_settings(cfg, overrides))
    models = players(cfg)
    if s.shuffle: random.Random(s.seed).shuffle(models)
    if len(set(models)) != len(models): sys.exit("duplicate models in the list")
    if len(models) < 2: sys.exit("Need at least 2 models")
    used = {split_player(m, cfg["providers"])[0] for m in models}
    mock_provs = {n for n, p in cfg["providers"].items() if p.get("type") == "mock"}
    all_mock = args.mock or used <= mock_provs
    if not args.mock:
        missing = [n for n in used - mock_provs if cfg["providers"][n].get("key_env") and not provider_key(cfg["providers"][n])]
        if missing:
            sys.exit("No API key for provider(s) " + ", ".join(f"{n} (set ${cfg['providers'][n]['key_env']})" for n in missing))
    client = Router(cfg["providers"], s, force_mock=args.mock)
    mock_players = set(models) if args.mock else {m for m in models if split_player(m, cfg["providers"])[0] in mock_provs}

    if args.preflight_only:
        preflight(client, models, mock_players); return

    outdir = args.run_dir or os.path.join(args.out, datetime.now().strftime("%Y%m%d-%H%M%S") + ("-mock" if all_mock else ""))
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "config.json"), "w") as f:
        json.dump({**public_config(cfg), "effective_settings": vars(s), "mock": args.mock}, f, indent=2)
    status = Status(outdir, entrants=list(models), models=models, mock=all_mock)

    def on_signal(signum, _frame):
        print(f"\nStopped by signal {signum}.", flush=True)
        status.update(state="stopped", finished=time.time())
        os._exit(130)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        if s.preflight and len(mock_players) < len(models):
            print("Preflight (tool calling):", flush=True)
            status.update(state="preflight")
            models, report = preflight(client, models, mock_players)
            status.update(preflight=report, models=models)
            if len(models) < 2:
                raise RuntimeError("fewer than 2 models passed preflight")
        print(f"Seeds: " + ", ".join(f"{i + 1}. {m}" for i, m in enumerate(models)))
        print(f"Output: {outdir}", flush=True)
        champ = Tournament(client, models, s, outdir, status).run()
        status.update(state="done", champion=champ, finished=time.time())
    except Exception as e:
        traceback.print_exc()
        status.update(state="failed", error=f"{type(e).__name__}: {e}", finished=time.time())
        sys.exit(1)

if __name__ == "__main__":
    main()
