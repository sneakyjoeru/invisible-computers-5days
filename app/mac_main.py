"""Headless 800x480 calendar image server for the work laptop.

Renders the company Google Calendar to an 800x480 1-bit PNG and serves it over
HTTPS — but ONLY while the laptop is on the restream office LAN via en0
(netguard). On public Wi-Fi or VPN-only, the listener is torn down and nothing
is exposed. Every endpoint is gated by a bearer token AND a restream-subnet
client-IP check.

Run:  python3 -m app.mac_main
"""
import datetime
import hmac
import ipaddress
import json
import logging
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse, RedirectResponse

from . import config, settings_store, calendar_client, render, mac_driver, netguard

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("eink.mac")

app = FastAPI(title="E-Ink Calendar (work, 800x480)")

# ---- Instance defaults (seeded on first run) ----
INSTANCE_DEFAULTS = {
    "view_mode": "5days",       # 5-day, larger fonts — best for 800x480
    "day_start": "08:00",
    "day_end": "19:00",
    "max_full_day_events": 2,
    "bw_mode": True,            # black cards, white text (matches the larger calendar)
    "dim_past_events": True,    # past events → checkerboard
    "dim_style": "checkerboard",
    "text_size_modifier": 13,   # bump fonts for the low-res panel (+1pt)
    "time_format": "24h",
    "show_time_line": True,
    "time_line_style": "dotted",
    "selected_calendars": [],
}

# ---- Render state ----
_render_lock = threading.Lock()
_last_render_sig = ""
_last_render_ts = 0.0
_force_render = threading.Event()


# ================= security gates =================

_EXEMPT_PATHS = {"/health", "/"}


def _client_ip_ok(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    return ip in ipaddress.ip_network(config.RESTREAM_SUBNET, strict=False)


def _token_ok(request: Request) -> bool:
    expected = config.get_access_token()
    supplied = ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    if not supplied:
        supplied = request.headers.get("x-auth-token", "").strip()
    if not supplied:
        supplied = request.query_params.get("token", "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@app.middleware("http")
async def _gate(request: Request, call_next):
    # OPEN_MODE (Orange-Pi deployment): the image is hosted openly on a trusted
    # network by design — skip all gating. Enabled explicitly via env.
    if config.OPEN_MODE:
        return await call_next(request)
    path = request.url.path
    if path in _EXEMPT_PATHS:
        return await call_next(request)
    # Capability URL: the image is served at /<access_token>.png. The long,
    # unguessable path IS the credential (lets a headers-less e-ink device fetch
    # it over plain HTTP). Constant-time compared; nothing else is exposed here.
    if path == f"/{config.get_access_token()}.png":
        return await call_next(request)
    loop = _is_loopback(request)
    # Network gate applies to NETWORK clients only. Loopback is the user's own
    # machine (never reachable over the LAN/VPN), so it may configure the app
    # regardless of which network the laptop is on. The e-ink device reaches us
    # over en0 and is fully gated below.
    if not loop:
        if not netguard.on_restream():
            return PlainTextResponse("off-network", status_code=503)
        if not _client_ip_ok(request):
            return PlainTextResponse("forbidden", status_code=403)
    # Bearer token (required for everyone, including loopback).
    if not _token_ok(request):
        return PlainTextResponse("unauthorized", status_code=401)
    return await call_next(request)


# ================= rendering =================

def _render_signature(events: list[dict], now: datetime.datetime) -> str:
    """Signature that changes on any add / remove / modify AND on an event
    starting or ending. Including per-event started/ended flags (relative to
    `now`) means a re-render fires the moment an event begins or ends — not just
    when its data changes."""
    parts = []
    for ev in events:
        started = 1 if _as_dt(ev["start"], now) <= now else 0
        ended = 1 if _as_dt(ev["end"], now) <= now else 0
        parts.append(f"{ev['id']}|{ev['summary']}|{ev['start']}|{ev['end']}|{ev['all_day']}|{started}|{ended}")
    return "|".join(sorted(parts))


def _as_dt(value, now: datetime.datetime) -> datetime.datetime:
    """Coerce an event start/end (date or datetime, naive or aware) to an aware
    datetime in now's timezone for comparison."""
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=now.tzinfo)
        return value.astimezone(now.tzinfo)
    # all-day (date): treat as start-of-day in local tz
    return datetime.datetime(value.year, value.month, value.day, tzinfo=now.tzinfo)


def _next_half_hour(dt: datetime.datetime) -> datetime.datetime:
    """The next :00 or :30 boundary strictly after dt."""
    if dt.minute < 30:
        return dt.replace(minute=30, second=0, microsecond=0)
    return dt.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)


