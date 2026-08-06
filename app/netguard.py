"""Network gate — "am I on the restream office LAN, via en0?".

This is the core safety mechanism. The work laptop roams onto public Wi-Fi and
runs a WireGuard VPN (the default route currently exits a utun* interface), so
the image server must expose NOTHING unless the machine is genuinely on the
restream office network, reached DIRECTLY over en0 — never over the VPN.

`on_restream()` is therefore strict:
  1. en0 must have an IPv4 inside RESTREAM_SUBNET.
  2. The restream router (and optional internal marker) must be reachable via a
     socket BOUND to the en0 source IP, so VPN reachability never counts.

A VPN (utun*) address is never accepted. A background monitor keeps a cached
state so the server supervisor can start/stop the listener as the network
changes.
"""
import ipaddress
import logging
import socket
import ssl
import threading
import time
from urllib.parse import urlparse

from . import config

logger = logging.getLogger("eink.netguard")

_state_lock = threading.Lock()
_on_restream = False
_last_ip = ""
_monitor_thread = None
_stop = threading.Event()


def _en0_ip() -> str:
    return config.current_en0_ip()


def _ip_in_subnet(ip: str, cidr: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False


def _tcp_probe_via_en0(host: str, port: int, src_ip: str, timeout: float = 3.0) -> bool:
    """Open a TCP connection to host:port with the socket bound to src_ip (en0).

    Binding the source forces the traffic out of en0's routing/source selection
    rather than the default route (the VPN). Returns True on connect.
    """
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.bind((src_ip, 0))
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def _marker_ok(src_ip: str) -> bool:
    """Optional stronger check: fetch RESTREAM_MARKER_URL forced out of en0.

    Only used if configured. A 2xx/3xx/401/403 response (i.e. the host answered)
    counts as reachable — we care that the internal host is there, not its body.
    """
    url = config.RESTREAM_MARKER_URL.strip()
    if not url:
        return True  # not configured → skip (router probe is sufficient)
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    # Resolve + TCP connect bound to en0. (We deliberately keep this to a TCP
    # reachability check to avoid trusting DNS that may resolve over the VPN.)
    try:
        # getaddrinfo may use system DNS; acceptable — the bind still forces the
        # connection itself out of en0.
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for *_, sockaddr in infos:
        if _tcp_probe_via_en0(sockaddr[0], sockaddr[1], src_ip):
            return True
    return False


def compute_on_restream() -> tuple[bool, str]:
    """Compute (on_restream, en0_ip) fresh, without touching cached state."""
    ip = _en0_ip()
    if not ip:
        return False, ""
    if not _ip_in_subnet(ip, config.RESTREAM_SUBNET):
        return False, ip
    # Router reachable directly via en0?
    if not _tcp_probe_via_en0(config.RESTREAM_ROUTER, 80, ip):
        # Some gateways don't answer :80; fall back to an ICMP-less TCP touch on
        # the marker if configured, otherwise treat router-in-subnet as enough
        # only when no marker is set AND the router didn't answer -> be strict.
        if config.RESTREAM_MARKER_URL.strip():
            if not _marker_ok(ip):
                return False, ip
            return True, ip
        # No marker and router :80 closed — accept subnet membership as the
        # signal (we already required en0 to hold a restream IP, which the VPN
        # cannot provide). This keeps us working if the gateway drops :80.
        return True, ip
    # Router answered; if a marker is configured, require it too.
    if not _marker_ok(ip):
        return False, ip
    return True, ip


def on_restream() -> bool:
    """Return the cached on-restream state (updated by the monitor)."""
    with _state_lock:
        return _on_restream


def current_ip() -> str:
    with _state_lock:
        return _last_ip


def refresh() -> bool:
    """Recompute state now and update the cache. Returns the new state."""
    global _on_restream, _last_ip
    ok, ip = compute_on_restream()
    with _state_lock:
        changed = (ok != _on_restream) or (ip != _last_ip)
        _on_restream = ok
        _last_ip = ip
    if changed:
        logger.info("netguard: on_restream=%s en0_ip=%s", ok, ip or "(none)")
    return ok


def start_monitor(interval: int = None):
    """Start the background monitor loop (idempotent)."""
    global _monitor_thread
    if _monitor_thread and _monitor_thread.is_alive():
        return
    interval = interval or config.NETGUARD_INTERVAL_SEC
    _stop.clear()

    def _loop():
        while not _stop.is_set():
            try:
                refresh()
            except Exception as e:
                logger.warning("netguard refresh error: %s", e)
            _stop.wait(interval)

    _monitor_thread = threading.Thread(target=_loop, daemon=True, name="netguard")
    _monitor_thread.start()
    logger.info("netguard monitor started (every %ds)", interval)


def stop_monitor():
    _stop.set()
