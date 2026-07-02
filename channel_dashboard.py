#!/usr/bin/env python3
"""
Channel Control — a local dashboard to manage your automated YouTube channels.

Run:   python3 channel_dashboard.py
Open:  http://127.0.0.1:8800

It reads a channels.json next to this file (auto-created on first run). For each
channel it can: show running/stopped status, start or stop the scheduler,
generate one video now, and edit the common settings in that channel's .env.

Binds to 127.0.0.1 only — it is never exposed to your network. Even so, it can
read and write .env files (which hold secrets), so only run it on your own machine.
"""
import json
import os
import signal
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
CHANNELS_FILE = os.path.join(HERE, "channels.json")
PORT = 8800

# Settings surfaced in the quick-edit form (operational, non-secret).
QUICK_FIELDS = [
    ("CONTENT_MODE", "Content mode", "select", ["bible", "scifi", "horror", "soccer"]),
    ("CHANNEL_NAME", "Channel name", "text", None),
    ("MAX_DAILY_UPLOADS", "Max uploads per day", "number", None),
    ("CHECK_INTERVAL_MINUTES", "Minutes between uploads", "number", None),
    ("UPLOAD_PRIVACY", "Upload privacy", "select", ["private", "public", "unlisted"]),
    ("LOCAL_MODEL_NAME", "AI model", "text", None),
]

# All keys surfaced in the guided Setup panel (grouped in the UI).
ALL_ENV_KEYS = [
    "CONTENT_MODE", "CHANNEL_NAME", "UPLOAD_PRIVACY", "MAX_DAILY_UPLOADS",
    "CHECK_INTERVAL_MINUTES", "ASSET_PROVIDER",
    "LOCAL_MODEL_ENDPOINT", "LOCAL_MODEL_NAME", "OLLAMA_API_KEY",
    "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID",
    "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
    "POLLINATIONS_API_KEY", "PEXELS_API_KEY",
]


# ─────────────────────────────  channel config  ──────────────────────────────
def load_channels():
    if not os.path.exists(CHANNELS_FILE):
        template = [
            {"name": "AI Nightmares",
             "path": "/mnt/c/Users/lokot/Downloads/soccer-agent-fixed/soccer-agent-fixed"},
            {"name": "Daily Manna",
             "path": "/mnt/c/Users/lokot/Downloads/youtube-agent/youtube-agent"},
        ]
        with open(CHANNELS_FILE, "w") as f:
            json.dump(template, f, indent=2)
    with open(CHANNELS_FILE) as f:
        chans = json.load(f)
    for i, c in enumerate(chans):
        c["id"] = i
    return chans


