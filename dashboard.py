"""
Web dashboard for the Autonomous YouTube Soccer Content Agent.

A lightweight aiohttp control room. Read endpoints (status / queue / history /
logs / analytics) work straight off the JSON state files, so monitoring works
even if the heavier agent dependencies aren't installed. Action endpoints
(run / discover) import the agent lazily and run it inside this event loop.

Run it:
    python dashboard.py                 # http://127.0.0.1:8787
    python dashboard.py --port 9000 --host 0.0.0.0
    ./run.sh --dashboard
"""

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from aiohttp import web

from config import config, STATE_DIR, LOG_DIR, BASE_DIR
from storage import InstanceLock

logger = logging.getLogger("dashboard")

# Shared runtime state for the control room
RUN = {"running": False, "started_at": None}
START_TIME = datetime.now(timezone.utc)
_AGENT = None  # lazily created SoccerContentAgent


# ─── State file readers (dependency-free) ──────────────────────────────────

def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("could not read %s: %s", path, e)
    return default


def _queue():
    return _read_json(STATE_DIR / "content_queue.json", [])


def _history():
    return _read_json(STATE_DIR / "publication_history.json", [])


def _published_today(history):
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(1 for h in history if str(h.get("completed_at", "")).startswith(today))


# ─── Lazy agent ────────────────────────────────────────────────────────────

def _get_agent():
    """Import + build the agent on first use. Raises with a clear message."""
    global _AGENT
    if _AGENT is None:
        try:
            from agent import SoccerContentAgent
        except Exception as e:
            raise RuntimeError(
                f"Agent unavailable ({e}). Install requirements to enable runs."
            ) from e
        _AGENT = SoccerContentAgent()
    return _AGENT


# ─── HTTP handlers ─────────────────────────────────────────────────────────

async def index(request):
    html = (BASE_DIR / "dashboard.html").read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html")


async def api_status(request):
    history = _history()
    uptime = int((datetime.now(timezone.utc) - START_TIME).total_seconds())
    return web.json_response({
        "channel": config.upload.channel_name,
        "channel_words": config.upload.channel_name.split(),
        "running": RUN["running"],
        "dry_run": bool(config.scheduler.dry_run),
        "max_daily_uploads": config.scheduler.max_daily_uploads,
        "queue_size": len(_queue()),
        "total_published": len(history),
        "published_today": _published_today(history),
        "uptime_seconds": uptime,
        "uptime_formatted": str(timedelta(seconds=uptime)),
    })


async def api_queue(request):
    out = []
    for it in _queue():
        out.append({
            "title": it.get("title"),
            "category": it.get("category"),
            "source": it.get("source"),
            "status": it.get("status", "queued"),
            "score": it.get("relevance_score"),
        })
    return web.json_response(out)


async def api_history(request):
    limit = int(request.query.get("limit", 15))
    items = []
    for h in reversed(_history()[-limit:]):
        result = h.get("result") or {}
        items.append({
            "title": h.get("title"),
            "category": h.get("category"),
            "completed_at": h.get("completed_at"),
            "video_id": result.get("video_id") or h.get("video_id"),
        })
    return web.json_response(items)


async def api_analytics(request):
    data = _read_json(STATE_DIR / "analytics.json", {})
    return web.json_response(data)


async def api_variants(request):
    """Per-variant performance (built by the daily stats refresh) — which
    hook styles / endings / closings actually get watched."""
    data = _read_json(STATE_DIR / "variant_stats.json", {})
    return web.json_response(data)


async def api_logs(request):
    n = int(request.query.get("n", 120))
    log_file = LOG_DIR / f"agent_{datetime.now().strftime('%Y%m%d')}.log"
    lines = []
    if log_file.exists():
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        except Exception:
            pass
    return web.json_response({"lines": lines})


async def _do_run(dry_run: bool, lock: InstanceLock):
    """Background task: run one full content cycle (holds the instance lock)."""
    RUN["running"] = True
    RUN["started_at"] = datetime.now(timezone.utc).isoformat()
    prev = config.scheduler.dry_run
    try:
        config.scheduler.dry_run = dry_run
        agent = _get_agent()
        logger.info("dashboard: starting %s cycle", "dry-run" if dry_run else "live")
        await agent.run_once()
        logger.info("dashboard: cycle finished")
    except Exception as e:
        logger.error("dashboard: cycle failed: %s", e)
    finally:
        config.scheduler.dry_run = prev
        RUN["running"] = False
        lock.release()