def _event_range(now: datetime.datetime, view_mode: str):
    """Fetch window for the current view."""
    if view_mode == "5days":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + datetime.timedelta(days=5)
    if view_mode == "7days":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + datetime.timedelta(days=7)
    if view_mode in ("month", "35days"):
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, start + datetime.timedelta(days=42)
    # week
    start = now - datetime.timedelta(days=now.weekday())
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + datetime.timedelta(days=7)


def do_render(force: bool = False, render_now: datetime.datetime = None) -> bool:
    """Fetch events, render to the 1-bit image file. Returns True on success.

    render_now: the time to DEPICT (time-line position, past/future dimming). For
    the scheduled boundary renders this is the upcoming :00/:30 so the image is
    correct once it reaches the screen. Change-detection always uses the ACTUAL
    current time, so a real add/remove/modify/start/end still triggers a render."""
    global _last_render_sig, _last_render_ts
    if not _render_lock.acquire(timeout=120):
        logger.error("do_render: lock timeout")
        return False
    try:
        s = settings_store.load()
        # Timezone-aware local time. Naive datetimes made render convert the
        # tz-aware Google events to UTC (3h off the local hour grid). astimezone()
        # attaches the system tz (Europe/Tallinn on the Pi) so events land right.
        actual_now = datetime.datetime.now().astimezone()
        depict_now = render_now.astimezone() if render_now else actual_now

        if not calendar_client.is_authenticated():
            _render_status_image("Connect a Google account", "Open the settings page")
            return False

        view_mode = s.get("view_mode", "5days")
        start, end = _event_range(depict_now, view_mode)
        events = calendar_client.fetch_events(start, end, s.get("selected_calendars") or None)

        # Change detection is relative to the REAL clock (real starts/ends), even
        # when we depict a slightly future minute.
        sig = _render_signature(events, actual_now)
        changed = (sig != _last_render_sig)      # add / remove / modify / start / end
        if not force and not changed and _last_render_ts:
            return False

        img = render.render_calendar(
            view_mode=view_mode,
            events=events,
            day_start=s.get("day_start", "08:00"),
            day_end=s.get("day_end", "19:00"),
            max_full_day=s.get("max_full_day_events", 2),
            time_format=s.get("time_format", "24h"),
            date_format=s.get("date_format", ""),
            text_size_modifier=s.get("text_size_modifier", 12),
            show_time_line=s.get("show_time_line", True),
            time_line_style=s.get("time_line_style", "dotted"),
            bw_mode=s.get("bw_mode", False),
            dim_past_events=s.get("dim_past_events", False),
            crossed_event_dim=s.get("crossed_event_dim", False),
            now=depict_now,
        )
        mac_driver.render_to_file(img)
        _last_render_sig = sig
        _last_render_ts = time.time()
        logger.info("Rendered (%d events, changed=%s, depict=%s)",
                    len(events), changed, depict_now.strftime("%H:%M"))
        return True
    except Exception as e:
        logger.error("do_render error: %s", e)
        return False
    finally:
        _render_lock.release()


