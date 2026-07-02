#!/usr/bin/env python3
"""
Caption Studio — a local web dashboard that turns story JSON into TikTok +
Instagram Reels captions you can copy or export.

Run it:
    python3 caption_dashboard.py                  # http://127.0.0.1:8788
    python3 caption_dashboard.py --port 9001
    python3 caption_dashboard.py --host 0.0.0.0    # reachable on your LAN

Then open the URL in your browser. Pick a story from the dropdown (auto-loaded
from output/scripts/) OR paste JSON directly, click Generate, and copy/download
the TikTok and Reels captions.
"""
import argparse
import glob
import json
from pathlib import Path

from aiohttp import web

from config import SCRIPTS_OUTPUT_DIR, OUTPUT_DIR
from script import ScriptGenerator
from generate_captions import PROMPT

CAPTIONS_DIR = OUTPUT_DIR / "captions"
_SG = None


def _sg() -> ScriptGenerator:
    global _SG
    if _SG is None:
        _SG = ScriptGenerator()
    return _SG


async def _captions_for(data: dict):
    """Core: story dict -> (title, caption_text). Raises ValueError on bad input."""
    if not isinstance(data, dict):
        raise ValueError("JSON must be an object (the story file).")
    title = data.get("seo_title") or data.get("title") or ""
    story = (data.get("full_text") or "").strip()
    if not story:
        raise ValueError("This JSON has no 'full_text' story field.")
    prompt = PROMPT.format(title=title, story=story[:1400])
    result = await _sg()._call_ai(prompt)
    if not result or not result.strip():
        raise RuntimeError("The AI returned nothing. Is your AI provider (Ollama) running?")
    return title, result.strip()


def _safe_name(title: str) -> str:
    safe = "".join(c for c in title if c.isalnum() or c in " -_").strip()
    return (safe.replace(" ", "_")[:60] or "caption")


# ─── Endpoints ────────────────────────────────────────────────────────────

async def index(request):
    return web.Response(text=HTML, content_type="text/html")


async def list_scripts(request):
    items = []
    for f in sorted(glob.glob(str(SCRIPTS_OUTPUT_DIR / "*.json")), reverse=True):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
            items.append({
                "path": f,
                "title": d.get("seo_title") or d.get("title") or Path(f).stem,
            })
        except Exception:
            continue
    return web.json_response(items)


async def generate(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request body."}, status=400)

    data = None
    if body.get("path"):
        p = Path(body["path"])
        try:
            if p.resolve().parent != Path(SCRIPTS_OUTPUT_DIR).resolve():
                return web.json_response({"error": "File must be in output/scripts/."}, status=400)
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            return web.json_response({"error": f"Could not read that file: {e}"}, status=400)
    elif body.get("raw_json"):
        try:
            data = json.loads(body["raw_json"])
        except Exception as e:
            return web.json_response({"error": f"That isn't valid JSON: {e}"}, status=400)
    else:
        return web.json_response({"error": "Pick a story or paste JSON first."}, status=400)

    try:
        title, captions = await _captions_for(data)
    except (ValueError, RuntimeError) as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"error": f"Generation failed: {e}"}, status=500)

    saved = None
    if title:
        CAPTIONS_DIR.mkdir(parents=True, exist_ok=True)
        out = CAPTIONS_DIR / f"{_safe_name(title)}_caption.txt"
        out.write_text(f"# {title}\n\n{captions}\n", encoding="utf-8")
        saved = str(out)

    return web.json_response({"title": title, "captions": captions, "saved": saved})


