#!/usr/bin/env python3
"""Mac side: pull the calendar image from the Orange Pi over WireGuard, then
serve it locally at one long, unguessable HTTP URL for the e-ink screen.

The Pi renders the calendar and handles Google auth (HTTPS); this process does
NOT render. It periodically `scp`s the Pi's rendered PNG into a local cache and
serves that cache at http://<mac>:<port>/<IMG_NAME> over plain HTTP (so a
headers-less device can fetch it). Any other path 404s.

Config (env):
  PI_HOST         ssh target                 (default: orangepi@192.168.0.199)
  PI_KEY          ssh identity file          (default: ./config/pi_key)
  PI_IMAGE_PATH   image path on the Pi       (default: /opt/eink-calendar-work/config/render.png)
  PULL_INTERVAL   seconds between pulls       (default: 60)
  CACHE_PATH      local cache file            (default: ./config/pi_render.png)
  IMG_NAME        long URL name (the secret)  (default: ./config/image_name, else random)
  IMG_HOST        bind address                (default: 0.0.0.0)
  IMG_PORT        bind port                    (default: 8891)
"""
import hmac
import http.server
import os
import secrets
import socketserver
import subprocess
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PI_HOST = os.environ.get("PI_HOST", "orangepi@192.168.0.199")
PI_KEY = os.environ.get("PI_KEY", str(HERE / "config" / "pi_key"))
PI_IMAGE_PATH = os.environ.get("PI_IMAGE_PATH", "/opt/eink-calendar-work/config/render.png")
PULL_INTERVAL = int(os.environ.get("PULL_INTERVAL", "60"))
CACHE_PATH = os.environ.get("CACHE_PATH", str(HERE / "config" / "pi_render.png"))
IMG_HOST = os.environ.get("IMG_HOST", "0.0.0.0")
IMG_PORT = int(os.environ.get("IMG_PORT", "8891"))


def _load_name() -> str:
    name = os.environ.get("IMG_NAME", "").strip()
    if name:
        return name
    f = HERE / "config" / "image_name"
    if f.exists() and f.read_text().strip():
        return f.read_text().strip()
    name = secrets.token_urlsafe(24) + ".png"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(name)
    return name


IMG_NAME = _load_name()
_URL_PATH = "/" + IMG_NAME


def pull_loop():
    """Every PULL_INTERVAL seconds, scp the Pi's image into the local cache."""
    tmp = CACHE_PATH + ".tmp"
    while True:
        try:
            r = subprocess.run(
                ["scp", "-q", "-i", PI_KEY,
                 "-o", "IdentitiesOnly=yes",
                 "-o", "StrictHostKeyChecking=accept-new",
                 "-o", "ConnectTimeout=10",
                 f"{PI_HOST}:{PI_IMAGE_PATH}", tmp],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0 and os.path.exists(tmp):
                os.replace(tmp, CACHE_PATH)
            else:
                print(f"pull failed: {r.stderr.strip()[:200]}")
        except Exception as e:
            print(f"pull error: {e}")
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        time.sleep(PULL_INTERVAL)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        # Clean predictable path plus the long capability path.
        if path != "/calendar.png" and not hmac.compare_digest(path, _URL_PATH):
            self.send_response(404)
            self.end_headers()
            return
        try:
            data = Path(CACHE_PATH).read_bytes()
        except OSError:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"image not available yet")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def main():
    threading.Thread(target=pull_loop, daemon=True, name="pull").start()
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((IMG_HOST, IMG_PORT), Handler) as srv:
        print(f"Pulling {PI_HOST}:{PI_IMAGE_PATH} every {PULL_INTERVAL}s")
        print(f"Serving at http://localhost:{IMG_PORT}/calendar.png")
        print(f"        and http://<this-mac-ip>:{IMG_PORT}{_URL_PATH}")
        srv.serve_forever()


if __name__ == "__main__":
    main()