async def api_run(request):
    if RUN["running"]:
        return web.json_response({"error": "A cycle is already running."}, status=409)
    body = await request.json() if request.can_read_body else {}
    dry = bool(body.get("dry_run", False))
    try:
        _get_agent()  # surface import errors before launching
    except Exception as e:
        return web.json_response({"error": str(e)}, status=503)
    # Cross-process lock: refuse if a CLI agent (or another dashboard) is
    # already running a cycle — prevents double uploads on shared state.
    lock = InstanceLock(STATE_DIR)
    if not lock.acquire():
        return web.json_response(
            {"error": "An agent process is already running (state/agent.lock)."},
            status=409)
    asyncio.create_task(_do_run(dry, lock))
    return web.json_response({"message": f"{'Dry run' if dry else 'Cycle'} started."})


async def api_discover(request):
    if RUN["running"]:
        return web.json_response({"error": "Busy running a cycle."}, status=409)
    try:
        agent = _get_agent()
    except Exception as e:
        return web.json_response({"error": str(e)}, status=503)
    try:
        items = await agent.discovery.get_top_stories(count=8)
        added = 0
        existing = {q.get("content_hash") for q in agent.state.queue}
        for it in items:
            if it.content_hash in existing:
                continue
            agent.state.add_to_queue({
                "title": it.title, "description": it.description,
                "source": it.source, "category": it.category,
                "content_hash": it.content_hash, "source_urls": it.source_urls,
                "relevance_score": it.relevance_score,
            })
            added += 1
        msg = f"Found {added} new topic{'s' if added != 1 else ''}." if added \
            else "No new topics found right now."
        return web.json_response({"message": msg, "added": added})
    except Exception as e:
        return web.json_response({"error": f"Discovery failed: {e}"}, status=500)


async def api_settings(request):
    body = await request.json() if request.can_read_body else {}
    if "dry_run" in body:
        config.scheduler.dry_run = bool(body["dry_run"])
    return web.json_response({"dry_run": config.scheduler.dry_run})


# ─── App factory ───────────────────────────────────────────────────────────

@web.middleware
async def error_middleware(request, handler):
    """Return the real error (and log a traceback) instead of an opaque 500."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise  # pass through intentional HTTP responses (404, 409, etc.)
    except Exception as e:
        import traceback
        logger.error("Unhandled error on %s %s:\n%s",
                     request.method, request.path, traceback.format_exc())
        return web.json_response(
            {"error": f"{type(e).__name__}: {e}"}, status=500)


def build_app(auth_token: str) -> web.Application:
    from localweb import security_middleware
    app = web.Application(middlewares=[security_middleware(auth_token),
                                       error_middleware])
    app.add_routes([
        web.get("/", index),
        web.get("/api/status", api_status),
        web.get("/api/queue", api_queue),
        web.get("/api/history", api_history),
        web.get("/api/analytics", api_analytics),
        web.get("/api/variants", api_variants),
        web.get("/api/logs", api_logs),
        web.post("/api/run", api_run),
        web.post("/api/discover", api_discover),
        web.post("/api/settings", api_settings),
    ])
    return app


def main():
    parser = argparse.ArgumentParser(description="Soccer agent dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-browser", action="store_true",
                        help="don't auto-open the browser")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    from storage import RedactingFilter
    for h in logging.getLogger().handlers:
        h.addFilter(RedactingFilter())

    # Browsers can't reach the server from a file:// page, so always open the
    # http URL ourselves (use a loopback host even if bound to 0.0.0.0).
    from localweb import make_token
    token = make_token()
    open_host = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    url = f"http://{open_host}:{args.port}/?token={token}"

    app = build_app(token)
    if not args.no_browser:
        async def _open(_app):
            import webbrowser
            try:
                webbrowser.open(url)
            except Exception:
                pass
        app.on_startup.append(_open)

    print(f"\n  Control room  →  open this exact URL (contains your access token):")
    print(f"  {url}")
    print("  (do NOT open dashboard.html directly)\n")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
