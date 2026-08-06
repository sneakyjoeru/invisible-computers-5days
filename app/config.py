"""Configuration for the 800x480 work-laptop calendar image server.

This is an independent instance of the e-ink calendar app, adapted to run as a
plain script on macOS. It does NOT drive a panel over SPI — it renders an
800x480 1-bit image and serves it over HTTPS, network-gated to the restream
office LAN (see netguard.py). Everything (token, client_secret, settings, SSL)
lives under this instance's own CONFIG_DIR, fully separate from the 1872x1404
device instance.

All tunables are env-overridable so the launchd plist / run.sh can adjust them.
"""
import os
import subprocess
from pathlib import Path

# Load .env if present (same convention as the original instance)
BASE_DIR = Path(__file__).resolve().parent.parent
env_file = BASE_DIR / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())

CONFIG_DIR = Path(os.environ.get("EINK_CONFIG_DIR", str(BASE_DIR / "config")))
TMP_DIR = BASE_DIR / "tmp_render"
SSL_DIR = CONFIG_DIR / "ssl"
ASSETS_DIR = BASE_DIR / "assets"

# ---- Screen: final 1-bit output the device fetches (Waveshare 7.5" class) ----
OUTPUT_W = int(os.environ.get("OUTPUT_W", "800"))
OUTPUT_H = int(os.environ.get("OUTPUT_H", "480"))

# ---- Render (supersampled) size ----
# The shared render.py layout is tuned for a ~1404-tall, 4:3-ish canvas. We
# render at 3x the output on the SAME 5:3 aspect as 800x480 (2400x1440 ≈ the
# tuned height), then downscale /3 and threshold to 1-bit in mac_driver. This
# reuses the proven card/text layout untouched, yields clean ~1px borders, and
# avoids rescaling dozens of hardcoded pixel offsets. render.py reads these as
# SCREEN_W/SCREEN_H.
SUPERSAMPLE = int(os.environ.get("SUPERSAMPLE", "3"))
SCREEN_W = OUTPUT_W * SUPERSAMPLE
SCREEN_H = OUTPUT_H * SUPERSAMPLE

# ---- App server ----
# NOTE: we never bind 0.0.0.0. BIND_IP is resolved at runtime to the en0 address
# (see current_en0_ip); APP_HOST here is only a last-resort override.
APP_PORT = int(os.environ.get("APP_PORT", "8890"))
EN0_IFACE = os.environ.get("EN0_IFACE", "en0")

# OPEN_MODE: host the image openly on a trusted network (home LAN + WireGuard +
# restream office) with NO token/subnet/network gating, bound to APP_HOST. This
# is the Orange-Pi deployment mode. When off (default), the Mac deployment's
# en0-only, token-gated, network-gated behaviour applies.
OPEN_MODE = os.environ.get("OPEN_MODE", "0") == "1"
APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")

# ---- SSL / HTTPS ----
SSL_ENABLED = os.environ.get("SSL_ENABLED", "1") == "1"
SSL_CERT = os.environ.get("SSL_CERT", str(SSL_DIR / "cert.pem"))
SSL_KEY = os.environ.get("SSL_KEY", str(SSL_DIR / "key.pem"))

# ---- Access gate (bearer token) ----
# The fetching 800x480 device must present this token. Generated on first run.
ACCESS_TOKEN_FILE = CONFIG_DIR / "access_token"

# ---- Network gate: "am I on the restream office LAN, via en0?" ----
# Defaults derived from the observed office network (en0 10.123.92.158,
# router 10.128.128.128). Override via env if the office renumbers.
RESTREAM_SUBNET = os.environ.get("RESTREAM_SUBNET", "10.123.0.0/16")
RESTREAM_ROUTER = os.environ.get("RESTREAM_ROUTER", "10.128.128.128")
# Optional internal-only URL to probe as a stronger "on restream" marker.
# If set, it is fetched forced out of en0 (see netguard). Empty = router probe only.
RESTREAM_MARKER_URL = os.environ.get("RESTREAM_MARKER_URL", "")
NETGUARD_INTERVAL_SEC = int(os.environ.get("NETGUARD_INTERVAL_SEC", "20"))

# ---- Render cadence ----
# Full re-render at least every 30 minutes; event-change polling in between.
RENDER_INTERVAL_SEC = int(os.environ.get("RENDER_INTERVAL_SEC", str(30 * 60)))
EVENT_POLL_SEC = int(os.environ.get("EVENT_POLL_SEC", "60"))

# ---- Google OAuth (same app as the 1872 instance, different account) ----
GOOGLE_CLIENT_SECRET = os.environ.get(
    "GOOGLE_CLIENT_SECRET", str(CONFIG_DIR / "client_secret.json"))
GOOGLE_TOKEN_FILE = str(CONFIG_DIR / "token.json")
GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]

# ---- Settings ----
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# ---- Rendered output (served by /image, and/or dropped as a file) ----
# Override EINK_RENDER_IMAGE to write the image somewhere a syncing folder or
# photo app picks it up (e.g. ~/Pictures/eink-calendar-800x480.png).
from os.path import expanduser as _expanduser
RENDER_IMAGE = _expanduser(os.environ.get("EINK_RENDER_IMAGE", str(CONFIG_DIR / "render.png")))

# ---- Fonts ----
# Prefer a bundled DejaVuSans.ttf (drop one into assets/ — e.g. copied from the
# Orange Pi at /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf). Fall back to a
# macOS system TTF so the app renders even without the bundled font.
FONT_CANDIDATES = [
    str(ASSETS_DIR / "DejaVuSans.ttf"),
    # Linux / Orange Pi (matches the existing 1872 instance's font):
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    # macOS fallbacks:
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Helvetica.ttf",
    "/Library/Fonts/Arial.ttf",
]

# Ensure dirs
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)
SSL_DIR.mkdir(parents=True, exist_ok=True)
(CONFIG_DIR / "logs").mkdir(parents=True, exist_ok=True)


def current_en0_ip() -> str:
    """Return the current IPv4 address of the en0 interface, or "" if none.

    Never returns a VPN (utun*) address — we look up en0 specifically.
    """
    try:
        out = subprocess.run(
            ["ipconfig", "getifaddr", EN0_IFACE],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def get_access_token() -> str:
    """Read the instance access token, generating one on first run."""
    p = ACCESS_TOKEN_FILE
    if p.exists():
        tok = p.read_text().strip()
        if tok:
            return tok
    import secrets
    tok = secrets.token_urlsafe(32)
    p.write_text(tok)
    p.chmod(0o600)
    return tok


def ensure_ssl_cert() -> bool:
    """Generate a self-signed SSL cert if one doesn't exist.

    Includes the current en0 IP in the SAN so the fetching device can verify
    the host by IP. Browser will still warn (self-signed) — expected on a LAN.
    """
    import logging
    cert_path = Path(SSL_CERT)
    key_path = Path(SSL_KEY)
    if cert_path.exists() and key_path.exists():
        return True
    en0 = current_en0_ip() or "127.0.0.1"
    san = f"subjectAltName=DNS:localhost,IP:127.0.0.1,IP:{en0}"
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:4096",
             "-keyout", str(key_path), "-out", str(cert_path),
             "-days", "3650", "-nodes",
             "-subj", "/CN=E-Ink Calendar (work)",
             "-addext", san],
            capture_output=True, text=True, timeout=30, check=True,
        )
        cert_path.chmod(0o644)
        key_path.chmod(0o600)
        return True
    except Exception as e:
        logging.getLogger("eink.config").warning("SSL cert generation failed: %s", e)
        return False