def build_app():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/scripts", list_scripts)
    app.router.add_post("/api/generate", generate)
    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    print(f"\n  Caption Studio running at  http://{args.host}:{args.port}\n")
    print("  Open that link in your browser. Ctrl+C to stop.\n")
    web.run_app(build_app(), host=args.host, port=args.port, print=None)


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>AI Nightmares — Caption Studio</title>
<style>
  :root{
    --bg:#0a0a0f; --panel:#13131c; --panel2:#1b1b27; --line:#2a2a3a;
    --text:#e8e8f0; --muted:#9a9ab0; --accent:#7c5cff; --accent2:#22d3ee;
    --ok:#34d399; --err:#f87171;
  }
  *{box-sizing:border-box}
  body{margin:0;background:radial-gradient(1200px 600px at 50% -10%,#1a1430 0%,var(--bg) 55%);
       color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;min-height:100vh}
  .wrap{max-width:880px;margin:0 auto;padding:32px 20px 80px}
  h1{font-size:26px;margin:0 0 4px;letter-spacing:.3px}
  h1 .g{background:linear-gradient(90deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}
  .sub{color:var(--muted);margin:0 0 28px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:18px}
  label{display:block;font-weight:600;margin:0 0 8px;font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
  select,textarea{width:100%;background:var(--panel2);color:var(--text);border:1px solid var(--line);
       border-radius:10px;padding:12px;font:inherit;outline:none}
  select:focus,textarea:focus{border-color:var(--accent)}
  textarea{min-height:140px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;resize:vertical}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .toggle{color:var(--accent2);cursor:pointer;font-size:13px;user-select:none;margin-top:10px;display:inline-block}
  button.gen{margin-top:16px;width:100%;padding:14px;border:0;border-radius:10px;cursor:pointer;
       font-size:16px;font-weight:700;color:#fff;background:linear-gradient(90deg,var(--accent),#a855f7)}
  button.gen:disabled{opacity:.55;cursor:default}
  .out{display:none}
  .block{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
  .block h3{margin:0 0 10px;font-size:14px;letter-spacing:.5px;display:flex;align-items:center;gap:8px}
  .pill{font-size:11px;padding:2px 8px;border-radius:999px;background:#2a2140;color:var(--accent2)}
  .block pre{white-space:pre-wrap;word-break:break-word;margin:0;font-family:inherit;font-size:14.5px}
  .copy{float:right;background:#241d3a;color:var(--accent2);border:1px solid var(--line);
       border-radius:8px;padding:5px 12px;cursor:pointer;font-size:12px;font-weight:600}
  .copy:hover{border-color:var(--accent2)}
  .bar{display:flex;gap:10px;margin-top:4px}
  .bar button{flex:1;padding:11px;border:1px solid var(--line);background:var(--panel2);color:var(--text);
       border-radius:10px;cursor:pointer;font-weight:600}
  .bar button:hover{border-color:var(--accent)}
  .msg{margin-top:12px;font-size:14px;min-height:20px}
  .msg.err{color:var(--err)} .msg.ok{color:var(--ok)}
  .spin{display:inline-block;width:15px;height:15px;border:2px solid #fff5;border-top-color:#fff;
       border-radius:50%;animation:s .7s linear infinite;vertical-align:-2px;margin-right:8px}
  @keyframes s{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="wrap">
  <h1><span class="g">AI Nightmares</span> — Caption Studio</h1>
  <p class="sub">Turn a story JSON into ready-to-paste TikTok &amp; Reels captions built to get watched and shared.</p>

  <div class="card">
    <label>1 · Choose a story</label>
    <select id="picker"><option value="">Loading your stories…</option></select>
    <span class="toggle" id="toggle">or paste JSON instead ↓</span>
    <div id="pasteWrap" style="display:none;margin-top:12px">
      <textarea id="raw" placeholder='Paste a story JSON here (must contain "full_text")…'></textarea>
    </div>
    <button class="gen" id="go">Generate captions</button>
    <div class="msg" id="msg"></div>
  </div>

  <div class="card out" id="out">
    <div class="block">
      <h3><span class="pill">TikTok</span>
        <button class="copy" data-t="tt">Copy</button></h3>
      <pre id="tt"></pre>
    </div>
    <div class="block">
      <h3><span class="pill">Reels</span>
        <button class="copy" data-t="rl">Copy</button></h3>
      <pre id="rl"></pre>
    </div>
    <div class="bar">
      <button id="copyAll">Copy everything</button>
      <button id="download">Download .txt</button>
    </div>
    <div class="msg" id="msg2"></div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
let lastTitle = "", lastRaw = "";

fetch('/api/scripts').then(r=>r.json()).then(items=>{
  const p = $('#picker');
  if(!items.length){ p.innerHTML='<option value="">No stories found in output/scripts/</option>'; return; }
  p.innerHTML = '<option value="">— select a story —</option>' +
    items.map(i=>`<option value="${i.path.replace(/"/g,'&quot;')}">${i.title.replace(/</g,'&lt;')}</option>`).join('');
}).catch(()=>{ $('#picker').innerHTML='<option value="">Could not load stories</option>'; });

$('#toggle').onclick = ()=>{
  const w=$('#pasteWrap'); const show=w.style.display==='none';
  w.style.display=show?'block':'none';
  $('#toggle').textContent = show ? 'use the dropdown instead ↑' : 'or paste JSON instead ↓';
};

function splitCaptions(text){
  let tt=text, rl="";
  const m=text.split(/REELS:/i);
  if(m.length>1){ tt=m[0]; rl=m[1]; }
  tt=tt.replace(/TIKTOK:/i,'').trim();
  rl=rl.trim();
  return [tt, rl||"(no Reels block returned)"];
}

$('#go').onclick = async ()=>{
  const path=$('#picker').value;
  const raw=$('#raw').value.trim();
  const payload = (raw && $('#pasteWrap').style.display!=='none') ? {raw_json:raw} : {path};
  if(!payload.path && !payload.raw_json){ setMsg('Pick a story or paste JSON first.','err'); return; }
  const btn=$('#go'); btn.disabled=true; btn.innerHTML='<span class="spin"></span>Generating…';
  setMsg('Talking to your AI…','');
  try{
    const r=await fetch('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d=await r.json();
    if(!r.ok){ setMsg(d.error||'Something went wrong.','err'); }
    else{
      const [tt,rl]=splitCaptions(d.captions);
      $('#tt').textContent=tt; $('#rl').textContent=rl;
      lastTitle=d.title||'captions'; lastRaw=d.captions;
      $('#out').style.display='block';
      setMsg(d.saved ? ('Saved to '+d.saved) : 'Done.','ok');
    }
  }catch(e){ setMsg('Request failed: '+e,'err'); }
  btn.disabled=false; btn.textContent='Generate captions';
};

document.querySelectorAll('.copy').forEach(b=>{
  b.onclick=()=>{ const t=$('#'+b.dataset.t).textContent; copy(t,b); };
});
$('#copyAll').onclick=()=>copy($('#tt').textContent+'\n\n'+$('#rl').textContent,$('#copyAll'));
$('#download').onclick=()=>{
  const blob=new Blob(['# '+lastTitle+'\n\n'+lastRaw+'\n'],{type:'text/plain'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
  a.download=(lastTitle.replace(/[^a-z0-9]+/gi,'_').slice(0,50)||'captions')+'.txt'; a.click();
};

function copy(text,btn){ navigator.clipboard.writeText(text).then(()=>{
  const o=btn.textContent; btn.textContent='Copied ✓'; setTimeout(()=>btn.textContent=o,1200);
}); }
function setMsg(t,c){ const m=$('#msg'); m.textContent=t; m.className='msg '+(c||''); }
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
