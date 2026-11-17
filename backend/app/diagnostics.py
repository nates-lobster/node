import ipaddress
import json
import os
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import settings
from .models import ScraperHeartbeat

TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _is_tailscale_ip(ip_str: Optional[str]) -> bool:
    if not ip_str:
        return False
    clean = ip_str.strip().split(",")[0].strip()
    if ":" in clean and clean.count(":") == 1:
        clean = clean.split(":", 1)[0]
    try:
        addr = ipaddress.ip_address(clean)
        return addr.version == 4 and addr in TAILSCALE_CGNAT
    except ValueError:
        return False


def _query_tailscaled_unix_socket(sock_path: Path) -> Optional[Dict[str, Any]]:
    """
    Query `tailscaled` LocalAPI directly over its Unix socket without requiring the
    `tailscale` CLI binary inside a Docker container.
    """
    if not sock_path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.2)
            s.connect(str(sock_path))
            req = (
                b"GET /localapi/v0/status HTTP/1.1\r\n"
                b"Host: local-tailscaled.sock\r\n"
                b"Connection: close\r\n\r\n"
            )
            s.sendall(req)
            chunks = []
            while True:
                buf = s.recv(65536)
                if not buf:
                    break
                chunks.append(buf)
        raw = b"".join(chunks).decode("utf-8", errors="replace")
        if "\r\n\r\n" in raw:
            _, body = raw.split("\r\n\r\n", 1)
            # Strip simple HTTP chunked framing if present
            idx = body.find("{")
            ridx = body.rfind("}")
            if idx != -1 and ridx != -1:
                return json.loads(body[idx : ridx + 1])
    except Exception:
        return None
    return None