def _render_status_image(msg: str, sub: str = ""):
    """Render a simple centered message to the output image (setup/notice)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (config.SCREEN_W, config.SCREEN_H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    f = render._font(60, bold=True)
    fs = render._font(36)
    tw = render._text_w(d, msg, f)
    d.text(((config.SCREEN_W - tw) // 2, config.SCREEN_H // 2 - 80), msg, fill=(0, 0, 0), font=f)
    if sub:
        sw = render._text_w(d, sub, fs)
        d.text(((config.SCREEN_W - sw) // 2, config.SCREEN_H // 2 + 10), sub, fill=(0, 0, 0), font=fs)
    mac_driver.render_to_file(img)


_render_loop_running = False
_render_loop_guard = threading.Lock()


def render_loop():
    """Background render loop. Re-renders:
      • every 30 min aligned to the clock (:00 and :30), and
      • whenever an event is added / removed / modified / starts / ends,
    detected within EVENT_POLL_SEC. Single-instance (safe to invoke from both
    main() and the ASGI startup hook)."""
    global _render_loop_running
    with _render_loop_guard:
        if _render_loop_running:
            return
        _render_loop_running = True
    logger.info("Render loop started")
    scheduled_for = None  # boundary datetime we've already pre-rendered
    while True:
        try:
            now = datetime.datetime.now().astimezone()
            boundary = _next_half_hour(now)                 # upcoming :00 / :30
            trigger = boundary - datetime.timedelta(seconds=config.RENDER_LEAD_SEC)  # :29:20 / :59:20
            if _force_render.is_set():
                do_render(force=True)                       # manual / auth just completed
                _force_render.clear()
            elif now >= trigger and scheduled_for != boundary:
                # Pre-render the upcoming boundary a bit early, depicting that
                # boundary time so it's correct once it reaches the screen.
                do_render(force=True, render_now=boundary)
                scheduled_for = boundary
            else:
                do_render()                                 # event add/remove/modify/start/end
        except Exception as e:
            logger.error("render_loop error: %s", e)

        # Sleep until the sooner of the next trigger (:29:20 / :59:20) or
        # EVENT_POLL_SEC (to catch event changes). If we're inside the lead
        # window, wake just past the boundary to roll to the next one.
        now2 = datetime.datetime.now().astimezone()
        trigger2 = _next_half_hour(now2) - datetime.timedelta(seconds=config.RENDER_LEAD_SEC)
        if now2 < trigger2:
            sleep_s = min(config.EVENT_POLL_SEC, (trigger2 - now2).total_seconds())
        else:
            sleep_s = (_next_half_hour(now2) - now2).total_seconds() + 2
        _force_render.wait(timeout=max(1, sleep_s))


# ================= endpoints =================

@app.on_event("startup")
async def _startup():
    """Ensure the render loop is running whenever the app is served directly by
    uvicorn (e.g. the Orange-Pi OPEN_MODE service runs `uvicorn app.mac_main:app`
    without going through main()). Idempotent."""
    # Seed instance defaults on first boot.
    if not config.SETTINGS_FILE.exists():
        merged = {**settings_store.load(), **INSTANCE_DEFAULTS}
        settings_store.save(merged)
        logger.info("Seeded default settings (%s)", config.SETTINGS_FILE)
    threading.Thread(target=render_loop, daemon=True, name="render").start()


@app.get("/health")
async def health():
    return {"ok": True, "on_restream": netguard.on_restream()}


@app.get("/")
async def root(request: Request):
    """Convenience landing. On loopback (the user's own machine) auto-append the
    access token so the settings page opens without typing it. Network clients
    are redirected token-less and must supply their own (they're fully gated)."""
    if _is_loopback(request):
        return RedirectResponse(f"/settings?token={config.get_access_token()}")
    return RedirectResponse("/settings")


@app.get("/image")
async def get_image():
    p = Path(config.RENDER_IMAGE)
    if not p.exists():
        return JSONResponse({"error": "no image yet"}, status_code=404)
    return FileResponse(str(p), media_type="image/png", filename="calendar-800x480.png")


@app.get("/{name}.png")
async def secret_image(name: str):
    """Serve the calendar image at the capability URL /<access_token>.png.

    The long path is the credential — a headers-less device can fetch it over
    plain HTTP. Any other name 404s (constant-time compared)."""
    if not hmac.compare_digest(name, config.get_access_token()):
        return PlainTextResponse("not found", status_code=404)
    p = Path(config.RENDER_IMAGE)
    if not p.exists():
        return JSONResponse({"error": "no image yet"}, status_code=404)
    return FileResponse(str(p), media_type="image/png", filename="calendar.png")


@app.get("/api/status")
async def status():
    return {
        "authenticated": calendar_client.is_authenticated(),
        "configured": calendar_client.is_configured(),
        "on_restream": netguard.on_restream(),
        "en0_ip": netguard.current_ip(),
        "settings": settings_store.load(),
    }


@app.get("/api/render")
async def api_render():
    _force_render.set()
    return {"ok": True}


@app.get("/preview", response_class=HTMLResponse)
async def preview(request: Request):
    tok = request.query_params.get("token", "")
    return f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Calendar preview</title>
<body style="background:#111;color:#eee;font-family:sans-serif;text-align:center;padding:16px">
<h3>800×480 · 1-bit preview</h3>
<img id=i style="width:800px;max-width:100%;image-rendering:pixelated;border:1px solid #555;background:#fff">
<p id=t style="color:#888;font-size:.8em"></p>
<script>
const tok={json.dumps(tok)};
function r(){{document.getElementById('i').src='/image?token='+encodeURIComponent(tok)+'&_='+Date.now();
document.getElementById('t').textContent='Updated '+new Date().toLocaleTimeString();}}
r();setInterval(r,15000);
</script>"""


# ---- Google OAuth (paste-code flow; server is bound to en0, not localhost) ----
_OAUTH_REDIRECT_URI = "http://localhost:%d/auth/callback" % config.APP_PORT


@app.get("/auth/start")
async def auth_start():
    try:
        url = calendar_client.start_auth(_OAUTH_REDIRECT_URI)
        return {"auth_url": url}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/auth/exchange")
async def auth_exchange(code: str = Form(...)):
    # Accept either a bare code or a full redirect URL with ?code=
    if "code=" in code:
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(code).query)
        code = (q.get("code") or [code])[0]
    ok, err = calendar_client.complete_auth(code.strip())
    if ok:
        _force_render.set()
        return {"ok": True}
    return JSONResponse({"error": err}, status_code=400)


@app.get("/auth/logout")
async def auth_logout(request: Request):
    calendar_client.logout()
    tok = request.query_params.get("token", "")
    return HTMLResponse(f'<meta http-equiv="refresh" content="0;url=/settings?token={tok}">')


@app.post("/api/upload-secret")
async def upload_secret(file: UploadFile = File(...)):
    if not file.filename.endswith(".json"):
        return JSONResponse({"error": "must be .json"}, status_code=400)
    content = await file.read()
    Path(config.GOOGLE_CLIENT_SECRET).write_bytes(content)
    logger.info("client_secret.json uploaded (%d bytes)", len(content))
    return {"ok": True}


@app.get("/api/calendars")
async def api_calendars():
    return {"calendars": calendar_client.list_calendars()}


@app.post("/api/settings")
async def update_settings(request: Request):
    form = await request.form()
    patch = {}
    if "view_mode" in form:
        patch["view_mode"] = form["view_mode"]
    if "day_start" in form:
        patch["day_start"] = form["day_start"]
    if "day_end" in form:
        patch["day_end"] = form["day_end"]
    if "text_size_modifier" in form:
        try:
            patch["text_size_modifier"] = int(form["text_size_modifier"])
        except ValueError:
            pass
    if "max_full_day_events" in form:
        try:
            patch["max_full_day_events"] = int(form["max_full_day_events"])
        except ValueError:
            pass
    cals = form.getlist("selected_calendars") if hasattr(form, "getlist") else form.get("selected_calendars")
    if cals:
        patch["selected_calendars"] = list(cals) if isinstance(cals, (list, tuple)) else [cals]
    settings_store.update(patch)
    _force_render.set()
    return {"ok": True}


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    tok = request.query_params.get("token", "")
    s = settings_store.load()
    authed = calendar_client.is_authenticated()
    configured = calendar_client.is_configured()
    return HTMLResponse(_settings_html(tok, s, authed, configured))


def _settings_html(tok: str, s: dict, authed: bool, configured: bool) -> str:
    t = json.dumps(tok)
    sel = s.get("selected_calendars", [])
    return f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Work Calendar · settings</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:640px;margin:0 auto;padding:16px;background:#f6f7f9;color:#1a1a1a}}
h2{{margin:.2em 0}} .card{{background:#fff;border:1px solid #e2e4e8;border-radius:10px;padding:14px;margin:12px 0}}
label{{display:block;margin:8px 0 4px;font-size:.9em;color:#444}} input,select{{padding:7px;font-size:1em;width:100%;box-sizing:border-box}}
button{{padding:9px 14px;border:0;border-radius:8px;background:#2b6cff;color:#fff;font-size:1em;cursor:pointer;margin-top:10px}}
.ok{{color:#0a7d33}} .warn{{color:#b26b00}} .muted{{color:#888;font-size:.8em}} a{{color:#2b6cff}}
.cal{{display:flex;align-items:center;gap:8px;margin:4px 0}} .cal input{{width:auto}}
</style>
<body>
<h2>Work Calendar — 800×480</h2>
<p class=muted>Company account · this instance is separate from the home device.</p>

<div class=card>
<h3>Google</h3>
<div id=authbox>{"<span class=ok>✓ Connected</span>" if authed else ("<span class=warn>client_secret.json needed</span>" if not configured else "<span class=warn>Not signed in</span>")}</div>
{"" if configured else '''
<label>Upload client_secret.json (same OAuth app as the home device)</label>
<input type=file id=secret accept=.json>
<button onclick=uploadSecret()>Upload</button>'''}
{"" if not configured or authed else '''
<button onclick=startAuth()>Sign in with Google</button>
<div id=flow style=display:none>
  <p>1. <a id=aurl target=_blank>Open this authorization link</a> and approve.</p>
  <p>2. The browser will fail to load a <code>localhost</code> page — that's expected. Copy the whole URL (or just the <code>code</code>) and paste it here:</p>
  <input id=code placeholder="paste redirect URL or code">
  <button onclick=exchange()>Finish sign-in</button>
  <p id=authmsg class=muted></p>
</div>'''}
{"<button onclick=logout()>Log out</button>" if authed else ""}
</div>

<div class=card>
<h3>Display</h3>
<label>View</label>
<select id=view_mode>
  {"".join(f'<option value="{v}"{" selected" if s.get("view_mode")==v else ""}>{v}</option>' for v in ["5days","7days","week","month","35days"])}
</select>
<label>Day start</label><input id=day_start value="{s.get('day_start','08:00')}">
<label>Day end</label><input id=day_end value="{s.get('day_end','19:00')}">
<label>Text size modifier ({s.get('text_size_modifier',12)})</label><input id=text_size_modifier type=number value="{s.get('text_size_modifier',12)}">
<label>Max full-day events</label><input id=max_full_day_events type=number value="{s.get('max_full_day_events',2)}">
<div id=cals></div>
<button onclick=saveSettings()>Save &amp; render</button>
</div>

<p><a id=prev href="#">Open live preview →</a></p>

<script>
const tok={t};
const qs=t=>'token='+encodeURIComponent(tok);
const H={{'X-Auth-Token':tok}};
document.getElementById('prev').href='/preview?'+qs();

async function uploadSecret(){{
  const f=document.getElementById('secret').files[0]; if(!f)return;
  const fd=new FormData(); fd.append('file',f);
  const r=await fetch('/api/upload-secret?'+qs(),{{method:'POST',headers:H,body:fd}});
  if((await r.json()).ok) location.reload();
}}
async function startAuth(){{
  const r=await fetch('/auth/start?'+qs(),{{headers:H}}); const d=await r.json();
  if(d.auth_url){{document.getElementById('aurl').href=d.auth_url;document.getElementById('flow').style.display='block';}}
  else alert(d.error||'error');
}}
async function exchange(){{
  const code=document.getElementById('code').value;
  const fd=new FormData(); fd.append('code',code);
  const r=await fetch('/auth/exchange?'+qs(),{{method:'POST',headers:H,body:fd}});
  const d=await r.json();
  if(d.ok) location.reload(); else document.getElementById('authmsg').textContent=d.error||'error';
}}
function logout(){{location.href='/auth/logout?'+qs();}}

const SELECTED={json.dumps(sel)};
async function loadCals(){{
  try{{
    const r=await fetch('/api/calendars?'+qs(),{{headers:H}}); const d=await r.json();
    const box=document.getElementById('cals'); if(!d.calendars||!d.calendars.length)return;
    box.innerHTML='<label>Calendars</label>'+d.calendars.filter(c=>!c._error).map(c=>
      `<div class=cal><input type=checkbox value="${{c.id}}" ${{(SELECTED.length===0||SELECTED.includes(c.id))?'checked':''}}> ${{c.summary}}</div>`).join('');
  }}catch(e){{}}
}}
{"loadCals();" if authed else ""}

async function saveSettings(){{
  const fd=new FormData();
  ['view_mode','day_start','day_end','text_size_modifier','max_full_day_events'].forEach(id=>fd.append(id,document.getElementById(id).value));
  document.querySelectorAll('#cals input:checked').forEach(c=>fd.append('selected_calendars',c.value));
  await fetch('/api/settings?'+qs(),{{method:'POST',headers:H,body:fd}});
  alert('Saved — re-rendering.');
}}
</script>"""


# ================= server supervisor =================

def _make_server(bind_ip: str) -> uvicorn.Server:
    kwargs = dict(host=bind_ip, port=config.APP_PORT, log_level="info")
    if config.SSL_ENABLED and Path(config.SSL_CERT).exists() and Path(config.SSL_KEY).exists():
        kwargs["ssl_certfile"] = config.SSL_CERT
        kwargs["ssl_keyfile"] = config.SSL_KEY
    return uvicorn.Server(uvicorn.Config(app, **kwargs))


def supervise():
    """Start/stop the HTTPS listener as the network changes. Bind ONLY to en0's
    restream IP — never 0.0.0.0, never a VPN address."""
    server = None
    server_thread = None
    bound_ip = None
    scheme = "https" if config.SSL_ENABLED else "http"

    while True:
        on = netguard.on_restream()
        ip = netguard.current_ip()
        want = on and bool(ip)

        if want and (server is None or ip != bound_ip):
            if server is not None:
                logger.info("en0 IP changed %s -> %s, rebinding", bound_ip, ip)
                server.should_exit = True
                if server_thread:
                    server_thread.join(timeout=10)
                server = None
            config.ensure_ssl_cert()
            server = _make_server(ip)
            server_thread = threading.Thread(target=server.run, daemon=True)
            server_thread.start()
            bound_ip = ip
            logger.info("Serving on %s://%s:%d  (token in %s)",
                        scheme, ip, config.APP_PORT, config.ACCESS_TOKEN_FILE)

        elif not want and server is not None:
            logger.info("Left restream network — stopping listener")
            server.should_exit = True
            if server_thread:
                server_thread.join(timeout=10)
            server = None
            server_thread = None
            bound_ip = None

        time.sleep(3)


def main():
    # Seed instance defaults on first run.
    if not config.SETTINGS_FILE.exists():
        merged = {**settings_store.load(), **INSTANCE_DEFAULTS}
        settings_store.save(merged)
        logger.info("Seeded default settings (%s)", config.SETTINGS_FILE)

    tok = config.get_access_token()
    logger.info("Access token (present it as Bearer / ?token=): %s", tok)

    netguard.start_monitor()
    netguard.refresh()

    threading.Thread(target=render_loop, daemon=True, name="render").start()

    # Always-on loopback listener so the user can configure from their own
    # browser (https://localhost:PORT) regardless of network. Loopback is not
    # reachable over the LAN/VPN, so this exposes nothing to others.
    config.ensure_ssl_cert()
    lo_server = _make_server("127.0.0.1")
    threading.Thread(target=lo_server.run, daemon=True, name="loopback").start()
    logger.info("Local settings UI: %s://localhost:%d/settings?token=%s",
                "https" if config.SSL_ENABLED else "http", config.APP_PORT, tok)

    # Supervisor (blocks main thread) manages the network-gated en0 listener
    # that the e-ink device fetches /image from.
    supervise()


if __name__ == "__main__":
    main()