# ─────────────────────────────  process status  ──────────────────────────────
def agent_processes():
    """Map each channel working-dir -> list of {pid, once} for live agent.py runs."""
    found = {}
    proc = "/proc"
    if not os.path.isdir(proc):
        return found
    for pid in os.listdir(proc):
        if not pid.isdigit():
            continue
        try:
            with open(f"{proc}/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
            if "agent.py" not in cmd or "dashboard" in cmd:
                continue
            cwd = os.readlink(f"{proc}/{pid}/cwd")
            found.setdefault(cwd, []).append(
                {"pid": int(pid), "once": "--once" in cmd})
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return found


def status_for(path):
    procs = agent_processes().get(os.path.normpath(path), [])
    scheduler = any(not p["once"] for p in procs)
    once = any(p["once"] for p in procs)
    state = "running" if scheduler else ("generating" if once else "stopped")
    return {"state": state, "pids": [p["pid"] for p in procs]}


# ─────────────────────────────  .env read / write  ───────────────────────────
def read_env(path):
    env = {}
    p = os.path.join(path, ".env")
    if not os.path.exists(p):
        return env
    for line in open(p, encoding="utf-8", errors="ignore"):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        env[k.strip()] = v.strip()
    return env


def update_env(path, updates):
    p = os.path.join(path, ".env")
    lines = []
    if os.path.exists(p):
        lines = open(p, encoding="utf-8", errors="ignore").read().splitlines()
    remaining = dict(updates)
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key = s.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    for k, v in remaining.items():
        out.append(f"{k}={v}")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def read_env_raw(path):
    """Return the raw .env text. If none exists, fall back to .env.example so a
    new user gets the template to fill in. Second value = whether a real .env exists."""
    p = os.path.join(path, ".env")
    if os.path.exists(p):
        return open(p, encoding="utf-8", errors="ignore").read(), True
    ex = os.path.join(path, ".env.example")
    if os.path.exists(ex):
        return open(ex, encoding="utf-8", errors="ignore").read(), False
    return "# No .env or .env.example found in this folder.\n", False


def write_env_raw(path, text):
    import shutil
    p = os.path.join(path, ".env")
    if os.path.exists(p):
        try:
            shutil.copy(p, p + ".backup")
        except Exception:
            pass
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return {"ok": True}


def auth_health(path):
    """Read the agent's channel_health.json (written on token refresh failures)."""
    p = os.path.join(path, "state", "channel_health.json")
    try:
        h = json.load(open(p))
        return h.get("auth", "unknown")
    except Exception:
        return "unknown"


def today_count(path):
    """Read state/publication_history.json and count today's (UTC) uploads."""
    import datetime
    p = os.path.join(path, "state", "publication_history.json")
    try:
        hist = json.load(open(p))
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        return sum(1 for h in hist if h.get("completed_at", "").startswith(today))
    except Exception:
        return None


def tail_log(path, n=40):
    p = os.path.join(path, "agent.log")
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            return "".join(f.readlines()[-n:])
    except FileNotFoundError:
        return "(no agent.log yet — start the scheduler or generate one to create it)"


# ─────────────────────────────  process actions  ─────────────────────────────
def spawn(path, once=False):
    if not os.path.isdir(path):
        return {"ok": False, "error": f"Folder not found: {path}"}
    if not os.path.exists(os.path.join(path, "agent.py")):
        return {"ok": False, "error": "agent.py not found in this folder"}
    log = open(os.path.join(path, "agent.log"), "a")
    args = [sys.executable, "agent.py"] + (["--once"] if once else [])
    try:
        subprocess.Popen(args, cwd=path, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def reset_today(path):
    """Remove today's (UTC) entries from publication_history.json so the daily
    cap resets. Backs the file up first; never touches verse/topic memory."""
    import datetime
    import shutil
    p = os.path.join(path, "state", "publication_history.json")
    try:
        hist = json.load(open(p))
    except Exception as e:
        return {"ok": False, "error": f"no upload history found ({e})"}
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    try:
        shutil.copy(p, p.replace(".json", ".backup.json"))
    except Exception:
        pass
    kept = [h for h in hist if not h.get("completed_at", "").startswith(today)]
    removed = len(hist) - len(kept)
    json.dump(kept, open(p, "w"), indent=2)
    return {"ok": True, "removed": removed}


def stop(path):
    pids = status_for(path)["pids"]
    for pid in pids:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
    return {"ok": True, "stopped": pids}


# ─────────────────────────────  HTTP handler  ────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client closed the connection (e.g. log auto-refresh) — harmless

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def log_message(self, *a):
        pass  # quiet

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/status":
            chans = load_channels()
            out = []
            for c in chans:
                env = read_env(c["path"])
                st = status_for(c["path"])
                out.append({
                    "id": c["id"], "name": c["name"], "path": c["path"],
                    "state": st["state"],
                    "mode": env.get("CONTENT_MODE", "?"),
                    "max_daily": env.get("MAX_DAILY_UPLOADS", "?"),
                    "interval": env.get("CHECK_INTERVAL_MINUTES", "?"),
                    "privacy": env.get("UPLOAD_PRIVACY", "?"),
                    "today": today_count(c["path"]),
                    "auth": auth_health(c["path"]),
                    "exists": os.path.isdir(c["path"]),
                })
            return self._json(out)
        if u.path == "/api/env":
            cid = int(parse_qs(u.query).get("id", [-1])[0])
            chans = load_channels()
            if 0 <= cid < len(chans):
                env = read_env(chans[cid]["path"])
                fields = {k: env.get(k, "") for k, *_ in QUICK_FIELDS}
                return self._json({"ok": True, "fields": fields})
            return self._json({"ok": False, "error": "bad id"}, 400)
        if u.path == "/api/allenv":
            cid = int(parse_qs(u.query).get("id", [-1])[0])
            chans = load_channels()
            if 0 <= cid < len(chans):
                env = read_env(chans[cid]["path"])
                fields = {k: env.get(k, "") for k in ALL_ENV_KEYS}
                return self._json({"ok": True, "fields": fields})
            return self._json({"ok": False, "error": "bad id"}, 400)
        if u.path == "/api/log":
            cid = int(parse_qs(u.query).get("id", [-1])[0])
            chans = load_channels()
            if 0 <= cid < len(chans):
                return self._json({"ok": True, "log": tail_log(chans[cid]["path"])})
            return self._json({"ok": False}, 400)
        if u.path == "/api/envraw":
            cid = int(parse_qs(u.query).get("id", [-1])[0])
            chans = load_channels()
            if 0 <= cid < len(chans):
                text, exists = read_env_raw(chans[cid]["path"])
                return self._json({"ok": True, "text": text, "exists": exists})
            return self._json({"ok": False}, 400)
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body or b"{}")
        except Exception:
            payload = {}
        chans = load_channels()
        cid = int(payload.get("id", -1))
        if not (0 <= cid < len(chans)):
            return self._json({"ok": False, "error": "bad id"}, 400)
        path = chans[cid]["path"]

        if u.path == "/api/start":
            return self._json(spawn(path, once=False))
        if u.path == "/api/once":
            return self._json(spawn(path, once=True))
        if u.path == "/api/stop":
            return self._json(stop(path))
        if u.path == "/api/reset":
            return self._json(reset_today(path))
        if u.path == "/api/envraw":
            text = payload.get("text", "")
            try:
                return self._json(write_env_raw(path, text))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if u.path == "/api/save":
            updates = {k: str(v) for k, v in payload.get("fields", {}).items()
                       if v != ""}
            try:
                update_env(path, updates)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        return self._json({"ok": False, "error": "unknown action"}, 404)


PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Channel Control</title>
<style>
:root{
  --bg:#0a0d12; --bg-glow:#10161f; --strip:#121821; --strip-2:#161d27;
  --edge:#222b38; --edge-soft:#1a212b;
  --ink:#eef3f9; --muted:#7e8a9a; --dim:#566071; --faint:#3a4452;
  --run:#3ddc97; --gen:#f5b945; --stop:#4a5462; --accent:#f3b75e; --danger:#e76d6d;
  --mono:ui-monospace,'SF Mono',SFMono-Regular,Menlo,Consolas,monospace;
  --sans:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
}
*{box-sizing:border-box}
html{-webkit-font-smoothing:antialiased}
body.light{
  --bg:#eef1f6; --bg-glow:#e3e9f1; --strip:#ffffff; --strip-2:#f6f8fb;
  --edge:#dde3ec; --edge-soft:#e7ecf3;
  --ink:#1b2433; --muted:#5a6678; --dim:#8893a4; --faint:#aeb8c6;
  --run:#13a06a; --gen:#c68a13; --stop:#bcc6d2; --accent:#c5821f; --danger:#d2544f;
}
body{margin:0;min-height:100vh;color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.5;
  background:
    radial-gradient(1100px 480px at 50% -8%, var(--bg-glow) 0%, rgba(16,22,31,0) 70%),
    var(--bg);}
.wrap{max-width:880px;margin:0 auto;padding:34px 22px 70px}

/* ── masthead ── */
.mast{display:flex;align-items:center;justify-content:space-between;padding-bottom:18px;border-bottom:1px solid var(--edge-soft)}
.brand{display:flex;align-items:center;gap:12px}
.sig{width:30px;height:30px;display:block}
.brand h1{font-size:19px;font-weight:700;letter-spacing:-.2px;margin:0}
.brand .tag{font-family:var(--mono);font-size:10.5px;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim);margin-top:2px}
.mast-right{display:flex;align-items:center;gap:18px;font-family:var(--mono);font-size:12px;color:var(--muted)}
.mast-right .live{display:flex;align-items:center;gap:7px}
.mast-right .live .d{width:7px;height:7px;border-radius:50%;background:var(--run);box-shadow:0 0 8px var(--run)}
#clock{color:var(--dim)}
.themebtn{font-family:var(--mono);font-size:15px;line-height:1;padding:6px 9px;border-radius:8px;
  background:transparent;border:1px solid var(--edge);color:var(--muted);cursor:pointer}