def probe_tailscale(
    client_host: Optional[str] = None,
    forwarded_for: Optional[str] = None,
    request_host: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Accurate Tailscale detector supporting both bare-metal Linux (`tailscale0` / `tailscaled`)
    and Docker bridge deployments accessed over Tailscale (`X-Real-IP: 100.x.y.z`, `*.ts.net`,
    or mounted `/host_tailscale/tailscaled.sock`).
    """
    interface_exists = Path("/sys/class/net/tailscale0").exists()
    sock_candidates = [
        Path("/var/run/tailscale/tailscaled.sock"),
        Path("/run/tailscale/tailscaled.sock"),
        Path("/host_tailscale/tailscaled.sock"),
    ]
    active_sock = next((p for p in sock_candidates if p.exists()), None)
    binary_path = shutil.which("tailscale")

    tailscale_ips: List[str] = []
    detected_iface: Optional[str] = "tailscale0" if interface_exists else None
    daemon_state = "not_installed"
    tailnet_name: Optional[str] = None

    # 1. Try Unix socket LocalAPI (works inside Docker when /var/run/tailscale is mounted!)
    if active_sock:
        ts_json = _query_tailscaled_unix_socket(active_sock)
        if ts_json:
            daemon_state = str(ts_json.get("BackendState", "Running"))
            tailnet_name = (ts_json.get("CurrentTailnet") or {}).get("Name")
            for ip_val in ts_json.get("TailscaleIPs") or []:
                if ip_val not in tailscale_ips:
                    tailscale_ips.append(ip_val)

    # 2. Fallback to CLI if installed
    if not tailscale_ips and binary_path and active_sock:
        try:
            proc = subprocess.run(
                [binary_path, "status", "--json"],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                ts_json = json.loads(proc.stdout)
                daemon_state = str(ts_json.get("BackendState", "unknown"))
                tailnet_name = (ts_json.get("CurrentTailnet") or {}).get("Name")
                for ip_val in ts_json.get("TailscaleIPs") or []:
                    if ip_val not in tailscale_ips:
                        tailscale_ips.append(ip_val)
        except Exception:
            daemon_state = "socket_unreachable"
    elif binary_path and daemon_state == "not_installed":
        daemon_state = "stopped"

    # 3. Inspect live network interfaces
    ip_bin = shutil.which("ip")
    if ip_bin:
        try:
            proc = subprocess.run(
                [ip_bin, "-j", "addr"],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                for iface in json.loads(proc.stdout):
                    ifname = iface.get("ifname", "")
                    is_ts_name = ifname.startswith("tailscale")
                    for addr_info in iface.get("addr_info", []):
                        local_ip = addr_info.get("local", "")
                        if is_ts_name and local_ip:
                            detected_iface = detected_iface or ifname
                            if local_ip not in tailscale_ips:
                                tailscale_ips.append(local_ip)
                        elif _is_tailscale_ip(local_ip) and (active_sock or binary_path):
                            detected_iface = detected_iface or ifname
                            if local_ip not in tailscale_ips:
                                tailscale_ips.append(local_ip)
        except Exception:
            pass

    # 4. Inspect real inbound client / Host headers (essential when backend runs inside a Docker bridge!)
    effective_client = (forwarded_for.split(",")[0].strip() if forwarded_for else None) or client_host
    host_only = (request_host or "").split(":", 1)[0].strip().lower()
    host_is_ts = host_only.endswith(".ts.net") or _is_tailscale_ip(host_only)
    client_is_ts = _is_tailscale_ip(effective_client)

    if _is_tailscale_ip(host_only) and host_only not in tailscale_ips:
        tailscale_ips.append(host_only)

    connected = bool(
        interface_exists
        or (daemon_state.lower() == "running" and tailscale_ips)
        or client_is_ts
        or host_is_ts
    )

    if connected and daemon_state in ("not_installed", "stopped"):
        daemon_state = "Active (Docker Proxy)"

    if connected:
        parts = []
        if tailscale_ips:
            parts.append(", ".join(tailscale_ips[:2]))
        if client_is_ts:
            parts.append(f"viewer {effective_client}")
        elif host_only.endswith(".ts.net"):
            parts.append(host_only)
        detail = f"Connected via Tailscale ({' • '.join(parts) if parts else 'active'})"
    elif binary_path:
        detail = f"Tailscale installed, daemon is {daemon_state}"
    else:
        detail = "Not active on this connection (local/non-Tailscale network)"

    return {
        "connected": connected,
        "daemon_state": daemon_state,
        "interface": detected_iface or ("tailscale-proxy" if (client_is_ts or host_is_ts) else None),
        "tailscale_ips": tailscale_ips,
        "tailnet_name": tailnet_name,
        "client_ip": effective_client,
        "client_via_tailscale": bool(client_is_ts or host_is_ts),
        "detail": detail,
    }


def probe_scraper_environment(
    heartbeats: List[ScraperHeartbeat],
    poll_interval_seconds: int = 30,
    live_stream_tabs: int = 0,
) -> Dict[str, Any]:
    profile_path = Path(settings.browser_profile_dir)
    state_file = Path(settings.scraper_state_path)

    profile_exists = profile_path.is_dir()
    cookie_db_exists = False
    profile_file_count = 0
    profile_size_kb = 0

    if profile_exists:
        cookie_candidates = [
            profile_path / "Default" / "Cookies",
            profile_path / "Cookies",
            profile_path / "Default" / "Network" / "Cookies",
        ]
        cookie_db_exists = any(p.exists() for p in cookie_candidates)
        try:
            total_bytes = 0
            for root, _dirs, files in os.walk(profile_path):
                for fname in files:
                    profile_file_count += 1
                    try:
                        total_bytes += (Path(root) / fname).stat().st_size
                    except OSError:
                        pass
            profile_size_kb = round(total_bytes / 1024, 1)
        except OSError:
            pass

    now = datetime.now(timezone.utc)
    freshness_window = max(poll_interval_seconds * 3, 120)
    live_warm_count = 0
    stale_count = 0
    simulated_count = 0

    for hb in heartbeats:
        st_code, _label = evaluate_honest_session_state(hb, poll_interval_seconds=poll_interval_seconds, now=now)
        if st_code == "warm":
            live_warm_count += 1
        elif st_code == "stale":
            stale_count += 1
        elif st_code == "simulated":
            simulated_count += 1

    active_tabs = max(live_warm_count, live_stream_tabs)
    daemon_active = active_tabs > 0
    brave_installed = bool(shutil.which("brave-browser") or Path("/usr/bin/brave-browser").is_file())
    engine_name = "Brave Browser (Zero-Google)" if brave_installed else "Hardened Chromium (Zero-Google)"

    if daemon_active:
        summary = f"{engine_name} active — {active_tabs} warm tab(s) running on server"
    elif cookie_db_exists:
        summary = f"{engine_name} profile saved on disk (open any app window to wake tab)"
    else:
        summary = f"{engine_name} ready — click 'Stream App' to launch & sign in on server"

    return {
        "daemon_active": daemon_active,
        "browser_engine": engine_name,
        "is_brave": brave_installed,
        "live_warm_sessions": active_tabs,
        "stale_sessions": stale_count,
        "simulated_only_sources": simulated_count,
        "browser_profile_dir": str(profile_path),
        "browser_profile_exists": profile_exists,
        "cookies_seeded": cookie_db_exists,
        "profile_file_count": profile_file_count,
        "profile_size_kb": profile_size_kb,
        "state_file_path": str(state_file),
        "state_file_exists": state_file.is_file(),
        "freshness_window_seconds": freshness_window,
        "detail": summary,
    }


def evaluate_honest_session_state(
    hb: Optional[ScraperHeartbeat],
    poll_interval_seconds: int = 30,
    now: Optional[datetime] = None,
) -> Tuple[str, str]:
    if hb is None or hb.total_pings <= 0:
        return "not_started", "Idle"
    if hb.status == "simulated":
        return "simulated", "Test Ping"
    if hb.status == "auth_required":
        return "auth_required", "Sign-In Needed"
    if hb.status in ("idle", "not_started"):
        return "not_started", "Idle"

    ref_now = now or datetime.now(timezone.utc)
    seen = hb.last_seen
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)

    age_seconds = max(0.0, (ref_now - seen).total_seconds())
    freshness_limit = max(poll_interval_seconds * 3, 120)

    if hb.status == "warm" and age_seconds <= freshness_limit:
        return "warm", "Warm on Server"

    mins = max(1, int(age_seconds // 60))
    return ("stale", f"Idle ({mins}m ago)") if mins < 60 else ("stale", f"Idle ({mins // 60}h ago)")
