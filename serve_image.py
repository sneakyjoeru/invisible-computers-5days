#!/usr/bin/env python3
"""Tiny serve-only HTTP server for the Mac.

Serves ONE image file at ONE long, unguessable URL over plain HTTP — nothing
else. The Orange Pi renders the calendar (and handles Google auth over HTTPS);
the resulting image reaches this Mac via a mounted/shared directory. This
process just exposes that file so a headers-less e-ink device can fetch it.

Config (env):
  IMG_PATH   path to the image file to serve   (required)
  IMG_NAME   the long URL name (the secret)     (default: read ./config/image_name, else random)
  IMG_HOST   bind address                       (default: 0.0.0.0)
  IMG_PORT   bind port                           (default: 8891)

URL served:  http://<host>:<port>/<IMG_NAME>
Any other path returns 404.
"""
import hmac
import http.server
import os
import secrets
import socketserver
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMG_PATH = os.environ.get("IMG_PATH", str(HERE / "config" / "render.png"))
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


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if not hmac.compare_digest(path, _URL_PATH):
            self.send_response(404)
            self.end_headers()
            return
        try:
            data = Path(IMG_PATH).read_bytes()
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
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((IMG_HOST, IMG_PORT), Handler) as srv:
        print(f"Serving {IMG_PATH}")
        print(f"URL: http://<this-mac-ip>:{IMG_PORT}{_URL_PATH}")
        srv.serve_forever()


if __name__ == "__main__":
    main()
