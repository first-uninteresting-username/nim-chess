#!/usr/bin/env python3
"""Web frontend for chess_tourney.py.

Serves ./web and a JSON API to browse results, edit the shared config (providers,
models, settings in tourney.json) and start/stop tournaments. Tournaments run as
separate `chess_tourney.py` processes, so they keep going if the server stops,
and anything the frontend does can also be done from the command line.

Games are replayed with the same Board class the harness uses, so positions match
exactly. In-progress runs are polled live.

Usage:
  python3 -I serve.py                 # http://127.0.0.1:8000
  python3 -I serve.py --port 8080 --results /path/to/results --config /path/to/tourney.json
"""
import argparse, json, os, re, signal, subprocess, sys, threading, time
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from types import SimpleNamespace
from urllib.parse import urlparse, unquote, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chess_tourney as ct  # noqa: E402
from chess_tourney import Board, LETTER  # noqa: E402

WEB = os.path.join(HERE, "web")
SCRIPT = os.path.join(HERE, "chess_tourney.py")
GAME_RE = re.compile(r"^game_(\d{3,})\.jsonl$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
LIVE_WINDOW = 300  # runs without status.json: seconds since last write to count as live
SPECIAL = ("resign", "declare_checkmate", "oppose_checkmate", "accept_checkmate")
ACTIVE = ("starting", "preflight", "running")
CONFIG_LOCK = threading.Lock()
PROCS = {}  # run id -> Popen (only runs started by this server)


# ------------------------------------------------------------ game replay

def board_str(b):
    """64 chars, rank 8 to rank 1, files a-h. Uppercase = white, '.' = empty."""
    out = []
    for r in range(8, 0, -1):
        for f in "abcdefgh":
            p = b.sq.get(f"{f}{r}")
            out.append("." if not p else LETTER[p[1]].upper() if p[0] == "white" else LETTER[p[1]])
    return "".join(out)


def read_events(path):
    ev = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try: ev.append(json.loads(line))
            except json.JSONDecodeError: pass  # partially written last line of a live game
    return ev


def replay(path):
    ev = read_events(path)
    b = Board()
    start = next((e for e in ev if e["type"] == "start"), {})
    frames = [{"kind": "start", "board": board_str(b), "log": [],
               "material": {"white": b.material("white"), "black": b.material("black")}}]
    buf, buf_color = [], None

    def flush(kind="turn", **extra):
        nonlocal buf, buf_color
        actions = [n for x in buf for n, _ in (x.get("tool_calls") or []) if n in SPECIAL]
        frames.append({"kind": kind, "color": buf_color, "board": board_str(b), "log": buf,
                       "actions": actions,
                       "material": {"white": b.material("white"), "black": b.material("black")}, **extra})
        buf, buf_color = [], None

    result = None
    for e in ev:
        t = e["type"]
        if t in ("response", "api_error"):
            if buf and buf_color != e["color"]: flush()
            buf_color = e["color"]
            item = {"type": t, "content": e.get("content"), "latency": e.get("latency"),
                    "error": e.get("error"), "step": e.get("step"), "t": e.get("t")}
            calls = []
            for name, args in e.get("tool_calls") or []:
                try: a = json.loads(args) if args else {}
                except Exception: a = args
                calls.append([name, a])
            item["tool_calls"] = calls
            buf.append(item)
        elif t == "move":
            b.apply(e["color"], e["piece"], e["frm"], e["to"], e.get("promotion"))
            buf_color = e["color"]
            flush("move", piece=e["piece"], frm=e["frm"], to=e["to"], promotion=e.get("promotion"),
                  captured=e.get("captured"), notes=e.get("notes") or [])
        elif t == "result":
            if buf: flush()
            result = {"winner": e.get("winner"), "reason": e.get("reason"), "material": e.get("material")}
    if buf: flush()
    return {"white": start.get("white"), "black": start.get("black"), "frames": frames,
            "result": result, "started": start.get("t"), "updated": ev[-1]["t"] if ev else None}


def game_header(path):
    """Cheap per-game summary: first and last line only."""
    with open(path, "rb") as f:
        first = f.readline()
        try: f.seek(-4096, os.SEEK_END)
        except OSError: f.seek(0)
        tail = f.read().splitlines()
    try: start = json.loads(first)
    except Exception: start = {}
    last = None
    for line in reversed(tail):
        try: last = json.loads(line); break
        except Exception: continue
    res = last if last and last.get("type") == "result" else None
    return {"white": start.get("white"), "black": start.get("black"),
            "winner_color": res and res.get("winner"), "reason": res and res.get("reason"),
            "live": res is None}


# ------------------------------------------------------------ processes

def pid_alive(pid, run_dir):
    """True if pid is a chess_tourney process for this run dir (guards against pid reuse)."""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f: cmd = f.read().split(b"\0")
    except (OSError, ValueError, TypeError):
        return False
    cmd = [c.decode(errors="replace") for c in cmd]
    return any(c.endswith("chess_tourney.py") for c in cmd) and (
        os.path.basename(run_dir) in " ".join(cmd) or "--run-dir" not in cmd)


def read_status(run_dir):
    try:
        with open(os.path.join(run_dir, "status.json")) as f: st = json.load(f)
    except Exception:
        return None
    if st.get("state") in ACTIVE and not pid_alive(st.get("pid"), run_dir):
        p = PROCS.get(os.path.basename(run_dir))
        if not (p and p.poll() is None):
            st["state"] = "crashed"
    return st


def tail(path, n=200):
    try:
        with open(path, "rb") as f:
            try: f.seek(-64 * 1024, os.SEEK_END)
            except OSError: f.seek(0)
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


# ------------------------------------------------------------ HTTP

class ApiError(Exception):
    def __init__(self, code, msg): super().__init__(msg); self.code = code


class Handler(SimpleHTTPRequestHandler):
    results = None
    config = None

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def end_headers(self):
        if not self.path.startswith("/api/"): self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/") or self.command != "GET": super().log_message(fmt, *args)

    def send_json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        # Requiring a JSON content type forces a CORS preflight for cross-site requests,
        # which this server never answers; the Origin check covers the rest.
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise ApiError(415, "Content-Type must be application/json")
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            raise ApiError(403, "cross-origin request refused")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1 << 20: raise ApiError(413, "body too large")
        try: return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError as e: raise ApiError(400, f"bad JSON: {e}")

    # ---- runs

    def list_run_ids(self):
        if not os.path.isdir(self.results): return []
        return sorted((d for d in os.listdir(self.results)
                       if os.path.isdir(os.path.join(self.results, d))), reverse=True)

    def run_dir(self, rid):
        if rid not in self.list_run_ids(): raise ApiError(404, "no such run")
        return os.path.join(self.results, rid)

    def run_info(self, rid):
        d = os.path.join(self.results, rid)
        files = os.listdir(d)
        games = sorted(f for f in files if GAME_RE.match(f))
        bracket = None
        if "bracket.json" in files:
            try:
                with open(os.path.join(d, "bracket.json")) as f: bracket = json.load(f)
            except Exception: pass
        mtime = max([os.path.getmtime(os.path.join(d, f)) for f in files] or [os.path.getmtime(d)])
        st = read_status(d)
        champion = (bracket and bracket.get("champion")) or (st and st.get("champion"))
        if st: live, state = st["state"] in ACTIVE, st["state"]
        else:
            live = not champion and time.time() - mtime < LIVE_WINDOW
            state = "done" if champion else "running" if live else "incomplete"
        return {"id": rid, "mock": bool(st.get("mock")) if st else rid.endswith("-mock"), "games": len(games),
                "champion": champion, "updated": mtime, "live": live, "state": state,
                "models": (bracket and bracket.get("models")) or (st and st.get("models")),
                "status": st, "has_log": "run.log" in files}, bracket, games

    def get_run(self, rid):
        d = self.run_dir(rid)
        info, bracket, games = self.run_info(rid)
        gl = []
        for g in games:
            h = game_header(os.path.join(d, g))
            w = h.pop("winner_color")
            h["winner"] = h[w] if w else None
            gl.append({"n": int(GAME_RE.match(g).group(1)), **h})
        return {"info": info, "bracket": bracket, "games": gl}

    def start_run(self, req):
        with CONFIG_LOCK: cfg = ct.load_config(self.config)
        mock = bool(req.get("mock"))
        models = ct.players(cfg)
        if len(models) < 2: raise ApiError(400, "enable at least 2 models")
        if len(set(models)) != len(models): raise ApiError(400, "the model list has duplicates")
        if not mock:
            used = {ct.split_player(m, cfg["providers"])[0] for m in models}
            missing = [n for n in used if cfg["providers"][n].get("type") != "mock"
                       and cfg["providers"][n].get("key_env") and not ct.provider_key(cfg["providers"][n])]
            if missing: raise ApiError(400, "no API key for provider(s): " + ", ".join(missing))
            all_mock = all(cfg["providers"][n].get("type") == "mock" for n in used)
        else:
            all_mock = True
        base = datetime.now().strftime("%Y%m%d-%H%M%S") + ("-mock" if all_mock else "")
        rid, i = base, 1
        while os.path.exists(os.path.join(self.results, rid)): i += 1; rid = f"{base}-{i}"
        d = os.path.join(self.results, rid)
        os.makedirs(d)
        cmd = [sys.executable, "-I", "-u", SCRIPT, "--config", self.config, "--out", self.results, "--run-dir", d]
        if mock: cmd.append("--mock")
        log = open(os.path.join(d, "run.log"), "w")
        log.write("$ " + " ".join(cmd) + "\n\n"); log.flush()
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             cwd=HERE, start_new_session=True)
        log.close()
        PROCS[rid] = p
        threading.Thread(target=p.wait, daemon=True).start()  # reap
        return {"id": rid, "pid": p.pid}

    def stop_run(self, rid):
        d = self.run_dir(rid)
        st = read_status(d)
        p = PROCS.get(rid)
        pid = (st or {}).get("pid") or (p and p.pid)
        if not pid or not (pid_alive(pid, d) or (p and p.poll() is None)):
            raise ApiError(409, "run is not running")
        os.kill(pid, signal.SIGTERM)
        return {"stopped": rid}

    # ---- config

    def get_config(self):
        with CONFIG_LOCK: cfg = ct.load_config(self.config)
        pub = ct.public_config(cfg)
        for p in pub["providers"].values():
            p["env_present"] = bool(p.get("key_env") and os.environ.get(p["key_env"]))
        return {"config": pub, "path": self.config, "exists": os.path.exists(self.config),
                "effective": ct.effective_settings(cfg),
                "schema": {k: {"default": d, "type": t.__name__, "help": h} for k, (d, t, h) in ct.SETTINGS.items()}}

    def put_config(self, req):
        with CONFIG_LOCK:
            old = ct.load_config(self.config)
            provs = req.get("providers")
            if not isinstance(provs, dict) or not provs: raise ApiError(400, "providers must be a non-empty object")
            clean = {}
            for name, p in provs.items():
                if not NAME_RE.match(name): raise ApiError(400, f"bad provider name {name!r} (letters, digits, _ . -)")
                typ = p.get("type", "openai")
                if typ not in ("openai", "mock"): raise ApiError(400, f"provider {name}: unknown type {typ}")
                q = {"type": typ}
                if typ == "openai":
                    url = str(p.get("base_url") or "").strip()
                    if not re.match(r"^https?://", url): raise ApiError(400, f"provider {name}: base_url must start with http(s)://")
                    q["base_url"] = url
                    if p.get("key_env"): q["key_env"] = str(p["key_env"]).strip()
                    if p.get("rpm"): q["rpm"] = float(p["rpm"])
                    for k in ("headers", "extra"):
                        if p.get(k):
                            if not isinstance(p[k], dict): raise ApiError(400, f"provider {name}: {k} must be a JSON object")
                            q[k] = p[k]
                    # API key: new value replaces, clear_key drops, otherwise keep the stored one
                    if p.get("api_key"): q["api_key"] = str(p["api_key"]).strip()
                    elif not p.get("clear_key") and old["providers"].get(name, {}).get("api_key"):
                        q["api_key"] = old["providers"][name]["api_key"]
                clean[name] = q
            models = []
            for m in req.get("models") or []:
                prov, model = str(m.get("provider", "")), str(m.get("model", "")).strip()
                if prov not in clean: raise ApiError(400, f"model {model}: unknown provider {prov}")
                if not model: raise ApiError(400, "empty model id")
                models.append({"provider": prov, "model": model, "enabled": bool(m.get("enabled", True))})
            settings = {}
            for k, v in (req.get("settings") or {}).items():
                if k not in ct.SETTINGS or v in (None, ""): continue
                try: settings[k] = ct.SETTINGS[k][1](v)
                except (TypeError, ValueError): raise ApiError(400, f"setting {k}: bad value {v!r}")
            ct.save_config(self.config, {"providers": clean, "models": models, "settings": settings})
        return self.get_config()

    def provider_from(self, req, name):
        """Saved provider, optionally overlaid with unsaved edits from the request (keeping the stored key)."""
        with CONFIG_LOCK: cfg = ct.load_config(self.config)
        p = dict(cfg["providers"].get(name) or {})
        draft = req.get("draft")
        if draft:
            key = p.get("api_key")
            p = {k: v for k, v in draft.items() if k in ("type", "base_url", "key_env", "headers", "extra", "rpm", "api_key")}
            if not p.get("api_key") and key and not draft.get("clear_key"): p["api_key"] = key
        if not p: raise ApiError(404, f"no such provider {name}")
        p.setdefault("type", "openai")
        if p["type"] != "mock" and not p.get("base_url"): raise ApiError(400, "base_url missing")
        return cfg, p

    def provider_models(self, name, req):
        _, p = self.provider_from(req, name)
        try: return {"models": ct.list_models(p)}
        except Exception as e: raise ApiError(502, f"{type(e).__name__}: {e}"[:300])

    def test_model(self, req):
        name, model = req.get("provider"), str(req.get("model") or "").strip()
        if not name or not model: raise ApiError(400, "provider and model required")
        cfg, p = self.provider_from(req, name)
        s = SimpleNamespace(**{**ct.effective_settings(cfg), "timeout": min(ct.effective_settings(cfg)["timeout"], 90)})
        router = ct.Router({name: p}, s)
        if p.get("type") != "mock" and p.get("key_env") and not ct.provider_key(p):
            return {"ok": False, "info": f"no API key (set ${p['key_env']} or store a key)"}
        ok, info = ct.check_model(router, f"{name}:{model}", retries=0)
        return {"ok": ok, "info": info}

    # ---- dispatch

    def route(self, method):
        u = urlparse(self.path)
        parts = [p for p in unquote(u.path).split("/") if p][1:]
        if method == "GET":
            if parts == ["runs"]: return [self.run_info(r)[0] for r in self.list_run_ids()]
            if parts == ["config"]: return self.get_config()
            if len(parts) == 2 and parts[0] == "runs": return self.get_run(parts[1])
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "log":
                n = int((parse_qs(u.query).get("n") or ["300"])[0])
                return {"log": tail(os.path.join(self.run_dir(parts[1]), "run.log"), n)}
            if len(parts) == 4 and parts[0] == "runs" and parts[2] == "games" and parts[3].isdigit():
                p = os.path.join(self.run_dir(parts[1]), f"game_{int(parts[3]):03d}.jsonl")
                if not os.path.isfile(p): raise ApiError(404, "no such game")
                return replay(p)
        else:
            req = self.body()
            if method == "PUT" and parts == ["config"]: return self.put_config(req)
            if method == "POST":
                if parts == ["runs"]: return self.start_run(req)
                if len(parts) == 3 and parts[0] == "runs" and parts[2] == "stop": return self.stop_run(parts[1])
                if parts == ["test"]: return self.test_model(req)
                if len(parts) == 3 and parts[0] == "providers" and parts[2] == "models":
                    return self.provider_models(parts[1], req)
        raise ApiError(404, "not found")

    def handle_api(self, method):
        try: self.send_json(self.route(method))
        except ApiError as e: self.send_json({"error": str(e)}, e.code)
        except (ValueError, json.JSONDecodeError) as e: self.send_json({"error": str(e)}, 400)
        except Exception as e: self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_GET(self):
        if self.path.startswith("/api/"): return self.handle_api("GET")
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"): return self.handle_api("POST")
        self.send_error(405)

    def do_PUT(self):
        if self.path.startswith("/api/"): return self.handle_api("PUT")
        self.send_error(405)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address; the API can start processes and store API keys, keep it local")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--results", default=ct.DEFAULT_RESULTS)
    p.add_argument("--config", default=ct.DEFAULT_CONFIG)
    a = p.parse_args()
    Handler.results = os.path.abspath(a.results)
    Handler.config = os.path.abspath(a.config)
    os.makedirs(Handler.results, exist_ok=True)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"Serving {Handler.results} at http://{a.host}:{a.port} (config: {Handler.config})", flush=True)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass


if __name__ == "__main__":
    main()