.themebtn:hover{border-color:var(--dim);color:var(--ink)}

/* ── summary ── */
.summary{display:flex;gap:26px;margin:18px 0 4px;font-family:var(--mono);font-size:12px;color:var(--muted)}
.summary b{color:var(--ink);font-weight:600}

/* ── channel strip ── */
.strip{position:relative;display:flex;background:linear-gradient(180deg,var(--strip) 0%,var(--strip-2) 100%);
  border:1px solid var(--edge);border-radius:16px;margin-top:18px;overflow:hidden;transition:border-color .2s,transform .2s}
.strip:hover{border-color:#2c3744}
.rail{width:4px;flex:none;background:var(--stop)}
.rail.running{background:var(--run);box-shadow:0 0 14px -1px var(--run)}
.rail.generating{background:var(--gen);box-shadow:0 0 14px -1px var(--gen)}
.body{flex:1;min-width:0;padding:18px 22px}

.head{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.state{display:inline-flex;align-items:center;gap:7px;font-family:var(--mono);font-size:11px;letter-spacing:.6px;
  text-transform:uppercase;color:var(--muted);padding:5px 11px;border:1px solid var(--edge);border-radius:30px;background:#0f141b}
.led{width:7px;height:7px;border-radius:50%;background:var(--stop)}
.led.running{background:var(--run);animation:pulse 1.9s infinite}
.led.generating{background:var(--gen);animation:pulse 1.3s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(61,220,151,.55)}70%{box-shadow:0 0 0 6px rgba(61,220,151,0)}100%{box-shadow:0 0 0 0 rgba(61,220,151,0)}}
.cname{font-size:19px;font-weight:700;letter-spacing:-.2px}
.cpath{font-family:var(--mono);font-size:11px;color:var(--faint);margin-left:auto;max-width:230px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* gauges */
.gauges{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;margin:16px 0 4px;background:var(--edge-soft);
  border:1px solid var(--edge-soft);border-radius:11px;overflow:hidden}
.g{background:#10151c;padding:11px 13px}
.g .k{font-family:var(--mono);font-size:9.5px;letter-spacing:1.2px;text-transform:uppercase;color:var(--dim);margin-bottom:5px}
.g .v{font-family:var(--mono);font-size:14px;color:var(--ink);font-weight:500}
.g .v.accent{color:var(--accent)}
.meter{height:4px;border-radius:3px;background:#202832;margin-top:7px;overflow:hidden}
.meter > i{display:block;height:100%;background:var(--run);border-radius:3px;transition:width .4s}
.meter > i.full{background:var(--gen)}
@media(max-width:620px){.gauges{grid-template-columns:repeat(2,1fr)}.cpath{display:none}}

/* actions */
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}
button{font-family:var(--sans);font-size:13px;font-weight:600;border-radius:9px;padding:9px 15px;
  border:1px solid var(--edge);background:#0f141b;color:var(--ink);cursor:pointer;transition:.15s}
button:hover{border-color:var(--dim);background:#131922}
button:disabled{opacity:.35;cursor:default}
.b-go{background:var(--run);color:#04130c;border-color:transparent}
.b-go:hover{filter:brightness(1.07);background:var(--run)}
.b-once{color:var(--accent);border-color:#3a3320}
.b-stop{color:var(--danger);border-color:#3a2525}
.b-ghost{color:var(--muted)}

/* drawer */
.drawer{max-height:0;overflow:hidden;transition:max-height .3s ease}
.drawer.open{max-height:2000px;overflow:visible}
.drawer-in{margin-top:16px;padding-top:16px;border-top:1px solid var(--edge-soft)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:13px 18px}
@media(max-width:620px){.grid{grid-template-columns:1fr}}
label{display:block;font-family:var(--mono);font-size:10px;letter-spacing:.8px;text-transform:uppercase;color:var(--dim);margin:0 0 6px}
input,select{width:100%;background:var(--bg);border:1px solid var(--edge);border-radius:8px;color:var(--ink);
  padding:9px 11px;font-size:14px;font-family:var(--sans)}
input:focus,select:focus{outline:none;border-color:var(--accent)}
.setupsec{margin-bottom:18px}
.sectitle{font-family:var(--mono);font-size:10.5px;letter-spacing:1.2px;text-transform:uppercase;color:var(--accent);margin:0 0 10px;padding-bottom:6px;border-bottom:1px solid var(--edge-soft)}
.fld{display:flex;flex-direction:column}
.fhelp{font-family:var(--mono);font-size:10px;color:var(--dim);margin-top:5px;line-height:1.4}
.secretwrap{position:relative;display:flex;align-items:center}
.secretwrap input{padding-right:56px}
.eye{position:absolute;right:6px;font-family:var(--mono);font-size:10.5px;padding:4px 8px;border-radius:6px;background:var(--panel);border:1px solid var(--edge);color:var(--muted);cursor:pointer}
.eye:hover{color:var(--ink);border-color:var(--dim)}
.savebar{display:flex;align-items:center;gap:14px;margin-top:18px}
.saved{color:var(--run);font-family:var(--mono);font-size:12px;opacity:0;transition:opacity .2s}
.saved.show{opacity:1}
.hint{font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:14px;line-height:1.6}
pre.log{margin:0;font-family:var(--mono);font-size:11.5px;line-height:1.55;color:#aeb9c7;background:#0b1016;
  border:1px solid var(--edge);border-radius:9px;padding:13px;max-height:300px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.missing{color:var(--danger);font-family:var(--mono);font-size:12px}
.authchip{font-family:var(--mono);font-size:11px;color:var(--danger);border:1px solid #3a2525;background:#1a1113;border-radius:20px;padding:4px 10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:340px}
.foot{color:var(--faint);font-size:11.5px;margin-top:30px;font-family:var(--mono);text-align:center;letter-spacing:.3px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<div class="wrap">
  <div class="mast">
    <div class="brand">
      <svg class="sig" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <circle cx="16" cy="16" r="15" stroke="#2c3744" stroke-width="1.5"/>
        <circle cx="16" cy="16" r="3" fill="#f3b75e"/>
        <path d="M16 6 L16 9 M16 23 L16 26 M6 16 L9 16 M23 16 L26 16" stroke="#3ddc97" stroke-width="1.6" stroke-linecap="round"/>
        <path d="M9 9 L11 11 M21 21 L23 23 M23 9 L21 11 M11 21 L9 23" stroke="#3a4452" stroke-width="1.4" stroke-linecap="round"/>
      </svg>
      <div>
        <h1>Channel Control</h1>
        <div class="tag">broadcast console</div>
      </div>
    </div>
    <div class="mast-right">
      <span class="live"><span class="d"></span><span id="runcount">—</span></span>
      <button class="themebtn" id="themeBtn" onclick="toggleTheme()" title="Switch theme">◐</button>
      <span id="clock">—</span>
    </div>
  </div>

  <div class="summary" id="summary"></div>
  <div id="list"></div>
  <div class="foot">refreshes every 4s · add channels in channels.json</div>
</div>
<script>
const $=(s,r=document)=>r.querySelector(s);
let openDrawer={};

async function api(path, body){
  const opt = body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt); return r.json();
}
const ledClass=s=>s==='running'?'running':(s==='generating'?'generating':'');
const stateLabel=s=>s==='running'?'On air':(s==='generating'?'Rendering':'Off');

let builtIds = null;
async function render(){
  const data = await api('/api/status');
  const running = data.filter(c=>c.state==='running').length;
  const totalToday = data.reduce((a,c)=>a+(typeof c.today==='number'?c.today:0),0);
  $('#runcount').textContent = running+' on air';
  $('#summary').innerHTML =
    `<span><b>${data.length}</b> channels</span>`+
    `<span><b>${running}</b> running</span>`+
    `<span><b>${totalToday}</b> published today</span>`;

  const ids = data.map(c=>c.id).join(',');
  const anyDrawerOpen = Object.values(openDrawer).some(v=>v);

  // Full build only the first time, or if the set of channels changed.
  // Never rebuild while a drawer is open (it would reset scroll + wipe inputs).
  if(builtIds !== ids && !anyDrawerOpen){
    builtIds = ids;
    $('#list').innerHTML = data.map(c=>stripHTML(c)).join('');
  } else {
    // Patch live values in place — no teardown, no scroll jump.
    data.forEach(patchStrip);
  }
}

function stripHTML(c){
  const led = ledClass(c.state);
  const cap = parseInt(c.max_daily)||0;
  const today = (typeof c.today==='number')?c.today:0;
  const pct = cap? Math.min(100, Math.round(today/cap*100)) : 0;
  const full = cap && today>=cap;
  const todayDisp = (c.today==null)?'—':`${c.today}${cap?'/'+cap:''}`;
  const missing = !c.exists ? `<span class="missing">folder not found — fix channels.json</span>` : '';
  const shortPath = c.path.split('/').slice(-2).join('/');
  return `<div class="strip" data-cid="${c.id}">
      <div class="rail ${led}" data-rail></div>
      <div class="body">
        <div class="head">
          <span class="state"><span class="led ${led}" data-led></span><span data-statelabel>${stateLabel(c.state)}</span></span>
          <span class="cname">${c.name}</span>
          <span class="authchip" data-auth style="${c.auth==='auth_dead'?'':'display:none'}">⚠ YouTube auth expired — update refresh token in Settings</span>
          <span class="cpath">…/${shortPath}</span>
        </div>
        <div class="gauges">
          <div class="g"><div class="k">Mode</div><div class="v accent" data-g="mode">${c.mode}</div></div>
          <div class="g"><div class="k">Per day</div><div class="v" data-g="max">${c.max_daily}</div></div>
          <div class="g"><div class="k">Interval</div><div class="v" data-g="interval">${c.interval}m</div></div>
          <div class="g"><div class="k">Privacy</div><div class="v" data-g="privacy">${c.privacy}</div></div>
          <div class="g"><div class="k">Today</div><div class="v" data-g="today">${todayDisp}</div>
            <div class="meter" ${cap?'':'style="display:none"'}><i data-meter class="${full?'full':''}" style="width:${pct}%"></i></div></div>
        </div>
        <div class="actions" data-actions>
          ${actionsHTML(c)}
          <span style="flex:1"></span>${missing}
        </div>
        <div class="drawer" id="drawer-${c.id}"></div>
      </div>
    </div>`;
}

function actionsHTML(c){
  return (c.state==='running'
      ? `<button class="b-stop" onclick="act(${c.id},'stop')">Stop scheduler</button>`
      : `<button class="b-go" onclick="act(${c.id},'start')" ${c.exists?'':'disabled'}>Start scheduler</button>`)
    + `<button class="b-once" onclick="act(${c.id},'once')" ${c.exists?'':'disabled'}>Generate one now</button>`
    + `<button class="b-ghost" onclick="toggle(${c.id},'settings')">Settings</button>`
    + `<button class="b-ghost" onclick="toggle(${c.id},'log')">Log</button>`
    + `<button class="b-ghost" onclick="resetToday(${c.id})" ${c.exists?'':'disabled'}>Reset today</button>`;
}

function patchStrip(c){
  const strip = document.querySelector(`.strip[data-cid="${c.id}"]`);
  if(!strip) return;
  const led = ledClass(c.state);
  const cap = parseInt(c.max_daily)||0;
  const today = (typeof c.today==='number')?c.today:0;
  const pct = cap? Math.min(100, Math.round(today/cap*100)) : 0;
  const full = cap && today>=cap;
  const todayDisp = (c.today==null)?'—':`${c.today}${cap?'/'+cap:''}`;
  const set=(sel,val)=>{const el=strip.querySelector(sel); if(el && el.textContent!==val) el.textContent=val;};
  strip.querySelector('[data-rail]').className = 'rail '+led;
  strip.querySelector('[data-led]').className = 'led '+led;
  set('[data-statelabel]', stateLabel(c.state));
  set('[data-g="mode"]', c.mode);
  set('[data-g="max"]', c.max_daily);
  set('[data-g="interval"]', c.interval+'m');
  set('[data-g="privacy"]', c.privacy);
  set('[data-g="today"]', todayDisp);
  const chip = strip.querySelector('[data-auth]');
  if(chip){ chip.style.display = (c.auth==='auth_dead') ? '' : 'none'; }
  const meter = strip.querySelector('[data-meter]');
  if(meter){ meter.style.width = pct+'%'; meter.className = full?'full':''; }
  // Only refresh the action buttons if the running-state changed (avoids nuking hover)
  const actions = strip.querySelector('[data-actions]');
  const isRunning = !!strip.querySelector('.b-stop');
  if(isRunning !== (c.state==='running')){
    const extra = actions.querySelector('span[style]');
    actions.innerHTML = actionsHTML(c);
    if(extra) actions.appendChild(extra);
  }
}

async function act(id, what){
  const map={start:'/api/start',stop:'/api/stop',once:'/api/once'};
  const res = await api(map[what], {id});
  if(res && res.error){ alert(res.error); }
  if(what==='once'){ openDrawer[id]='log'; }
  setTimeout(render, 400);
}

function toggle(id, which){
  openDrawer[id] = (openDrawer[id]===which) ? null : which;
  const d = $('#drawer-'+id);
  if(!openDrawer[id]){ d.classList.remove('open'); d.innerHTML=''; return; }
  fillDrawer(id, which);
}

async function fillDrawer(id, which){
  const d = $('#drawer-'+id);
  d.classList.add('open');
  if(which==='settings'){
    const r = await api('/api/allenv?id='+id); const f=r.fields||{};
    const esc = s => (s||'').replace(/"/g,'&quot;');
    const field=(g,fl)=>{
      const val = esc(f[fl.k]);
      const help = fl.help ? `<div class="fhelp">${fl.help}</div>` : '';
      if(fl.type==='select'){
        const o=fl.opts.map(v=>`<option ${f[fl.k]===v?'selected':''}>${v}</option>`).join('');
        return `<div class="fld"><label>${fl.label}</label><select id="f-${id}-${fl.k}">${o}</select>${help}</div>`;
      }
      if(fl.type==='secret'){
        return `<div class="fld"><label>${fl.label}</label>
          <div class="secretwrap">
            <input id="f-${id}-${fl.k}" type="password" value="${val}" autocomplete="off" spellcheck="false" placeholder="not set">
            <button type="button" class="eye" onclick="peek('f-${id}-${fl.k}',this)">show</button>
          </div>${help}</div>`;
      }
      return `<div class="fld"><label>${fl.label}</label><input id="f-${id}-${fl.k}" type="${fl.type==='number'?'number':'text'}" value="${val}" spellcheck="false">${help}</div>`;
    };
    const section = g => `
      <div class="setupsec">
        <div class="sectitle">${g.group}</div>
        <div class="grid">${g.fields.map(fl=>field(g,fl)).join('')}</div>
      </div>`;
    d.innerHTML = `<div class="drawer-in">
      ${SETUP.map(section).join('')}
      <div class="savebar">
        <button class="b-go" onclick="saveSetup(${id})">Save all settings</button>
        <span class="saved" id="saved-${id}">Saved · restart scheduler to apply</span>
      </div>
      <div class="hint">Everything is stored only in this channel's local .env file, on your machine. A backup is written on each save. Leave a field blank to keep its current value.</div>
      <div style="margin-top:12px">
        <button class="b-ghost" onclick="toggleRaw(${id})" id="rawbtn-${id}">Advanced: edit raw .env</button>
      </div>
      <div id="raw-${id}"></div>
    </div>`;
  } else {
    d.innerHTML = `<div class="drawer-in"><pre class="log" id="log-${id}">loading…</pre></div>`;
    refreshLog(id);
  }
}

async function refreshLog(id){
  if(openDrawer[id]!=='log') return;
  const r = await api('/api/log?id='+id);
  const el = $('#log-'+id);
  if(el){ el.textContent = r.log||'(empty)'; el.scrollTop = el.scrollHeight; }
  setTimeout(()=>refreshLog(id), 3000);
}

async function save(id){
  const keys=['CONTENT_MODE','CHANNEL_NAME','MAX_DAILY_UPLOADS','CHECK_INTERVAL_MINUTES','UPLOAD_PRIVACY','LOCAL_MODEL_NAME'];
  const fields={}; keys.forEach(k=>{const el=$(`#f-${id}-${k}`);if(el)fields[k]=el.value;});
  const r = await api('/api/save',{id,fields});
  const s=$('#saved-'+id);
  if(r.ok){ s.classList.add('show'); setTimeout(()=>s.classList.remove('show'),2500); }
  else alert(r.error||'save failed');
}

const SETUP = [
  {group:'Content', fields:[
    {k:'CONTENT_MODE', label:'Content mode', type:'select', opts:['bible','scifi','horror','soccer']},
    {k:'CHANNEL_NAME', label:'Channel name', type:'text'},
    {k:'UPLOAD_PRIVACY', label:'Upload privacy', type:'select', opts:['private','public','unlisted']},
    {k:'MAX_DAILY_UPLOADS', label:'Max uploads per day', type:'number'},
    {k:'CHECK_INTERVAL_MINUTES', label:'Minutes between uploads', type:'number'},
    {k:'ASSET_PROVIDER', label:'Visuals', type:'select', opts:['ai_image','pexels','generated']},
  ]},
  {group:'AI script model', fields:[
    {k:'LOCAL_MODEL_ENDPOINT', label:'Model endpoint', type:'text', help:'e.g. https://ollama.com/api/generate'},
    {k:'LOCAL_MODEL_NAME', label:'Model name', type:'text', help:'e.g. gemma4:31b'},
    {k:'OLLAMA_API_KEY', label:'Ollama API key', type:'secret', help:'from ollama.com/settings/keys'},
  ]},
  {group:'Voice — ElevenLabs (optional; free voice used if blank)', fields:[
    {k:'ELEVENLABS_API_KEY', label:'ElevenLabs API key', type:'secret'},
    {k:'ELEVENLABS_VOICE_ID', label:'Voice ID', type:'text', help:'from your ElevenLabs voice library'},
  ]},
  {group:'YouTube upload', fields:[
    {k:'YOUTUBE_CLIENT_ID', label:'Client ID', type:'secret'},
    {k:'YOUTUBE_CLIENT_SECRET', label:'Client secret', type:'secret'},
    {k:'YOUTUBE_REFRESH_TOKEN', label:'Refresh token', type:'secret', help:'starts with 1// — easiest: run python3 setup_oauth.py in the channel folder'},
  ]},
  {group:'Images (optional)', fields:[
    {k:'POLLINATIONS_API_KEY', label:'Pollinations key', type:'secret', help:'optional — reduces rate limits'},
    {k:'PEXELS_API_KEY', label:'Pexels key', type:'secret', help:'only if Visuals = pexels'},
  ]},
];

function peek(inputId, btn){
  const el = document.getElementById(inputId);
  if(!el) return;
  if(el.type==='password'){ el.type='text'; btn.textContent='hide'; }
  else { el.type='password'; btn.textContent='show'; }
}

async function saveSetup(id){
  const keys = SETUP.flatMap(g=>g.fields.map(f=>f.k));
  const fields={};
  keys.forEach(k=>{ const el=$(`#f-${id}-${k}`); if(el && el.value!=='') fields[k]=el.value; });
  const r = await api('/api/save',{id,fields});
  const s=$('#saved-'+id);
  if(r.ok){ s.classList.add('show'); setTimeout(()=>s.classList.remove('show'),2500); render(); }
  else alert(r.error||'save failed');
}

let rawOpen={};
async function toggleRaw(id){
  const box = $('#raw-'+id);
  rawOpen[id] = !rawOpen[id];
  if(!rawOpen[id]){ box.innerHTML=''; $('#rawbtn-'+id).textContent='Edit full .env (API keys)'; return; }
  $('#rawbtn-'+id).textContent='Hide .env';
  const r = await api('/api/envraw?id='+id);
  const note = r.exists ? '' : '<div class="hint" style="color:var(--gen)">No .env yet — this is the template. Fill in your keys and Save to create it.</div>';
  box.innerHTML = `
    <div class="hint" style="margin:10px 0">⚠ This file holds your API keys and tokens. Only you can see it (it lives on your machine). A backup is saved on each Save.</div>
    ${note}
    <textarea id="rawtext-${id}" spellcheck="false" style="width:100%;height:300px;background:var(--bg);border:1px solid var(--edge);border-radius:9px;color:var(--ink);padding:12px;font-family:var(--mono);font-size:12.5px;line-height:1.5;resize:vertical">${(r.text||'').replace(/</g,'&lt;')}</textarea>
    <div class="savebar">
      <button class="b-go" onclick="saveRaw(${id})">Save .env</button>
      <span class="saved" id="rawsaved-${id}">Saved · restart scheduler to apply</span>
    </div>`;
}
async function saveRaw(id){
  const text = $('#rawtext-'+id).value;
  const r = await api('/api/envraw',{id,text});
  const s=$('#rawsaved-'+id);
  if(r.ok){ s.classList.add('show'); setTimeout(()=>s.classList.remove('show'),2500); }
  else alert(r.error||'save failed');
}

setInterval(()=>{ $('#clock').textContent = new Date().toLocaleTimeString(); },1000);

function toggleTheme(){
  const light = document.body.classList.toggle('light');
  try{ localStorage.setItem('cc-theme', light?'light':'dark'); }catch(e){}
  $('#themeBtn').textContent = light ? '☀' : '◐';
}
(function initTheme(){
  let t='dark'; try{ t = localStorage.getItem('cc-theme')||'dark'; }catch(e){}
  if(t==='light'){ document.body.classList.add('light'); $('#themeBtn').textContent='☀'; }
})();

async function resetToday(id){
  if(!confirm("Reset today's upload count for this channel?\n\nThis clears today's entries so it can publish a fresh batch. A backup is saved automatically. Your verse/topic memory is not touched.")) return;
  const r = await api('/api/reset',{id});
  if(r && r.error){ alert(r.error); return; }
  if(r && typeof r.removed==='number'){ /* removed r.removed entries */ }
  setTimeout(render, 300);
}

render(); setInterval(render, 4000);
</script></body></html>"""


def main():
    load_channels()  # ensure channels.json exists
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("\n  Channel Control running")
    print(f"  → open http://127.0.0.1:{PORT} in your browser")
    print(f"  → channels file: {CHANNELS_FILE}")
    print("  → Ctrl+C to stop\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")


if __name__ == "__main__":
    main()
