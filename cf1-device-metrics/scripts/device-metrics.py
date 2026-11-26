#!/usr/bin/env python3
"""Cloudflare One (WARP / DEX) Device Metrics Collector & Visualizer.

Supports BOTH the Cloudflare `cf` CLI and direct Cloudflare Client v4 REST API
calls (using Python 3 standard library only — zero external dependencies).

Collects `dex_device_state_events` enriched with:
  - Physical Device metadata (`devices/physical-devices`)
  - DEX synthetic tests (`dex_application_tests` + test name mapping)
  - WARP toggle & profile/config changes (`warp_toggle_changes`, `warp_config_changes`)
  - Latest Device Posture evaluations (`device_posture_results`)

Outputs a concise terminal/Markdown report with Unicode sparklines and an
optional self-contained, offline-ready interactive HTML dashboard (`--html`).
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

CF_API_BASE = os.environ.get("CF_API_BASE", "https://api.cloudflare.com/client/v4").rstrip("/")
SPARK_CHARS = "▁▂▃▄▅▆▇█"


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def sql_escape(value: str) -> str:
    return value.replace("'", "''")


def parse_json_loose(raw: str) -> Any:
    """Parse JSON from `cf` CLI output that may include banner/warning/spinner lines."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch in "[{":
            try:
                obj, _ = decoder.raw_decode(text[idx:])
                return obj
            except json.JSONDecodeError:
                continue
    return None


def safe_json_field(value: Any, default: Any) -> Any:
    if value is None or value == "" or value == "null":
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return default if parsed is None else parsed
        except Exception:
            return default
    return default


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        num = float(value)
        return default if math.isnan(num) or math.isinf(num) else num
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    num = safe_float(value)
    return default if num is None else int(round(num))


def percentile(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return ordered[int(k)]
    return ordered[f] * (c - k) + ordered[c] * (k - f)


def stats_summary(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"count": 0, "min": None, "avg": None, "p95": None, "max": None, "latest": None}
    return {
        "count": len(values),
        "min": min(values),
        "avg": statistics.fmean(values),
        "p95": percentile(values, 95.0),
        "max": max(values),
        "latest": values[-1],
    }


def sparkline(values: List[Optional[float]], width: int = 32) -> str:
    clean = [v for v in values if v is not None]
    if not clean:
        return "(no data)"
    # Downsample to target width by averaging buckets
    if len(clean) > width:
        step = len(clean) / width
        sampled = []
        for i in range(width):
            lo = int(i * step)
            hi = max(lo + 1, int((i + 1) * step))
            sampled.append(statistics.fmean(clean[lo:hi]))
        clean = sampled
    lo, hi = min(clean), max(clean)
    if hi - lo < 1e-12:
        return SPARK_CHARS[3] * len(clean)
    chars = []
    for v in clean:
        idx = int((v - lo) / (hi - lo) * (len(SPARK_CHARS) - 1))
        idx = max(0, min(len(SPARK_CHARS) - 1, idx))
        chars.append(SPARK_CHARS[idx])
    return "".join(chars)


def fmt_bytes_rate(bps: Optional[float]) -> str:
    if bps is None:
        return "—"
    if bps >= 1_048_576:
        return f"{bps / 1_048_576:.2f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps:.0f} B/s"


def fmt_num(val: Optional[float], unit: str = "", decimals: int = 1) -> str:
    if val is None:
        return "—"
    return f"{val:.{decimals}f}{unit}"


def wifi_quality_label(dbm: Optional[float]) -> str:
    if dbm is None or dbm >= 0:
        return "N/A"
    if dbm >= -50:
        return "Excellent"
    if dbm >= -60:
        return "Good"
    if dbm >= -70:
        return "Fair"
    if dbm >= -80:
        return "Weak"
    return "Poor"


# ---------------------------------------------------------------------------
# Dual-mode Cloudflare Client (`cf` CLI + Client v4 REST API)
# ---------------------------------------------------------------------------

class CloudflareClient:
    def __init__(
        self,
        mode: str = "auto",
        account_id: str = "",
        profile: str = "",
        verbose: bool = False,
    ) -> None:
        self.profile = profile
        self.verbose = verbose
        self.warnings: List[str] = []

        if mode == "auto":
            self.mode = "cli" if shutil.which("cf") else "api"
        elif mode in ("cli", "api"):
            self.mode = mode
        else:
            raise ValueError(f"Invalid mode '{mode}' (expected: auto, cli, api)")

        self.auth_headers: Dict[str, str] = {}
        if self.mode == "api":
            token = os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CF_API_TOKEN")
            email = os.environ.get("CLOUDFLARE_EMAIL")
            api_key = os.environ.get("CLOUDFLARE_API_KEY")
            if token:
                self.auth_headers = {"Authorization": f"Bearer {token}"}
            elif email and api_key:
                self.auth_headers = {"X-Auth-Email": email, "X-Auth-Key": api_key}
            else:
                raise RuntimeError(
                    "REST API mode requires CLOUDFLARE_API_TOKEN (or CLOUDFLARE_EMAIL + CLOUDFLARE_API_KEY)."
                )
        elif not shutil.which("cf"):
            raise RuntimeError("`cf` CLI was requested (--mode cli) but is not installed on PATH.")

        self.account_id = account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[cf:{self.mode}] {msg}", file=sys.stderr)

    def _run_cf(self, args: List[str], timeout: int = 40) -> Tuple[Any, Optional[str]]:
        cmd = ["cf", *args, "-q"]
        if self.profile:
            cmd.extend(["--profile", self.profile])
        env = os.environ.copy()
        if self.account_id:
            env["CLOUDFLARE_ACCOUNT_ID"] = self.account_id
        self._log(" ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return None, f"Command timed out after {timeout}s"

        parsed = parse_json_loose(proc.stdout)
        if proc.returncode != 0:
            err_msg = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
            # Compact multiline box errors
            err_line = " | ".join(l.strip() for l in err_msg.splitlines() if l.strip())[:240]
            return None, err_line
        return parsed, None

    def _run_api(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        content_type: Optional[str] = None,
        timeout: int = 35,
    ) -> Tuple[Any, Optional[str]]:
        url = f"{CF_API_BASE}{path}"
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v != ""})
            if query:
                url = f"{url}?{query}"
        headers = dict(self.auth_headers)
        if content_type:
            headers["Content-Type"] = content_type
        self._log(f"{method} {url}")
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")[:240]
            return None, f"HTTP {exc.code}: {err_body}"
        except Exception as exc:
            return None, str(exc)

        payload = parse_json_loose(raw)
        if isinstance(payload, dict) and "success" in payload:
            if not payload.get("success"):
                return None, json.dumps(payload.get("errors", []))[:240]
            return payload.get("result"), None
        return payload, None

    def discover_account_id(self) -> str:
        if self.account_id:
            return self.account_id
        if self.mode == "cli":
            data, err = self._run_cf(["accounts", "list", "--per-page", "50"])
        else:
            data, err = self._run_api("GET", "/accounts", {"per_page": "50"})
        if err or not isinstance(data, list) or not data:
            raise RuntimeError(f"Could not discover Cloudflare account ID: {err or 'no accounts returned'}")
        self.account_id = data[0]["id"]
        return self.account_id

    def list_devices(self, email: str = "") -> List[Dict[str, Any]]:
        if self.mode == "cli":
            args = ["zero-trust", "devices", "list"]
            if email:
                args.extend(["--last-seen-user-email", email])
            data, err = self._run_cf(args)
        else:
            params = {"last_seen_user.email": email} if email else None
            data, err = self._run_api("GET", f"/accounts/{self.account_id}/devices/physical-devices", params)
        if err:
            raise RuntimeError(f"Failed to list WARP devices: {err}")
        return data if isinstance(data, list) else []

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        if self.mode == "cli":
            data, err = self._run_cf(["zero-trust", "devices", "get", device_id])
        else:
            data, err = self._run_api("GET", f"/accounts/{self.account_id}/devices/physical-devices/{device_id}")
        if err:
            self.warnings.append(f"Physical device lookup skipped ({err})")
            return None
        return data if isinstance(data, dict) else None

    def list_dex_tests(self) -> Dict[str, Dict[str, Any]]:
        """Return mapping of test_id -> test metadata (name, kind, target)."""
        if self.mode == "cli":
            data, err = self._run_cf(["zero-trust", "dex", "tests", "list"])
        else:
            data, err = self._run_api("GET", f"/accounts/{self.account_id}/dex/devices/dex_tests")
        if err:
            self.warnings.append(f"DEX test metadata unavailable ({err})")
            return {}
        tests = data.get("tests", data) if isinstance(data, dict) else data
        mapping: Dict[str, Dict[str, Any]] = {}
        if isinstance(tests, list):
            for t in tests:
                if isinstance(t, dict) and t.get("test_id"):
                    mapping[t["test_id"]] = t
                elif isinstance(t, dict) and t.get("id"):
                    mapping[t["id"]] = t
        return mapping

    def query_sql(self, sql: str, label: str = "query") -> List[Dict[str, Any]]:
        if self.mode == "cli":
            data, err = self._run_cf(["logs", "query", "--body", sql])
        else:
            data, err = self._run_api(
                "POST",
                f"/accounts/{self.account_id}/logs/explorer/query/sql",
                body=sql.encode("utf-8"),
                content_type="text/plain",
            )
        if err:
            self.warnings.append(f"{label} query failed: {err}")
            return []
        return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Telemetry Normalization & Analytics
# ---------------------------------------------------------------------------

def normalize_state_event(row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize units and unpack JSON-stringified fields for one state event."""
    cpu_pct = safe_float(row.get("cpupercentage"))
    ram_pct = safe_float(row.get("ramusedpercentage"))
    disk_pct = safe_float(row.get("diskusagepercentage"))
    batt_pct = safe_float(row.get("batterypercentage"))
    ram_total_kb = safe_int(row.get("ramavailablekb"))
    ram_total_gb = (ram_total_kb / 1_048_576.0) if ram_total_kb else None
    ram_used_gb = (ram_total_gb * ram_pct) if (ram_total_gb is not None and ram_pct is not None) else None

    hs_ms = safe_float(row.get("handshakelatencyms"))
    if hs_ms is not None and hs_ms < 0:
        hs_ms = None

    conn_type = str(row.get("connectiontype") or "").lower()
    wifi_raw = safe_float(row.get("wifistrengthdbm"))
    wifi_dbm = wifi_raw if (wifi_raw is not None and wifi_raw < 0 and (conn_type == "wifi" or not conn_type)) else None

    down = safe_json_field(row.get("tunnelstatsdownstream"), {})
    up = safe_json_field(row.get("tunnelstatsupstream"), {})

    def parse_tunnel_side(obj: Dict[str, Any]) -> Dict[str, Optional[float]]:
        if not isinstance(obj, dict) or not obj:
            return {
                "rtt_ms": None,
                "min_rtt_ms": None,
                "jitter_ms": None,
                "pkt_sent": None,
                "pkt_recvd": None,
                "pkt_lost": None,
                "pkt_retrans": None,
                "loss_pct": None,
                "retrans_pct": None,
                "bytes_sent": None,
                "bytes_recvd": None,
                "window_ms": None,
            }
        rtt_us = safe_float(obj.get("rttUs"))
        min_rtt_us = safe_float(obj.get("minRttUs"))
        var_us = safe_float(obj.get("rttVarUs"))
        pkt_sent = safe_float(obj.get("packetsSent"))
        pkt_lost = safe_float(obj.get("packetsLost"))
        pkt_retrans = safe_float(obj.get("packetsRetransmitted"))
        loss_pct = (pkt_lost / pkt_sent * 100.0) if (pkt_sent and pkt_sent > 0 and pkt_lost is not None) else None
        retrans_pct = (pkt_retrans / pkt_sent * 100.0) if (pkt_sent and pkt_sent > 0 and pkt_retrans is not None) else None
        return {
            "rtt_ms": (rtt_us / 1000.0) if rtt_us is not None else None,
            "min_rtt_ms": (min_rtt_us / 1000.0) if min_rtt_us is not None else None,
            "jitter_ms": (var_us / 1000.0) if var_us is not None else None,
            "pkt_sent": pkt_sent,
            "pkt_recvd": safe_float(obj.get("packetsRecvd")),
            "pkt_lost": pkt_lost,
            "pkt_retrans": pkt_retrans,
            "loss_pct": loss_pct,
            "retrans_pct": retrans_pct,
            "bytes_sent": safe_float(obj.get("bytesSent")),
            "bytes_recvd": safe_float(obj.get("bytesRecvd")),
            "window_ms": safe_float(obj.get("statsWindowMs")),
        }

    def group_apps(raw_apps: Any) -> List[Dict[str, Any]]:
        parsed = safe_json_field(raw_apps, [])
        if not isinstance(parsed, list):
            return []
        merged: Dict[str, float] = defaultdict(float)
        for item in parsed:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            pct = safe_float(item.get("percentage"))
            if name and pct is not None:
                merged[name] += pct * 100.0
        return [{"name": k, "pct": round(v, 3)} for k, v in sorted(merged.items(), key=lambda x: -x[1])]

    return {
        "timestamp": str(row.get("timestamp") or ""),
        "status": str(row.get("status") or "unknown").lower(),
        "mode": str(row.get("mode") or ""),
        "tunnel_type": str(row.get("tunneltype") or ""),
        "colo": str(row.get("warpcolocode") or ""),
        "platform": str(row.get("clientplatform") or ""),
        "client_version": str(row.get("clientversion") or ""),
        "profile_id": str(row.get("deviceregistrationprofileid") or ""),
        "always_on": bool(row.get("alwayson")),
        "switch_locked": bool(row.get("switchlocked")),
        "firewall_enabled": bool(row.get("firewallenabled")),
        "app_firewall_enabled": bool(row.get("appfirewallenabled")),
        "connection_type": conn_type,
        "ssid": str(row.get("networkssid") or ""),
        "wifi_dbm": wifi_dbm,
        "device_ip": str(row.get("deviceipv4address") or row.get("deviceipv6address") or ""),
        "gateway_ip": str(row.get("gatewayipv4address") or row.get("gatewayipv6address") or ""),
        "isp_ip": str(row.get("ispipv4address") or row.get("ispipv6address") or ""),
        "isp_asn": safe_int(row.get("ispipv4asn")) or safe_int(row.get("ispipv6asn")) or 0,
        "isp_aso": str(row.get("ispipv4aso") or row.get("ispipv6aso") or ""),
        "isp_city": str(row.get("ispipv4city") or row.get("ispipv6city") or ""),
        "isp_state": str(row.get("ispipv4stateiso") or row.get("ispipv6stateiso") or ""),
        "isp_country": str(row.get("ispipv4countryiso") or row.get("ispipv6countryiso") or ""),
        "handshake_ms": hs_ms,
        "down": parse_tunnel_side(down),
        "up": parse_tunnel_side(up),
        "net_rx_bps": safe_float(row.get("networkreceivedbps")),
        "net_tx_bps": safe_float(row.get("networksentbps")),
        "cpu_pct": (cpu_pct * 100.0) if cpu_pct is not None else None,
        "ram_pct": (ram_pct * 100.0) if ram_pct is not None else None,
        "ram_total_gb": ram_total_gb,
        "ram_used_gb": ram_used_gb,
        "disk_pct": (disk_pct * 100.0) if disk_pct is not None else None,
        "disk_read_bps": safe_float(row.get("diskreadbps")),
        "disk_write_bps": safe_float(row.get("diskwritebps")),
        "battery_pct": (batt_pct * 100.0) if batt_pct is not None else None,
        "battery_charging": bool(row.get("batterycharging")),
        "battery_cycles": safe_int(row.get("batterycycles"), 0) or 0,
        "cpu_apps": group_apps(row.get("cpupercentagebyapp")),
        "ram_apps": group_apps(row.get("ramusedpercentagebyapp")),
    }


def aggregate_top_apps(events: List[Dict[str, Any]], key: str, ram_total_gb: Optional[float] = None) -> List[Dict[str, Any]]:
    total_samples = max(1, len(events))
    per_app: Dict[str, List[float]] = defaultdict(list)
    for ev in events:
        for app in ev.get(key, []):
            per_app[app["name"]].append(app["pct"])
    ranked = []
    for name, vals in per_app.items():
        avg_v = statistics.fmean(vals)
        peak_v = max(vals)
        item: Dict[str, Any] = {
            "name": name,
            "avg_pct": round(avg_v, 2),
            "peak_pct": round(peak_v, 2),
            "samples": len(vals),
            "presence_pct": round(len(vals) / total_samples * 100.0, 1),
        }
        if ram_total_gb is not None:
            item["avg_mb"] = round(ram_total_gb * 1024.0 * (avg_v / 100.0), 0)
            item["peak_mb"] = round(ram_total_gb * 1024.0 * (peak_v / 100.0), 0)
        ranked.append(item)
    ranked.sort(key=lambda x: (-x["avg_pct"], -x["peak_pct"], x["name"]))
    return ranked[:12]


def detect_transitions(events: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Detect changes in status, colo, connection_type, SSID, ISP, or tunnel_type."""
    transitions: List[Dict[str, str]] = []
    fields = [
        ("status", "WARP Status"),
        ("colo", "Edge Colo"),
        ("connection_type", "Connection"),
        ("ssid", "Wi-Fi SSID"),
        ("isp_aso", "ISP"),
        ("tunnel_type", "Tunnel"),
    ]
    prev: Optional[Dict[str, Any]] = None
    for ev in events:
        if prev is not None:
            for key, label in fields:
                old_v = prev.get(key) or ""
                new_v = ev.get(key) or ""
                if old_v != new_v:
                    transitions.append({
                        "timestamp": ev["timestamp"],
                        "dimension": label,
                        "from": old_v or "(none)",
                        "to": new_v or "(none)",
                    })
        prev = ev
    return transitions


def analyze_dex_tests(
    raw_tests: List[Dict[str, Any]],
    test_catalog: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    http_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    trace_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for row in raw_tests:
        ttype = str(row.get("testtype") or "").lower()
        tid = str(row.get("testid") or "")
        meta = test_catalog.get(tid, {})
        friendly = str(meta.get("name") or "")

        if ttype == "http":
            url = str(row.get("httpurl") or meta.get("data", {}).get("host") or tid)
            key = friendly or url
            dns_s = safe_float(row.get("httpdomainlookupstartms"), 0.0) or 0.0
            dns_e = safe_float(row.get("httpdomainlookupendms"), 0.0) or 0.0
            conn_s = safe_float(row.get("httpconnectstartms"), 0.0) or 0.0
            conn_e = safe_float(row.get("httpconnectendms"), 0.0) or 0.0
            tls_s = safe_float(row.get("httpsecureconnectionstartms"), 0.0) or 0.0
            req_s = safe_float(row.get("httprequeststartms"), 0.0) or 0.0
            resp_s = safe_float(row.get("httpresponsestartms"), 0.0) or 0.0
            resp_e = safe_float(row.get("httpresponseendms"), 0.0) or 0.0

            dns_ms = max(0.0, dns_e - dns_s)
            if tls_s > 0 and tls_s >= conn_s and conn_e >= tls_s:
                tcp_ms = max(0.0, tls_s - conn_s)
                tls_ms = max(0.0, conn_e - tls_s)
            else:
                tcp_ms = max(0.0, conn_e - conn_s)
                tls_ms = 0.0
            ttfb_ms = max(0.0, resp_s - req_s) if resp_s >= req_s else 0.0
            dl_ms = max(0.0, resp_e - resp_s) if resp_e >= resp_s else 0.0

            status_code = safe_int(row.get("httpstatuscode"), 0) or 0
            err_msg = str(row.get("httperrormessage") or "").strip()
            ok = (200 <= status_code < 400) and not err_msg
            http_groups[key].append({
                "timestamp": str(row.get("timestamp") or ""),
                "url": url,
                "method": str(row.get("httpmethod") or "GET"),
                "status_code": status_code,
                "error": err_msg,
                "ok": ok,
                "execution_context": str(row.get("executioncontext") or ""),
                "colo": str(row.get("colocode") or ""),
                "server_ip": str(row.get("httpserveripaddress") or ""),
                "server_aso": str(row.get("httpserveripaso") or ""),
                "total_ms": resp_e,
                "dns_ms": dns_ms,
                "tcp_ms": tcp_ms,
                "tls_ms": tls_ms,
                "ttfb_ms": ttfb_ms,
                "dl_ms": dl_ms,
            })
        elif ttype == "traceroute":
            dest = str(row.get("traceroutedestinationhostname") or row.get("traceroutedestinationipaddress") or tid)
            key = friendly or dest
            hops_raw = safe_json_field(row.get("traceroutehops"), [])
            hops = []
            if isinstance(hops_raw, list):
                for h in hops_raw:
                    if not isinstance(h, dict):
                        continue
                    rtts = [safe_float(x) for x in (h.get("rtts") or []) if safe_float(x) is not None]
                    sent = safe_int(h.get("sent"), 0) or 0
                    recvd = safe_int(h.get("received"), 0) or 0
                    loss_pct = ((sent - recvd) / sent * 100.0) if sent > 0 else 0.0
                    ip_obj = h.get("ip") if isinstance(h.get("ip"), dict) else {}
                    loc_obj = ip_obj.get("location") if isinstance(ip_obj.get("location"), dict) else {}
                    hops.append({
                        "ttl": safe_int(h.get("ttl"), 0) or 0,
                        "name": str(h.get("name") or ""),
                        "ip": str(ip_obj.get("address") or ""),
                        "asn": safe_int(ip_obj.get("asn"), 0) or 0,
                        "aso": str(ip_obj.get("aso") or ""),
                        "geo": ", ".join(p for p in [loc_obj.get("city"), loc_obj.get("countryISO")] if p),
                        "sent": sent,
                        "received": recvd,
                        "loss_pct": round(loss_pct, 1),
                        "avg_rtt_ms": round(statistics.fmean(rtts), 1) if rtts else None,
                        "errors": h.get("errors") or [],
                    })
            hops.sort(key=lambda x: x["ttl"])
            trace_groups[key].append({
                "timestamp": str(row.get("timestamp") or ""),
                "dest": dest,
                "dest_ip": str(row.get("traceroutedestinationipaddress") or ""),
                "dest_aso": str(row.get("traceroutedestinationipaso") or ""),
                "protocol": str(row.get("tracerouteprotocol") or ""),
                "execution_context": str(row.get("executioncontext") or ""),
                "colo": str(row.get("colocode") or ""),
                "duration_ms": safe_float(row.get("traceroutedurationms"), 0.0) or 0.0,
                "hop_count": len(hops),
                "loss_hops": sum(1 for h in hops if h["loss_pct"] > 0),
                "hops": hops,
            })

    http_summary = []
    for name, runs in sorted(http_groups.items()):
        runs.sort(key=lambda r: r["timestamp"])
        totals = [r["total_ms"] for r in runs if r["total_ms"] > 0]
        ok_count = sum(1 for r in runs if r["ok"])
        http_summary.append({
            "name": name,
            "url": runs[-1]["url"],
            "runs": len(runs),
            "success_pct": round(ok_count / len(runs) * 100.0, 1),
            "avg_ms": round(statistics.fmean(totals), 1) if totals else 0.0,
            "p95_ms": round(percentile(totals, 95.0) or 0.0, 1) if totals else 0.0,
            "avg_dns_ms": round(statistics.fmean(r["dns_ms"] for r in runs), 1),
            "avg_tcp_ms": round(statistics.fmean(r["tcp_ms"] for r in runs), 1),
            "avg_tls_ms": round(statistics.fmean(r["tls_ms"] for r in runs), 1),
            "avg_ttfb_ms": round(statistics.fmean(r["ttfb_ms"] for r in runs), 1),
            "avg_dl_ms": round(statistics.fmean(r["dl_ms"] for r in runs), 1),
            "latest_status": runs[-1]["status_code"],
            "errors": [r for r in runs if not r["ok"]][-5:],
        })

    trace_summary = []
    for name, runs in sorted(trace_groups.items()):
        runs.sort(key=lambda r: r["timestamp"])
        durs = [r["duration_ms"] for r in runs]
        latest = runs[-1]
        trace_summary.append({
            "name": name,
            "dest": latest["dest"],
            "dest_ip": latest["dest_ip"],
            "dest_aso": latest["dest_aso"],
            "protocol": latest["protocol"],
            "runs": len(runs),
            "avg_duration_ms": round(statistics.fmean(durs), 1) if durs else 0.0,
            "p95_duration_ms": round(percentile(durs, 95.0) or 0.0, 1) if durs else 0.0,
            "latest_hop_count": latest["hop_count"],
            "latest_loss_hops": latest["loss_hops"],
            "latest_timestamp": latest["timestamp"],
            "latest_hops": latest["hops"],
        })

    return {
        "http": http_summary,
        "traceroute": trace_summary,
        "total_runs": len(raw_tests),
    }


def summarize_posture(raw_posture: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the most recent evaluation per posture check name."""
    latest_by_check: Dict[str, Dict[str, Any]] = {}
    for row in sorted(raw_posture, key=lambda r: str(r.get("timestamp") or "")):
        name = str(row.get("posturecheckname") or row.get("posturechecktype") or "unknown")
        raw_res = row.get("postureevaluatedresult")
        passed = raw_res if isinstance(raw_res, bool) else str(raw_res).lower() == "true"
        latest_by_check[name] = {
            "name": name,
            "type": str(row.get("posturechecktype") or ""),
            "passed": passed,
            "timestamp": str(row.get("timestamp") or ""),
        }
    return sorted(latest_by_check.values(), key=lambda x: (x["passed"], x["name"]))


def build_analytics(bundle: Dict[str, Any]) -> Dict[str, Any]:
    raw_states = bundle.get("state_events", [])
    events = [normalize_state_event(r) for r in raw_states]
    events.sort(key=lambda e: e["timestamp"])

    latest = events[-1] if events else {}
    total_n = len(events)
    connected_n = sum(1 for e in events if e["status"] == "connected")
    uptime_pct = (connected_n / total_n * 100.0) if total_n else None

    has_battery = any(
        (e["battery_pct"] is not None and e["battery_pct"] > 0)
        or e["battery_charging"]
        or e["battery_cycles"] > 0
        for e in events
    )

    ram_total_gb = next((e["ram_total_gb"] for e in reversed(events) if e["ram_total_gb"]), None)

    kpis = {
        "samples": total_n,
        "first_timestamp": events[0]["timestamp"] if events else "",
        "last_timestamp": events[-1]["timestamp"] if events else "",
        "uptime_pct": round(uptime_pct, 2) if uptime_pct is not None else None,
        "status_counts": dict(Counter(e["status"] for e in events)),
        "colo_counts": dict(Counter(e["colo"] for e in events if e["colo"])),
        "handshake_ms": stats_summary([e["handshake_ms"] for e in events if e["handshake_ms"] is not None]),
        "down_rtt_ms": stats_summary([e["down"]["rtt_ms"] for e in events if e["down"]["rtt_ms"] is not None]),
        "up_rtt_ms": stats_summary([e["up"]["rtt_ms"] for e in events if e["up"]["rtt_ms"] is not None]),
        "down_jitter_ms": stats_summary([e["down"]["jitter_ms"] for e in events if e["down"]["jitter_ms"] is not None]),
        "up_jitter_ms": stats_summary([e["up"]["jitter_ms"] for e in events if e["up"]["jitter_ms"] is not None]),
        "down_loss_pct": stats_summary([e["down"]["loss_pct"] for e in events if e["down"]["loss_pct"] is not None]),
        "up_loss_pct": stats_summary([e["up"]["loss_pct"] for e in events if e["up"]["loss_pct"] is not None]),
        "wifi_dbm": stats_summary([e["wifi_dbm"] for e in events if e["wifi_dbm"] is not None]),
        "net_rx_bps": stats_summary([e["net_rx_bps"] for e in events if e["net_rx_bps"] is not None]),
        "net_tx_bps": stats_summary([e["net_tx_bps"] for e in events if e["net_tx_bps"] is not None]),
        "cpu_pct": stats_summary([e["cpu_pct"] for e in events if e["cpu_pct"] is not None]),
        "ram_pct": stats_summary([e["ram_pct"] for e in events if e["ram_pct"] is not None]),
        "ram_total_gb": round(ram_total_gb, 1) if ram_total_gb else None,
        "ram_used_gb": stats_summary([e["ram_used_gb"] for e in events if e["ram_used_gb"] is not None]),
        "disk_pct": stats_summary([e["disk_pct"] for e in events if e["disk_pct"] is not None]),
        "disk_read_bps": stats_summary([e["disk_read_bps"] for e in events if e["disk_read_bps"] is not None]),
        "disk_write_bps": stats_summary([e["disk_write_bps"] for e in events if e["disk_write_bps"] is not None]),
        "has_battery": has_battery,
        "battery_pct": stats_summary([e["battery_pct"] for e in events if e["battery_pct"] is not None]) if has_battery else stats_summary([]),
    }

    top_cpu_apps = aggregate_top_apps(events, "cpu_apps")
    top_ram_apps = aggregate_top_apps(events, "ram_apps", ram_total_gb=ram_total_gb)
    transitions = detect_transitions(events)
    dex_tests = analyze_dex_tests(bundle.get("dex_tests", []), bundle.get("dex_test_catalog", {}))
    posture = summarize_posture(bundle.get("posture_results", []))
    warp_toggles = sorted(bundle.get("warp_toggles", []), key=lambda x: str(x.get("timestamp") or ""))
    warp_configs = sorted(bundle.get("warp_configs", []), key=lambda x: str(x.get("timestamp") or ""))

    # Automated health findings
    findings: List[Dict[str, str]] = []
    if uptime_pct is not None and uptime_pct < 95.0:
        findings.append({
            "severity": "critical",
            "title": f"Low WARP connectivity uptime ({uptime_pct:.1f}%)",
            "detail": f"Connected in {connected_n}/{total_n} samples. Check WARP toggle events and local network transitions.",
        })
    elif uptime_pct is not None and uptime_pct < 99.0:
        findings.append({
            "severity": "warning",
            "title": f"Intermittent WARP disconnects ({uptime_pct:.1f}% connected)",
            "detail": f"Non-connected samples detected ({total_n - connected_n} of {total_n}).",
        })

    max_jitter = max(
        kpis["down_jitter_ms"]["max"] or 0.0,
        kpis["up_jitter_ms"]["max"] or 0.0,
    )
    p95_jitter = max(
        kpis["down_jitter_ms"]["p95"] or 0.0,
        kpis["up_jitter_ms"]["p95"] or 0.0,
    )
    if p95_jitter > 30.0:
        findings.append({
            "severity": "critical",
            "title": f"High MASQUE tunnel jitter (P95 {p95_jitter:.1f} ms, peak {max_jitter:.1f} ms)",
            "detail": "RTT variance exceeds 30 ms — likely degrades voice/video calls and interactive SSH.",
        })
    elif max_jitter > 10.0:
        findings.append({
            "severity": "warning",
            "title": f"Elevated tunnel jitter spikes (peak {max_jitter:.1f} ms, P95 {p95_jitter:.1f} ms)",
            "detail": "Intermittent RTT variance > 10 ms detected; correlate with Wi-Fi signal and handshake latency.",
        })

    max_loss = max(kpis["down_loss_pct"]["max"] or 0.0, kpis["up_loss_pct"]["max"] or 0.0)
    avg_loss = max(kpis["down_loss_pct"]["avg"] or 0.0, kpis["up_loss_pct"]["avg"] or 0.0)
    if avg_loss > 2.0 or max_loss > 5.0:
        findings.append({
            "severity": "critical",
            "title": f"Tunnel packet loss detected (avg {avg_loss:.2f}%, peak {max_loss:.2f}%)",
            "detail": "Significant packet loss on WARP MASQUE transport; check ISP path and Wi-Fi signal.",
        })
    elif max_loss > 0.5:
        findings.append({
            "severity": "warning",
            "title": f"Minor tunnel packet loss spikes (peak {max_loss:.2f}%)",
            "detail": "Occasional dropped packets observed in MASQUE tunnel statistics.",
        })

    if kpis["wifi_dbm"]["min"] is not None and kpis["wifi_dbm"]["min"] < -70:
        findings.append({
            "severity": "warning" if kpis["wifi_dbm"]["avg"] >= -70 else "critical",
            "title": f"Weak Wi-Fi signal (min {kpis['wifi_dbm']['min']:.0f} dBm, avg {kpis['wifi_dbm']['avg']:.0f} dBm)",
            "detail": "RSSI dropped below -70 dBm, which frequently causes latency spikes and packet retransmits.",
        })

    ram_gb_suffix = f" of {kpis['ram_total_gb']} GB" if kpis["ram_total_gb"] else ""
    if kpis["ram_pct"]["avg"] is not None and kpis["ram_pct"]["avg"] >= 85.0:
        findings.append({
            "severity": "critical",
            "title": f"High memory pressure (avg {kpis['ram_pct']['avg']:.1f}%, peak {kpis['ram_pct']['max']:.1f}%)",
            "detail": f"Sustained RAM usage above 85%{ram_gb_suffix}.",
        })
    elif kpis["ram_pct"]["avg"] is not None and kpis["ram_pct"]["avg"] >= 75.0:
        findings.append({
            "severity": "warning",
            "title": f"Elevated RAM utilization (avg {kpis['ram_pct']['avg']:.1f}%, peak {kpis['ram_pct']['max']:.1f}%)",
            "detail": f"Memory is consistently above 75%{ram_gb_suffix}; monitor top RAM consumers.",
        })

    if kpis["disk_pct"]["latest"] is not None and kpis["disk_pct"]["latest"] >= 90.0:
        findings.append({
            "severity": "critical",
            "title": f"Disk nearly full ({kpis['disk_pct']['latest']:.1f}% used)",
            "detail": "Less than 10% free disk space remaining.",
        })
    elif kpis["disk_pct"]["latest"] is not None and kpis["disk_pct"]["latest"] >= 80.0:
        findings.append({
            "severity": "warning",
            "title": f"Disk usage above 80% ({kpis['disk_pct']['latest']:.1f}% used)",
            "detail": "Free space is approaching the 80% threshold.",
        })

    if kpis["cpu_pct"]["p95"] is not None and kpis["cpu_pct"]["p95"] >= 85.0:
        findings.append({
            "severity": "critical",
            "title": f"Sustained high CPU load (P95 {kpis['cpu_pct']['p95']:.1f}%, peak {kpis['cpu_pct']['max']:.1f}%)",
            "detail": "Inspect Top CPU Consumers table to identify heavy processes.",
        })

    if latest and not latest.get("firewall_enabled"):
        findings.append({
            "severity": "warning",
            "title": "OS System Firewall reported disabled",
            "detail": "`firewallenabled` is false on the latest device state event.",
        })
    elif latest and not latest.get("app_firewall_enabled"):
        findings.append({
            "severity": "info",
            "title": "Application Firewall reported disabled",
            "detail": "`appfirewallenabled` is false (while OS system firewall is enabled).",
        })

    failed_posture = [p for p in posture if not p["passed"]]
    if failed_posture:
        names = ", ".join(p["name"] for p in failed_posture[:5])
        findings.append({
            "severity": "critical",
            "title": f"{len(failed_posture)} Device Posture check(s) failing",
            "detail": f"Failed checks: {names}",
        })

    failing_http = [t for t in dex_tests["http"] if t["success_pct"] < 100.0]
    if failing_http:
        names = ", ".join(f"{t['name']} ({t['success_pct']}%)" for t in failing_http[:4])
        findings.append({
            "severity": "warning",
            "title": f"DEX HTTP test errors detected on {len(failing_http)} target(s)",
            "detail": names,
        })

    if not findings and events:
        findings.append({
            "severity": "ok",
            "title": "Device telemetry looks healthy across the observed window",
            "detail": "WARP tunnel, Wi-Fi/network, system resources, and posture metrics are within healthy thresholds.",
        })

    return {
        "device": bundle.get("device") or {},
        "meta": bundle.get("meta") or {},
        "warnings": bundle.get("warnings") or [],
        "latest": latest,
        "events": events,
        "kpis": kpis,
        "top_cpu_apps": top_cpu_apps,
        "top_ram_apps": top_ram_apps,
        "transitions": transitions,
        "dex_tests": dex_tests,
        "posture": posture,
        "warp_toggles": warp_toggles,
        "warp_configs": warp_configs,
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Terminal / Markdown Renderer
# ---------------------------------------------------------------------------

def render_terminal_report(report: Dict[str, Any], fmt: str = "text") -> str:
    dev = report["device"]
    latest = report["latest"]
    kpis = report["kpis"]
    events = report["events"]
    meta = report["meta"]
    user = dev.get("last_seen_user") if isinstance(dev.get("last_seen_user"), dict) else {}

    lines: List[str] = []
    md = fmt == "markdown"

    title = dev.get("name") or meta.get("device_id") or "Cloudflare One Device"
    if md:
        lines.append(f"# Cloudflare One Device Metrics: `{title}`")
    else:
        lines.append("=" * 84)
        lines.append(f" Cloudflare One Device Metrics: {title}")
        lines.append("=" * 84)

    lines.append(
        f"Device ID : {meta.get('device_id', dev.get('id', '—'))} | "
        f"User: {user.get('name', '—')} ({user.get('email', '—')})"
    )
    os_str = " ".join(p for p in [dev.get("device_type") or latest.get("platform"), dev.get("os_version"), dev.get("os_version_extra")] if p)
    lines.append(
        f"Hardware  : {dev.get('model', '—')} | OS: {os_str or '—'} | "
        f"WARP: {latest.get('client_version') or dev.get('client_version', '—')}"
    )
    lines.append(
        f"Window    : {meta.get('hours', '—')}h ({kpis['samples']} samples: "
        f"{kpis['first_timestamp'] or '—'} → {kpis['last_timestamp'] or '—'}) | Mode: {meta.get('mode', '—')}"
    )

    if latest:
        loc = ", ".join(p for p in [latest.get("isp_city"), latest.get("isp_state"), latest.get("isp_country")] if p)
        isp = f"{latest.get('isp_aso') or '—'} (AS{latest.get('isp_asn') or '—'})"
        net = f"{latest.get('connection_type') or '—'}"
        if latest.get("ssid"):
            net += f" · {latest['ssid']}"
        if latest.get("wifi_dbm") is not None:
            net += f" ({latest['wifi_dbm']:.0f} dBm · {wifi_quality_label(latest['wifi_dbm'])})"
        flags = (
            f"AlwaysOn={'ON' if latest['always_on'] else 'OFF'} | "
            f"SwitchLock={'ON' if latest['switch_locked'] else 'OFF'} | "
            f"SysFW={'ON' if latest['firewall_enabled'] else 'OFF'} | "
            f"AppFW={'ON' if latest['app_firewall_enabled'] else 'OFF'}"
        )
        lines.append(
            f"Latest    : status={latest['status']} | mode={latest['mode']} | "
            f"tunnel={latest['tunnel_type'] or '—'} | colo={latest['colo'] or '—'}"
        )
        lines.append(f"Network   : {net} | ISP: {isp} · {loc or '—'}")
        lines.append(f"Posture   : {flags}")

    # Findings
    lines.append("")
    lines.append("## Health Findings" if md else "--- Health Findings ---")
    sev_tag = {"critical": "[CRIT]", "warning": "[WARN]", "info": "[INFO]", "ok": "[ OK ]"}
    for f in report["findings"]:
        prefix = f"- **{sev_tag.get(f['severity'], '[INFO]')} {f['title']}** — " if md else f"  {sev_tag.get(f['severity'], '[INFO]')} {f['title']} — "
        lines.append(f"{prefix}{f['detail']}")

    if not events:
        lines.append("\nNo `dex_device_state_events` rows found for this device in the requested window.")
    else:
        # KPIs + Sparklines
        lines.append("")
        lines.append("## KPI Summary & Trends" if md else "--- KPI Summary & Trends ---")
        rows = [
            ("WARP Uptime", fmt_num(kpis["uptime_pct"], "%"), "—", "—", f"{kpis['status_counts']}"),
            ("Handshake Latency", fmt_num(kpis["handshake_ms"]["avg"], " ms"), fmt_num(kpis["handshake_ms"]["p95"], " ms"), fmt_num(kpis["handshake_ms"]["max"], " ms"), sparkline([e["handshake_ms"] for e in events])),
            ("Tunnel RTT (Down)", fmt_num(kpis["down_rtt_ms"]["avg"], " ms"), fmt_num(kpis["down_rtt_ms"]["p95"], " ms"), fmt_num(kpis["down_rtt_ms"]["max"], " ms"), sparkline([e["down"]["rtt_ms"] for e in events])),
            ("Tunnel RTT (Up)", fmt_num(kpis["up_rtt_ms"]["avg"], " ms"), fmt_num(kpis["up_rtt_ms"]["p95"], " ms"), fmt_num(kpis["up_rtt_ms"]["max"], " ms"), sparkline([e["up"]["rtt_ms"] for e in events])),
            ("Jitter (Down/Up)", f"{fmt_num(kpis['down_jitter_ms']['avg'], 'ms')}/{fmt_num(kpis['up_jitter_ms']['avg'], 'ms')}", f"{fmt_num(kpis['down_jitter_ms']['p95'], 'ms')}/{fmt_num(kpis['up_jitter_ms']['p95'], 'ms')}", f"{fmt_num(kpis['down_jitter_ms']['max'], 'ms')}/{fmt_num(kpis['up_jitter_ms']['max'], 'ms')}", sparkline([max(e["down"]["jitter_ms"] or 0, e["up"]["jitter_ms"] or 0) if (e["down"]["jitter_ms"] is not None or e["up"]["jitter_ms"] is not None) else None for e in events])),
            ("Packet Loss (D/U)", f"{fmt_num(kpis['down_loss_pct']['avg'], '%', 2)}/{fmt_num(kpis['up_loss_pct']['avg'], '%', 2)}", "—", f"{fmt_num(kpis['down_loss_pct']['max'], '%', 2)}/{fmt_num(kpis['up_loss_pct']['max'], '%', 2)}", sparkline([max(e["down"]["loss_pct"] or 0, e["up"]["loss_pct"] or 0) if (e["down"]["loss_pct"] is not None or e["up"]["loss_pct"] is not None) else None for e in events])),
            ("Wi-Fi Signal", fmt_num(kpis["wifi_dbm"]["avg"], " dBm", 0), fmt_num(kpis["wifi_dbm"]["min"], " dBm (min)", 0), fmt_num(kpis["wifi_dbm"]["max"], " dBm", 0), sparkline([e["wifi_dbm"] for e in events])),
            ("CPU Utilization", fmt_num(kpis["cpu_pct"]["avg"], "%"), fmt_num(kpis["cpu_pct"]["p95"], "%"), fmt_num(kpis["cpu_pct"]["max"], "%"), sparkline([e["cpu_pct"] for e in events])),
            ("RAM Utilization", f"{fmt_num(kpis['ram_pct']['avg'], '%')} ({fmt_num(kpis['ram_used_gb']['avg'], 'G')}/{kpis['ram_total_gb'] or '—'}G)", fmt_num(kpis["ram_pct"]["p95"], "%"), fmt_num(kpis["ram_pct"]["max"], "%"), sparkline([e["ram_pct"] for e in events])),
            ("Disk Usage", fmt_num(kpis["disk_pct"]["avg"], "%"), "—", fmt_num(kpis["disk_pct"]["max"], "%"), sparkline([e["disk_pct"] for e in events])),
            ("Disk Read / Write", f"{fmt_bytes_rate(kpis['disk_read_bps']['avg'])} / {fmt_bytes_rate(kpis['disk_write_bps']['avg'])}", "—", f"{fmt_bytes_rate(kpis['disk_read_bps']['max'])} / {fmt_bytes_rate(kpis['disk_write_bps']['max'])}", sparkline([e["disk_read_bps"] for e in events])),
            ("Network Rx / Tx", f"{fmt_bytes_rate(kpis['net_rx_bps']['avg'])} / {fmt_bytes_rate(kpis['net_tx_bps']['avg'])}", "—", f"{fmt_bytes_rate(kpis['net_rx_bps']['max'])} / {fmt_bytes_rate(kpis['net_tx_bps']['max'])}", sparkline([e["net_rx_bps"] for e in events])),
        ]
        if kpis["has_battery"]:
            rows.append(("Battery Level", fmt_num(kpis["battery_pct"]["avg"], "%", 0), fmt_num(kpis["battery_pct"]["min"], "% (min)", 0), fmt_num(kpis["battery_pct"]["max"], "%", 0), sparkline([e["battery_pct"] for e in events])))
        else:
            rows.append(("Battery", "Desktop / AC-only", "—", "—", "(no battery detected)"))

        if md:
            lines.append("| Metric | Avg | P95 / Min | Peak | Trend |")
            lines.append("| :--- | :--- | :--- | :--- | :--- |")
            for r in rows:
                lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | `{r[4]}` |")
        else:
            lines.append(f"{'Metric':<20} {'Avg':<22} {'P95 / Min':<15} {'Peak':<19} Trend")
            lines.append("-" * 108)
            for r in rows:
                lines.append(f"{r[0]:<20} {r[1]:<22} {r[2]:<15} {r[3]:<19} {r[4]}")

        # Top Apps
        if report["top_cpu_apps"] or report["top_ram_apps"]:
            lines.append("")
            lines.append("## Top Resource-Consuming Apps" if md else "--- Top Resource-Consuming Apps ---")
            if report["top_cpu_apps"]:
                lines.append("Top CPU: " + ", ".join(f"{a['name']} (avg {a['avg_pct']}%, peak {a['peak_pct']}%)" for a in report["top_cpu_apps"][:6]))
            if report["top_ram_apps"]:
                ram_parts = []
                for a in report["top_ram_apps"][:6]:
                    mb_note = f" ~{a['avg_mb']:.0f}MB" if "avg_mb" in a else ""
                    ram_parts.append(f"{a['name']} (avg {a['avg_pct']}%{mb_note}, peak {a['peak_pct']}%)")
                lines.append("Top RAM: " + ", ".join(ram_parts))

    # Enriched sections
    dex = report["dex_tests"]
    if dex["http"] or dex["traceroute"]:
        lines.append("")
        lines.append("## Enriched: DEX Application Tests" if md else "--- Enriched: DEX Application Tests ---")
        for t in dex["http"]:
            lines.append(
                f"  HTTP  {t['name']} | {t['runs']} runs | OK {t['success_pct']}% | "
                f"avg {t['avg_ms']} ms (DNS {t['avg_dns_ms']} · TCP {t['avg_tcp_ms']} · "
                f"TLS {t['avg_tls_ms']} · TTFB {t['avg_ttfb_ms']} · DL {t['avg_dl_ms']} ms)"
            )
        for t in dex["traceroute"]:
            lines.append(
                f"  TRACE {t['name']} ({t['dest']}) | {t['runs']} runs | "
                f"avg {t['avg_duration_ms']} ms | {t['latest_hop_count']} hops ({t['latest_loss_hops']} with loss)"
            )

    if report["posture"]:
        lines.append("")
        lines.append("## Enriched: Device Posture Checks" if md else "--- Enriched: Device Posture Checks ---")
        passed = sum(1 for p in report["posture"] if p["passed"])
        lines.append(f"  {passed}/{len(report['posture'])} checks passing:")
        for p in report["posture"]:
            lines.append(f"  [{'PASS' if p['passed'] else 'FAIL'}] {p['name']} ({p['type']}) @ {p['timestamp']}")

    if report["transitions"] or report["warp_toggles"] or report["warp_configs"]:
        lines.append("")
        lines.append("## State Transitions & WARP Events" if md else "--- State Transitions & WARP Events ---")
        for tr in report["transitions"][-10:]:
            lines.append(f"  {tr['timestamp']}  {tr['dimension']}: {tr['from']} -> {tr['to']}")
        for tg in report["warp_toggles"][-10:]:
            lines.append(f"  {tg.get('timestamp')}  WARP Toggle: {'ON' if tg.get('toggled') else 'OFF'} ({tg.get('useremail', '')})")
        for cfg in report["warp_configs"][-10:]:
            lines.append(f"  {cfg.get('timestamp')}  WARP Profile: {cfg.get('confignamefrom')} -> {cfg.get('confignameto')}")

    if report["warnings"]:
        lines.append("")
        lines.append("## Notes / Partial Data Warnings" if md else "--- Notes ---")
        for w in report["warnings"]:
            lines.append(f"  * {w}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-Contained Interactive HTML Dashboard Generator (Zero CDN Dependencies)
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__PAGE_TITLE__</title>
<style>
  :root {
    --bg: #0b0f17;
    --surface: #131926;
    --surface-2: #1b2334;
    --border: #263147;
    --text: #e6edf7;
    --muted: #8b9bb4;
    --accent: #f38020;
    --accent-soft: rgba(243, 128, 32, 0.16);
    --green: #22c55e;
    --yellow: #eab308;
    --red: #ef4444;
    --blue: #38bdf8;
    --purple: #a78bfa;
    --teal: #2dd4bf;
    --grid: rgba(148, 163, 184, 0.14);
    --shadow: 0 8px 24px rgba(0,0,0,0.35);
  }
  [data-theme="light"] {
    --bg: #f4f6fa;
    --surface: #ffffff;
    --surface-2: #f8fafc;
    --border: #dde3ee;
    --text: #0f172a;
    --muted: #5b6b82;
    --accent: #e56a08;
    --accent-soft: rgba(229, 106, 8, 0.12);
    --green: #16a34a;
    --yellow: #ca8a04;
    --red: #dc2626;
    --blue: #0284c7;
    --purple: #7c3aed;
    --teal: #0d9488;
    --grid: rgba(15, 23, 42, 0.09);
    --shadow: 0 6px 20px rgba(15, 23, 42, 0.08);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.45;
  }
  .wrap { max-width: 1380px; margin: 0 auto; padding: 20px 22px 48px; }
  .topbar {
    display: flex; justify-content: space-between; align-items: center; gap: 14px;
    flex-wrap: wrap; margin-bottom: 18px;
  }
  .brand { display: flex; align-items: center; gap: 10px; }
  .brand-dot {
    width: 12px; height: 12px; border-radius: 50%; background: var(--accent);
    box-shadow: 0 0 12px var(--accent);
  }
  .brand h1 { margin: 0; font-size: 20px; font-weight: 700; letter-spacing: -0.01em; }
  .brand .sub { font-size: 12px; color: var(--muted); }
  .controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .btn {
    background: var(--surface); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 6px 11px; font-size: 12px; font-weight: 600;
    cursor: pointer; transition: all 0.15s;
  }
  .btn:hover { border-color: var(--accent); color: var(--accent); }
  .btn.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 16px 18px; box-shadow: var(--shadow);
    margin-bottom: 16px;
  }
  .card-title {
    display: flex; justify-content: space-between; align-items: center; gap: 8px;
    margin: 0 0 12px; font-size: 14px; font-weight: 700; letter-spacing: 0.01em;
  }
  .card-title .hint { font-size: 12px; color: var(--muted); font-weight: 500; }
  .header-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px;
  }
  .meta-box {
    background: var(--surface-2); border: 1px solid var(--border);
    border-radius: 9px; padding: 10px 12px;
  }
  .meta-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
  .meta-val { font-size: 14px; font-weight: 600; margin-top: 2px; word-break: break-word; }
  .meta-sub { font-size: 12px; color: var(--muted); margin-top: 2px; }
  .badges { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
  .badge {
    display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 600;
    padding: 3px 9px; border-radius: 999px; border: 1px solid var(--border);
    background: var(--surface-2);
  }
  .badge.ok { border-color: rgba(34,197,94,0.45); color: var(--green); background: rgba(34,197,94,0.1); }
  .badge.warn { border-color: rgba(234,179,8,0.5); color: var(--yellow); background: rgba(234,179,8,0.12); }
  .badge.crit { border-color: rgba(239,68,68,0.5); color: var(--red); background: rgba(239,68,68,0.12); }
  .badge.info { border-color: rgba(56,189,248,0.45); color: var(--blue); background: rgba(56,189,248,0.1); }
  .findings { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 10px; }
  .finding {
    border-left: 4px solid var(--blue); background: var(--surface-2);
    border-radius: 8px; padding: 10px 12px;
  }
  .finding.critical { border-left-color: var(--red); }
  .finding.warning { border-left-color: var(--yellow); }
  .finding.ok { border-left-color: var(--green); }
  .finding-t { font-size: 13px; font-weight: 700; }
  .finding-d { font-size: 12px; color: var(--muted); margin-top: 3px; }
  .kpi-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 12px;
    margin-bottom: 16px;
  }
  .kpi {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 11px; padding: 13px 14px; box-shadow: var(--shadow);
  }
  .kpi-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .kpi-value { font-size: 23px; font-weight: 700; margin: 4px 0 2px; }
  .kpi-sub { font-size: 11.5px; color: var(--muted); }
  .chart-box { position: relative; width: 100%; }
  .chart-legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .leg-item { display: inline-flex; align-items: center; gap: 6px; }
  .leg-swatch { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
  svg.chart { width: 100%; display: block; overflow: visible; user-select: none; }
  .tooltip {
    position: fixed; pointer-events: none; z-index: 99; display: none;
    background: var(--surface); border: 1px solid var(--accent); border-radius: 9px;
    padding: 9px 12px; font-size: 12px; box-shadow: 0 12px 30px rgba(0,0,0,0.45);
    max-width: 340px;
  }
  .tooltip .tt-time { font-weight: 700; color: var(--accent); margin-bottom: 5px; font-family: ui-monospace, monospace; }
  .tooltip .tt-row { display: flex; justify-content: space-between; gap: 14px; margin: 2px 0; }
  .two-col { display: grid; grid-template-columns: repeat(auto-fit, minmax(460px, 1fr)); gap: 16px; }
  .bar-row { margin-bottom: 9px; }
  .bar-head { display: flex; justify-content: space-between; font-size: 12.5px; margin-bottom: 3px; gap: 8px; }
  .bar-name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bar-val { color: var(--muted); font-family: ui-monospace, monospace; font-size: 11.5px; white-space: nowrap; }
  .bar-track { height: 8px; border-radius: 999px; background: var(--surface-2); overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 999px; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
  th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  .waterfall { display: flex; height: 10px; border-radius: 5px; overflow: hidden; background: var(--surface-2); min-width: 140px; }
  .wf-dns { background: #38bdf8; }
  .wf-tcp { background: #f38020; }
  .wf-tls { background: #a78bfa; }
  .wf-ttfb { background: #22c55e; }
  .wf-dl { background: #eab308; }
  details { margin-top: 8px; border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px; background: var(--surface-2); }
  summary { cursor: pointer; font-weight: 600; font-size: 12.5px; }
  .ribbon-row { display: flex; align-items: center; gap: 10px; margin-bottom: 7px; }
  .ribbon-label { width: 88px; font-size: 11.5px; color: var(--muted); flex-shrink: 0; }
  .ribbon-strip { display: flex; flex: 1; height: 18px; border-radius: 5px; overflow: hidden; border: 1px solid var(--border); }
  .ribbon-seg { height: 100%; transition: opacity 0.12s; }
  .ribbon-seg:hover { opacity: 0.8; }
  .empty { color: var(--muted); font-size: 13px; padding: 10px 0; }
  @media (max-width: 640px) { .two-col { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">
      <span class="brand-dot"></span>
      <div>
        <h1 id="pageTitle">Cloudflare One Device Metrics</h1>
        <div class="sub" id="pageSub"></div>
      </div>
    </div>
    <div class="controls">
      <div id="rangeButtons">
        <button class="btn active" data-hours="0">All</button>
        <button class="btn" data-hours="1">1h</button>
        <button class="btn" data-hours="4">4h</button>
        <button class="btn" data-hours="12">12h</button>
      </div>
      <button class="btn" id="themeBtn">Toggle Theme</button>
    </div>
  </div>

  <div class="card" id="headerCard"></div>
  <div class="card">
    <div class="card-title"><span>Automated Health Findings</span><span class="hint" id="findingCount"></span></div>
    <div class="findings" id="findingsBox"></div>
  </div>

  <div class="kpi-grid" id="kpiGrid"></div>

  <div class="card">
    <div class="card-title"><span>Connectivity &amp; Path Timeline</span><span class="hint">Hover any point across charts to correlate events</span></div>
    <div id="ribbonBox"></div>
  </div>

  <div class="card">
    <div class="card-title"><span>WARP / MASQUE Tunnel Latency, Jitter &amp; Loss</span><span class="hint" id="tunnelHint"></span></div>
    <div class="chart-legend">
      <span class="leg-item"><span class="leg-swatch" style="background:#f38020"></span>Handshake (ms)</span>
      <span class="leg-item"><span class="leg-swatch" style="background:#38bdf8"></span>Downstream RTT (ms)</span>
      <span class="leg-item"><span class="leg-swatch" style="background:#a78bfa"></span>Upstream RTT (ms)</span>
      <span class="leg-item"><span class="leg-swatch" style="background:#2dd4bf"></span>Max Jitter rttVar (ms)</span>
      <span class="leg-item"><span class="leg-swatch" style="background:#ef4444"></span>Packet Loss &gt; 0%</span>
    </div>
    <div class="chart-box" id="chartTunnel"></div>
  </div>

  <div class="two-col">
    <div class="card">
      <div class="card-title"><span>Wi-Fi Signal Strength &amp; Network Throughput</span><span class="hint" id="wifiHint"></span></div>
      <div class="chart-legend">
        <span class="leg-item"><span class="leg-swatch" style="background:#22c55e"></span>Wi-Fi RSSI (dBm)</span>
        <span class="leg-item"><span class="leg-swatch" style="background:#38bdf8"></span>Rx KB/s</span>
        <span class="leg-item"><span class="leg-swatch" style="background:#f38020"></span>Tx KB/s</span>
      </div>
      <div class="chart-box" id="chartWifi"></div>
    </div>
    <div class="card">
      <div class="card-title"><span>System Resources (CPU, RAM, Disk &amp; Battery)</span><span class="hint" id="sysHint"></span></div>
      <div class="chart-legend">
        <span class="leg-item"><span class="leg-swatch" style="background:#38bdf8"></span>CPU %</span>
        <span class="leg-item"><span class="leg-swatch" style="background:#f38020"></span>RAM %</span>
        <span class="leg-item"><span class="leg-swatch" style="background:#a78bfa"></span>Disk %</span>
        <span class="leg-item" id="legBatt" style="display:none"><span class="leg-swatch" style="background:#22c55e"></span>Battery %</span>
      </div>
      <div class="chart-box" id="chartSys"></div>
    </div>
  </div>

  <div class="two-col">
    <div class="card">
      <div class="card-title"><span>Top CPU-Consuming Applications</span><span class="hint">Aggregated across window</span></div>
      <div id="topCpuBox"></div>
    </div>
    <div class="card">
      <div class="card-title"><span>Top Memory-Consuming Applications</span><span class="hint" id="ramTotalHint"></span></div>
      <div id="topRamBox"></div>
    </div>
  </div>

  <div class="card" id="dexTestsCard">
    <div class="card-title"><span>Enriched: DEX Synthetic Application Tests</span><span class="hint" id="dexTestHint"></span></div>
    <div id="dexTestsBox"></div>
  </div>

  <div class="two-col">
    <div class="card">
      <div class="card-title"><span>Enriched: Device Posture Evaluations</span><span class="hint" id="postureHint"></span></div>
      <div id="postureBox"></div>
    </div>
    <div class="card">
      <div class="card-title"><span>State Transitions &amp; WARP Change Events</span><span class="hint" id="eventsHint"></span></div>
      <div id="eventsBox"></div>
    </div>
  </div>
</div>

<div class="tooltip" id="tooltip"></div>

<script>
const REPORT = __REPORT_JSON__;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, u = "", d = 1) => (v === null || v === undefined || Number.isNaN(v)) ? "—" : `${Number(v).toFixed(d)}${u}`;
const fmtTime = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toISOString().replace("T", " ").slice(5, 19) + "Z";
};
const fmtShort = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().slice(11, 16);
};
const fmtRate = (bps) => {
  if (bps === null || bps === undefined) return "—";
  if (bps >= 1048576) return `${(bps / 1048576).toFixed(2)} MB/s`;
  if (bps >= 1024) return `${(bps / 1024).toFixed(1)} KB/s`;
  return `${Math.round(bps)} B/s`;
};
const wifiBand = (dbm) => dbm === null || dbm === undefined ? "N/A" : dbm >= -50 ? "Excellent" : dbm >= -60 ? "Good" : dbm >= -70 ? "Fair" : dbm >= -80 ? "Weak" : "Poor";

let activeHours = 0;
let currentEvents = REPORT.events || [];
const chartRegistry = [];

function filterEvents(hours) {
  const all = REPORT.events || [];
  if (!hours || !all.length) return all;
  const endMs = new Date(all[all.length - 1].timestamp).getTime();
  const cutoff = endMs - hours * 3600 * 1000;
  return all.filter((e) => new Date(e.timestamp).getTime() >= cutoff);
}

function renderHeader() {
  const dev = REPORT.device || {};
  const latest = REPORT.latest || {};
  const meta = REPORT.meta || {};
  const user = dev.last_seen_user || {};
  const name = dev.name || meta.device_id || "Cloudflare One Client";
  document.getElementById("pageTitle").textContent = `Cloudflare One Device Metrics · ${name}`;
  document.getElementById("pageSub").textContent =
    `Account ${meta.account_id || "—"} · ${REPORT.kpis.samples} samples · ${fmtTime(REPORT.kpis.first_timestamp)} → ${fmtTime(REPORT.kpis.last_timestamp)}`;

  const loc = [latest.isp_city, latest.isp_state, latest.isp_country].filter(Boolean).join(", ");
  const statusCls = latest.status === "connected" ? "ok" : latest.status === "paused" ? "warn" : "crit";
  const osStr = [dev.device_type || latest.platform, dev.os_version, dev.os_version_extra].filter(Boolean).join(" ");

  document.getElementById("headerCard").innerHTML = `
    <div class="header-grid">
      <div class="meta-box">
        <div class="meta-label">User &amp; Device</div>
        <div class="meta-val">${esc(user.name || name)}</div>
        <div class="meta-sub">${esc(user.email || "—")} · <span class="mono">${esc(meta.device_id || dev.id || "—")}</span></div>
      </div>
      <div class="meta-box">
        <div class="meta-label">Hardware &amp; WARP Client</div>
        <div class="meta-val">${esc(dev.model || latest.platform || "—")} · ${esc(osStr || "—")}</div>
        <div class="meta-sub">WARP ${esc(latest.client_version || dev.client_version || "—")} · Profile: ${esc(latest.profile_id || "—")}</div>
      </div>
      <div class="meta-box">
        <div class="meta-label">Cloudflare Edge &amp; Tunnel</div>
        <div class="meta-val">${esc((latest.colo || "—").toUpperCase())} · ${esc((latest.tunnel_type || "—").toUpperCase())} (${esc(latest.mode || "—")})</div>
        <div class="meta-sub">Handshake: ${fmt(latest.handshake_ms, " ms", 0)} · Local IP: ${esc(latest.device_ip || "—")}</div>
      </div>
      <div class="meta-box">
        <div class="meta-label">Network &amp; ISP</div>
        <div class="meta-val">${esc(latest.isp_aso || "—")} ${latest.isp_asn ? `(AS${latest.isp_asn})` : ""}</div>
        <div class="meta-sub">${esc(loc || "—")} · ${esc(latest.connection_type || "—")}${latest.ssid ? ` · ${esc(latest.ssid)}` : ""}${latest.wifi_dbm !== null && latest.wifi_dbm !== undefined ? ` (${latest.wifi_dbm} dBm · ${wifiBand(latest.wifi_dbm)})` : ""}</div>
      </div>
    </div>
    <div class="badges">
      <span class="badge ${statusCls}">Status: ${esc(latest.status || "unknown")}</span>
      <span class="badge ${latest.always_on ? "ok" : "warn"}">Always-On: ${latest.always_on ? "ON" : "OFF"}</span>
      <span class="badge ${latest.switch_locked ? "ok" : "info"}">Switch Lock: ${latest.switch_locked ? "Locked" : "Unlocked"}</span>
      <span class="badge ${latest.firewall_enabled ? "ok" : "warn"}">System Firewall: ${latest.firewall_enabled ? "ON" : "OFF"}</span>
      <span class="badge ${latest.app_firewall_enabled ? "ok" : "info"}">App Firewall: ${latest.app_firewall_enabled ? "ON" : "OFF"}</span>
      ${dev.serial_number ? `<span class="badge info">Serial: ${esc(dev.serial_number)}</span>` : ""}
    </div>`;
}

function renderFindings() {
  const list = REPORT.findings || [];
  document.getElementById("findingCount").textContent = `${list.length} item(s)`;
  document.getElementById("findingsBox").innerHTML = list.map((f) => `
    <div class="finding ${esc(f.severity)}">
      <div class="finding-t">${esc(f.title)}</div>
      <div class="finding-d">${esc(f.detail)}</div>
    </div>`).join("");
}

function renderKpis(evs) {
  const vals = (fn) => evs.map(fn).filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
  const avg = (a) => a.length ? a.reduce((s, x) => s + x, 0) / a.length : null;
  const max = (a) => a.length ? Math.max(...a) : null;
  const min = (a) => a.length ? Math.min(...a) : null;
  const p95 = (a) => {
    if (!a.length) return null;
    const s = [...a].sort((x, y) => x - y);
    return s[Math.min(s.length - 1, Math.floor(0.95 * (s.length - 1)))];
  };

  const connPct = evs.length ? (evs.filter((e) => e.status === "connected").length / evs.length) * 100 : null;
  const hs = vals((e) => e.handshake_ms);
  const rttD = vals((e) => e.down?.rtt_ms);
  const rttU = vals((e) => e.up?.rtt_ms);
  const jit = vals((e) => (e.down?.jitter_ms ?? e.up?.jitter_ms) !== null ? Math.max(e.down?.jitter_ms || 0, e.up?.jitter_ms || 0) : null);
  const loss = vals((e) => (e.down?.loss_pct ?? e.up?.loss_pct) !== null ? Math.max(e.down?.loss_pct || 0, e.up?.loss_pct || 0) : null);
  const wifi = vals((e) => e.wifi_dbm);
  const cpu = vals((e) => e.cpu_pct);
  const ram = vals((e) => e.ram_pct);
  const disk = vals((e) => e.disk_pct);
  const colos = [...new Set(evs.map((e) => e.colo).filter(Boolean))];

  const cards = [
    { l: "WARP Uptime", v: fmt(connPct, "%", 1), s: `${evs.length} samples · Colos: ${colos.join(", ") || "—"}`, c: connPct >= 99 ? "var(--green)" : connPct >= 95 ? "var(--yellow)" : "var(--red)" },
    { l: "Handshake Latency", v: fmt(avg(hs), " ms", 1), s: `P95 ${fmt(p95(hs), " ms", 1)} · Peak ${fmt(max(hs), " ms", 0)}`, c: (p95(hs) || 0) > 150 ? "var(--red)" : (p95(hs) || 0) > 50 ? "var(--yellow)" : "var(--blue)" },
    { l: "Tunnel RTT (D / U)", v: `${fmt(avg(rttD), "", 1)} / ${fmt(avg(rttU), " ms", 1)}`, s: `P95 ${fmt(p95(rttD), "ms", 1)} / ${fmt(p95(rttU), "ms", 1)}`, c: "var(--purple)" },
    { l: "Tunnel Jitter & Loss", v: fmt(avg(jit), " ms", 1), s: `Peak jitter ${fmt(max(jit), " ms", 1)} · Peak loss ${fmt(max(loss), "%", 2)}`, c: (max(jit) || 0) > 30 || (max(loss) || 0) > 2 ? "var(--red)" : (max(jit) || 0) > 10 ? "var(--yellow)" : "var(--teal)" },
    { l: "Wi-Fi Signal", v: wifi.length ? `${avg(wifi).toFixed(0)} dBm` : "Ethernet / N/A", s: wifi.length ? `${wifiBand(avg(wifi))} · Min ${min(wifi).toFixed(0)} dBm` : "No Wi-Fi RSSI in range", c: !wifi.length ? "var(--muted)" : avg(wifi) >= -60 ? "var(--green)" : avg(wifi) >= -70 ? "var(--yellow)" : "var(--red)" },
    { l: "CPU Usage", v: fmt(avg(cpu), "%", 1), s: `P95 ${fmt(p95(cpu), "%", 1)} · Peak ${fmt(max(cpu), "%", 1)}`, c: (p95(cpu) || 0) > 85 ? "var(--red)" : "var(--blue)" },
    { l: "RAM Usage", v: fmt(avg(ram), "%", 1), s: `${REPORT.kpis.ram_total_gb ? `${fmt((avg(ram) || 0) * REPORT.kpis.ram_total_gb / 100, " / " + REPORT.kpis.ram_total_gb + " GB", 1)}` : ""} · Peak ${fmt(max(ram), "%", 1)}`, c: (avg(ram) || 0) >= 85 ? "var(--red)" : (avg(ram) || 0) >= 75 ? "var(--yellow)" : "var(--accent)" },
    { l: "Disk Usage", v: fmt(disk.length ? disk[disk.length - 1] : null, "%", 1), s: `Read avg ${fmtRate(avg(vals((e) => e.disk_read_bps)))}`, c: (disk[disk.length - 1] || 0) >= 90 ? "var(--red)" : (disk[disk.length - 1] || 0) >= 80 ? "var(--yellow)" : "var(--purple)" },
  ];

  document.getElementById("kpiGrid").innerHTML = cards.map((k) => `
    <div class="kpi">
      <div class="kpi-label">${esc(k.l)}</div>
      <div class="kpi-value" style="color:${k.c}">${esc(k.v)}</div>
      <div class="kpi-sub">${esc(k.s)}</div>
    </div>`).join("");
}

function renderRibbon(evs) {
  const box = document.getElementById("ribbonBox");
  if (!evs.length) { box.innerHTML = '<div class="empty">No state events in selected range.</div>'; return; }
  const palette = ["#22c55e", "#38bdf8", "#f38020", "#a78bfa", "#2dd4bf", "#eab308", "#f472b6"];
  const colorFor = (dim, val) => {
    if (dim === "status") return val === "connected" ? "#22c55e" : val === "paused" ? "#eab308" : "#ef4444";
    let h = 0; for (let i = 0; i < val.length; i++) h = (h * 31 + val.charCodeAt(i)) >>> 0;
    return palette[h % palette.length];
  };
  const dims = [
    ["WARP Status", "status"],
    ["Edge Colo", "colo"],
    ["Network", "ssid_or_conn"],
    ["ISP", "isp_aso"],
  ];
  box.innerHTML = dims.map(([label, key]) => {
    const segs = [];
    for (const e of evs) {
      const val = key === "ssid_or_conn" ? (e.ssid || e.connection_type || "unknown") : (e[key] || "none");
      if (segs.length && segs[segs.length - 1].val === val) segs[segs.length - 1].count++;
      else segs.push({ val, count: 1, start: e.timestamp });
    }
    const htmlSegs = segs.map((s) => {
      const w = (s.count / evs.length) * 100;
      return `<div class="ribbon-seg" style="width:${w}%;background:${colorFor(key, s.val)}" title="${esc(label)}: ${esc(s.val)} (${s.count} samples, from ${fmtTime(s.start)})"></div>`;
    }).join("");
    const legend = [...new Set(segs.map((s) => s.val))].slice(0, 5).join(" · ");
    return `<div class="ribbon-row"><div class="ribbon-label">${esc(label)}</div><div class="ribbon-strip">${htmlSegs}</div><div class="ribbon-label" style="width:180px;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(legend)}</div></div>`;
  }).join("");
}

function drawLineChart(containerId, evs, seriesList, opts = {}) {
  const el = document.getElementById(containerId);
  if (!evs.length) { el.innerHTML = '<div class="empty">No data points in selected range.</div>'; return; }

  const W = Math.max(520, el.clientWidth || 620);
  const H = opts.height || 220;
  const pad = { l: 48, r: 16, t: 14, b: 28 };
  const iw = W - pad.l - pad.r;
  const ih = H - pad.t - pad.b;

  const allVals = [];
  for (const s of seriesList) {
    for (const e of evs) {
      const v = s.fn(e);
      if (v !== null && v !== undefined && !Number.isNaN(v)) allVals.push(v);
    }
  }
  if (!allVals.length) { el.innerHTML = '<div class="empty">No numeric values for these series.</div>'; return; }

  let yMin = opts.yMin !== undefined ? opts.yMin : Math.min(...allVals);
  let yMax = opts.yMax !== undefined ? opts.yMax : Math.max(...allVals);
  if (yMax - yMin < 1e-6) { yMin -= 1; yMax += 1; }
  const margin = (yMax - yMin) * 0.08;
  if (opts.yMin === undefined) yMin = opts.allowNegative ? yMin - margin : Math.max(0, yMin - margin);
  if (opts.yMax === undefined) yMax = yMax + margin;

  const xOf = (i) => pad.l + (evs.length === 1 ? iw / 2 : (i / (evs.length - 1)) * iw);
  const yOf = (v) => pad.t + ih - ((v - yMin) / (yMax - yMin)) * ih;

  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" data-chart="${containerId}">`;

  // Optional threshold bands (e.g. Wi-Fi quality)
  if (opts.bands) {
    for (const b of opts.bands) {
      const y1 = Math.max(pad.t, Math.min(pad.t + ih, yOf(b.hi)));
      const y2 = Math.max(pad.t, Math.min(pad.t + ih, yOf(b.lo)));
      if (y2 > y1) svg += `<rect x="${pad.l}" y="${y1}" width="${iw}" height="${y2 - y1}" fill="${b.fill}" />`;
    }
  }

  // Horizontal grid + Y ticks
  const ticks = 4;
  for (let t = 0; t <= ticks; t++) {
    const val = yMin + (t / ticks) * (yMax - yMin);
    const y = yOf(val);
    svg += `<line x1="${pad.l}" y1="${y}" x2="${W - pad.r}" y2="${y}" stroke="var(--grid)" stroke-dasharray="3,3" />`;
    svg += `<text x="${pad.l - 7}" y="${y + 4}" text-anchor="end" fill="var(--muted)" font-size="10.5" font-family="ui-monospace,monospace">${val.toFixed(opts.yDecimals ?? 0)}${opts.yUnit || ""}</text>`;
  }

  // X ticks
  const xTicks = Math.min(6, evs.length);
  for (let t = 0; t < xTicks; t++) {
    const idx = xTicks === 1 ? 0 : Math.round((t / (xTicks - 1)) * (evs.length - 1));
    const x = xOf(idx);
    svg += `<text x="${x}" y="${H - 7}" text-anchor="middle" fill="var(--muted)" font-size="10.5" font-family="ui-monospace,monospace">${fmtShort(evs[idx].timestamp)}</text>`;
  }

  // Series paths
  for (const s of seriesList) {
    let d = "";
    let started = false;
    const pts = [];
    evs.forEach((e, i) => {
      const v = s.fn(e);
      if (v === null || v === undefined || Number.isNaN(v)) { started = false; return; }
      const x = xOf(i), y = yOf(v);
      pts.push([x, y, i, v]);
      d += (started ? " L " : " M ") + `${x.toFixed(1)} ${y.toFixed(1)}`;
      started = true;
    });
    if (d) {
      svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width || 2}" ${s.dash ? `stroke-dasharray="${s.dash}"` : ""} />`;
    }
    if (evs.length <= 40) {
      for (const [x, y] of pts) svg += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2.6" fill="${s.color}" />`;
    }
  }

  // Optional red anomaly dots (e.g. packet loss > 0)
  if (opts.markerFn) {
    evs.forEach((e, i) => {
      const mv = opts.markerFn(e);
      if (mv) {
        const x = xOf(i);
        svg += `<circle cx="${x.toFixed(1)}" cy="${pad.t + 7}" r="4" fill="#ef4444" stroke="var(--surface)" stroke-width="1.2" />`;
      }
    });
  }

  svg += `<line class="crosshair" x1="${pad.l}" y1="${pad.t}" x2="${pad.l}" y2="${pad.t + ih}" stroke="var(--accent)" stroke-width="1.2" stroke-dasharray="3,2" style="display:none" />`;
  svg += `<rect class="hit" x="${pad.l}" y="${pad.t}" width="${iw}" height="${ih}" fill="transparent" style="cursor:crosshair" />`;
  svg += `</svg>`;
  el.innerHTML = svg;

  const svgEl = el.querySelector("svg");
  const cross = svgEl.querySelector(".crosshair");
  const hit = svgEl.querySelector(".hit");
  const reg = { cross, xOf, evs };
  chartRegistry.push(reg);

  hit.addEventListener("mousemove", (evt) => {
    const rect = svgEl.getBoundingClientRect();
    const relX = ((evt.clientX - rect.left) / rect.width) * W;
    const norm = Math.max(0, Math.min(1, (relX - pad.l) / iw));
    const idx = Math.round(norm * (evs.length - 1));
    syncCrosshair(idx, evt.clientX, evt.clientY);
  });
  hit.addEventListener("mouseleave", () => hideCrosshair());
}

function syncCrosshair(idx, cx, cy) {
  for (const c of chartRegistry) {
    const x = c.xOf(idx);
    c.cross.setAttribute("x1", x);
    c.cross.setAttribute("x2", x);
    c.cross.style.display = "block";
  }
  const e = currentEvents[idx];
  if (!e) return;
  const tt = document.getElementById("tooltip");
  const rows = [
    ["Status / Colo", `${e.status} · ${e.colo || "—"} (${e.tunnel_type || "—"})`],
    ["Handshake / RTT D/U", `${fmt(e.handshake_ms, "ms", 0)} · ${fmt(e.down?.rtt_ms, "ms", 1)} / ${fmt(e.up?.rtt_ms, "ms", 1)}`],
    ["Jitter D / U", `${fmt(e.down?.jitter_ms, "ms", 1)} / ${fmt(e.up?.jitter_ms, "ms", 1)}`],
    ["Loss D / U", `${fmt(e.down?.loss_pct, "%", 2)} / ${fmt(e.up?.loss_pct, "%", 2)}`],
    ["Wi-Fi / Net", `${e.wifi_dbm !== null ? `${e.wifi_dbm} dBm` : e.connection_type} · Rx ${fmtRate(e.net_rx_bps)} / Tx ${fmtRate(e.net_tx_bps)}`],
    ["CPU / RAM / Disk", `${fmt(e.cpu_pct, "%", 1)} · ${fmt(e.ram_pct, "%", 1)} · ${fmt(e.disk_pct, "%", 1)}`],
  ];
  tt.innerHTML = `<div class="tt-time">${fmtTime(e.timestamp)}</div>` +
    rows.map(([k, v]) => `<div class="tt-row"><span style="color:var(--muted)">${esc(k)}</span><span class="mono">${esc(v)}</span></div>`).join("");
  tt.style.display = "block";
  const left = cx + 16 + tt.offsetWidth > window.innerWidth ? cx - tt.offsetWidth - 14 : cx + 16;
  const top = cy + 14 + tt.offsetHeight > window.innerHeight ? cy - tt.offsetHeight - 14 : cy + 14;
  tt.style.left = `${Math.max(8, left)}px`;
  tt.style.top = `${Math.max(8, top)}px`;
}

function hideCrosshair() {
  for (const c of chartRegistry) c.cross.style.display = "none";
  document.getElementById("tooltip").style.display = "none";
}

function renderTopApps() {
  const renderList = (elId, items, color, isRam) => {
    const box = document.getElementById(elId);
    if (!items || !items.length) { box.innerHTML = '<div class="empty">No application telemetry reported.</div>'; return; }
    const peakMax = Math.max(...items.map((a) => a.peak_pct), 1);
    box.innerHTML = items.slice(0, 8).map((a) => {
      const w = Math.max(3, (a.avg_pct / peakMax) * 100);
      const mbStr = isRam && a.avg_mb ? ` (~${a.avg_mb >= 1024 ? (a.avg_mb / 1024).toFixed(1) + " GB" : Math.round(a.avg_mb) + " MB"})` : "";
      return `
        <div class="bar-row">
          <div class="bar-head">
            <span class="bar-name" title="${esc(a.name)}">${esc(a.name)}</span>
            <span class="bar-val">avg ${a.avg_pct.toFixed(1)}%${mbStr} · peak ${a.peak_pct.toFixed(1)}% · ${a.presence_pct}% of samples</span>
          </div>
          <div class="bar-track"><div class="bar-fill" style="width:${w}%;background:${color}"></div></div>
        </div>`;
    }).join("");
  };
  document.getElementById("ramTotalHint").textContent = REPORT.kpis.ram_total_gb ? `Installed RAM: ${REPORT.kpis.ram_total_gb} GB` : "Aggregated across window";
  renderList("topCpuBox", REPORT.top_cpu_apps, "var(--blue)", false);
  renderList("topRamBox", REPORT.top_ram_apps, "var(--accent)", true);
}

function renderDexTests() {
  const dex = REPORT.dex_tests || { http: [], traceroute: [], total_runs: 0 };
  document.getElementById("dexTestHint").textContent = `${dex.total_runs} test run(s)`;
  const box = document.getElementById("dexTestsBox");
  if (!dex.http.length && !dex.traceroute.length) {
    box.innerHTML = '<div class="empty">No `dex_application_tests` results recorded for this device in the selected window (or dataset not enabled).</div>';
    return;
  }
  let out = "";
  if (dex.http.length) {
    out += `<div class="chart-legend" style="margin-bottom:8px">
      <span class="leg-item"><span class="leg-swatch wf-dns"></span>DNS</span>
      <span class="leg-item"><span class="leg-swatch wf-tcp"></span>TCP</span>
      <span class="leg-item"><span class="leg-swatch wf-tls"></span>TLS</span>
      <span class="leg-item"><span class="leg-swatch wf-ttfb"></span>TTFB</span>
      <span class="leg-item"><span class="leg-swatch wf-dl"></span>Download</span>
    </div>`;
    out += `<table><thead><tr><th>HTTP Test</th><th>Runs</th><th>Success</th><th>Avg / P95</th><th>Timing Waterfall (ms)</th></tr></thead><tbody>`;
    for (const t of dex.http) {
      const sum = Math.max(1, t.avg_dns_ms + t.avg_tcp_ms + t.avg_tls_ms + t.avg_ttfb_ms + t.avg_dl_ms);
      const pct = (v) => (v / sum * 100).toFixed(1);
      const cls = t.success_pct === 100 ? "ok" : t.success_pct >= 90 ? "warn" : "crit";
      out += `<tr>
        <td><strong>${esc(t.name)}</strong><div class="meta-sub mono">${esc(t.url)}</div></td>
        <td>${t.runs}</td>
        <td><span class="badge ${cls}">${t.success_pct}% (${t.latest_status || "ERR"})</span></td>
        <td class="mono">${t.avg_ms} / ${t.p95_ms} ms</td>
        <td>
          <div class="waterfall" title="DNS ${t.avg_dns_ms} | TCP ${t.avg_tcp_ms} | TLS ${t.avg_tls_ms} | TTFB ${t.avg_ttfb_ms} | DL ${t.avg_dl_ms} ms">
            <div class="wf-dns" style="width:${pct(t.avg_dns_ms)}%"></div>
            <div class="wf-tcp" style="width:${pct(t.avg_tcp_ms)}%"></div>
            <div class="wf-tls" style="width:${pct(t.avg_tls_ms)}%"></div>
            <div class="wf-ttfb" style="width:${pct(t.avg_ttfb_ms)}%"></div>
            <div class="wf-dl" style="width:${pct(t.avg_dl_ms)}%"></div>
          </div>
          <div class="meta-sub mono">DNS ${t.avg_dns_ms} · TCP ${t.avg_tcp_ms} · TLS ${t.avg_tls_ms} · TTFB ${t.avg_ttfb_ms}</div>
        </td>
      </tr>`;
    }
    out += `</tbody></table>`;
  }
  if (dex.traceroute.length) {
    out += `<div style="margin-top:14px">`;
    for (const tr of dex.traceroute) {
      out += `<details><summary>Traceroute: ${esc(tr.name)} (${esc(tr.dest)} · ${esc(tr.protocol.toUpperCase())}) — avg ${tr.avg_duration_ms} ms · ${tr.latest_hop_count} hops (${tr.latest_loss_hops} with loss)</summary>`;
      if (tr.latest_hops && tr.latest_hops.length) {
        out += `<table style="margin-top:8px"><thead><tr><th>TTL</th><th>Host / IP</th><th>Network (ASN / ASO)</th><th>Geo</th><th>Loss</th><th>Avg RTT</th></tr></thead><tbody>`;
        for (const h of tr.latest_hops) {
          const lossBadge = h.loss_pct > 0 ? `<span class="badge crit">${h.loss_pct}%</span>` : `<span class="badge ok">0%</span>`;
          out += `<tr><td class="mono">${h.ttl}</td><td class="mono">${esc(h.name || h.ip || "*")} ${h.name && h.ip ? `<span style="color:var(--muted)">(${esc(h.ip)})</span>` : ""}</td><td>${h.asn ? `AS${h.asn} ` : ""}${esc(h.aso || "—")}</td><td>${esc(h.geo || "—")}</td><td>${lossBadge}</td><td class="mono">${fmt(h.avg_rtt_ms, " ms", 1)}</td></tr>`;
        }
        out += `</tbody></table>`;
      }
      out += `</details>`;
    }
    out += `</div>`;
  }
  box.innerHTML = out;
}

function renderPostureAndEvents() {
  const posture = REPORT.posture || [];
  document.getElementById("postureHint").textContent = `${posture.length} check(s)`;
  const pBox = document.getElementById("postureBox");
  if (!posture.length) {
    pBox.innerHTML = '<div class="empty">No `device_posture_results` recorded in this window.</div>';
  } else {
    pBox.innerHTML = `<table><thead><tr><th>Posture Check</th><th>Type</th><th>Result</th><th>Evaluated</th></tr></thead><tbody>` +
      posture.map((p) => `<tr><td>${esc(p.name)}</td><td class="mono">${esc(p.type)}</td><td><span class="badge ${p.passed ? "ok" : "crit"}">${p.passed ? "PASS" : "FAIL"}</span></td><td class="mono">${fmtTime(p.timestamp)}</td></tr>`).join("") +
      `</tbody></table>`;
  }

  const timeline = [];
  for (const t of REPORT.transitions || []) timeline.push({ ts: t.timestamp, kind: t.dimension, detail: `${t.from} → ${t.to}` });
  for (const tg of REPORT.warp_toggles || []) timeline.push({ ts: tg.timestamp, kind: "WARP Toggle", detail: `${tg.toggled ? "Enabled (ON)" : "Disabled (OFF)"} (${tg.useremail || ""})` });
  for (const cfg of REPORT.warp_configs || []) timeline.push({ ts: cfg.timestamp, kind: "WARP Profile", detail: `${cfg.confignamefrom || "—"} → ${cfg.confignameto || "—"}` });
  timeline.sort((a, b) => (b.ts || "").localeCompare(a.ts || ""));
  document.getElementById("eventsHint").textContent = `${timeline.length} event(s)`;
  const eBox = document.getElementById("eventsBox");
  if (!timeline.length) {
    eBox.innerHTML = '<div class="empty">No state transitions, WARP toggles, or profile changes in this window (stable session).</div>';
  } else {
    eBox.innerHTML = `<table><thead><tr><th>Time (UTC)</th><th>Event</th><th>Change</th></tr></thead><tbody>` +
      timeline.slice(0, 25).map((ev) => `<tr><td class="mono">${fmtTime(ev.ts)}</td><td><strong>${esc(ev.kind)}</strong></td><td>${esc(ev.detail)}</td></tr>`).join("") +
      `</tbody></table>`;
  }
}

function renderCharts() {
  chartRegistry.length = 0;
  currentEvents = filterEvents(activeHours);
  renderKpis(currentEvents);
  renderRibbon(currentEvents);

  const hasWifi = currentEvents.some((e) => e.wifi_dbm !== null && e.wifi_dbm !== undefined);
  document.getElementById("tunnelHint").textContent = `${currentEvents.length} samples · red dots indicate packet loss > 0%`;
  drawLineChart("chartTunnel", currentEvents, [
    { color: "#f38020", fn: (e) => e.handshake_ms, width: 2 },
    { color: "#38bdf8", fn: (e) => e.down?.rtt_ms, width: 2 },
    { color: "#a78bfa", fn: (e) => e.up?.rtt_ms, width: 2 },
    { color: "#2dd4bf", fn: (e) => (e.down?.jitter_ms ?? e.up?.jitter_ms) !== null ? Math.max(e.down?.jitter_ms || 0, e.up?.jitter_ms || 0) : null, width: 1.7, dash: "4,3" },
  ], {
    height: 230, yUnit: "ms",
    markerFn: (e) => ((e.down?.loss_pct || 0) > 0 || (e.up?.loss_pct || 0) > 0),
  });

  if (hasWifi) {
    document.getElementById("wifiHint").textContent = "Green > -60 dBm · Yellow -60..-70 · Red < -70";
    drawLineChart("chartWifi", currentEvents, [
      { color: "#22c55e", fn: (e) => e.wifi_dbm, width: 2.2 },
    ], {
      height: 210, yMin: -90, yMax: -30, allowNegative: true, yUnit: "dBm",
      bands: [
        { lo: -60, hi: -30, fill: "rgba(34,197,94,0.08)" },
        { lo: -70, hi: -60, fill: "rgba(234,179,8,0.08)" },
        { lo: -90, hi: -70, fill: "rgba(239,68,68,0.08)" },
      ],
    });
  } else {
    document.getElementById("wifiHint").textContent = "Ethernet / No Wi-Fi RSSI — showing Rx/Tx KB/s";
    drawLineChart("chartWifi", currentEvents, [
      { color: "#38bdf8", fn: (e) => e.net_rx_bps !== null ? e.net_rx_bps / 1024 : null, width: 2 },
      { color: "#f38020", fn: (e) => e.net_tx_bps !== null ? e.net_tx_bps / 1024 : null, width: 2 },
    ], { height: 210, yUnit: " KB/s" });
  }

  const sysSeries = [
    { color: "#38bdf8", fn: (e) => e.cpu_pct, width: 2 },
    { color: "#f38020", fn: (e) => e.ram_pct, width: 2.2 },
    { color: "#a78bfa", fn: (e) => e.disk_pct, width: 1.8, dash: "5,3" },
  ];
  if (REPORT.kpis.has_battery) {
    document.getElementById("legBatt").style.display = "inline-flex";
    document.getElementById("sysHint").textContent = "Battery detected (0–100%)";
    sysSeries.push({ color: "#22c55e", fn: (e) => e.battery_pct, width: 2 });
  } else {
    document.getElementById("sysHint").textContent = "Desktop / AC-only (battery hidden)";
  }
  drawLineChart("chartSys", currentEvents, sysSeries, { height: 210, yMin: 0, yMax: 100, yUnit: "%" });
}

document.getElementById("themeBtn").addEventListener("click", () => {
  const root = document.documentElement;
  root.setAttribute("data-theme", root.getAttribute("data-theme") === "dark" ? "light" : "dark");
});
document.querySelectorAll("#rangeButtons .btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#rangeButtons .btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    activeHours = Number(btn.dataset.hours || 0);
    renderCharts();
  });
});
window.addEventListener("resize", () => renderCharts());

renderHeader();
renderFindings();
renderTopApps();
renderDexTests();
renderPostureAndEvents();
renderCharts();
</script>
</body>
</html>
"""


def render_html_dashboard(report: Dict[str, Any]) -> str:
    dev = report.get("device") or {}
    title = f"CF1 Device Metrics — {dev.get('name') or report.get('meta', {}).get('device_id', 'Device')}"
    # Safely embed JSON inside <script> tag
    safe_json = json.dumps(report, separators=(",", ":")).replace("</", "<\\/")
    return (
        HTML_TEMPLATE
        .replace("__PAGE_TITLE__", html_lib.escape(title))
        .replace("__REPORT_JSON__", safe_json)
    )


# ---------------------------------------------------------------------------
# Device Resolution & Data Collection
# ---------------------------------------------------------------------------

def resolve_device(
    client: CloudflareClient,
    device_id: str,
    email: str,
    hostname: str,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    if device_id:
        return device_id, client.get_device(device_id)

    devices = client.list_devices(email=email)
    if hostname:
        needle = hostname.lower()
        devices = [d for d in devices if needle in str(d.get("name") or "").lower()]

    if not devices:
        target = email or hostname or "account"
        raise RuntimeError(f"No WARP devices found matching '{target}'. Run with --list-devices to inspect available devices.")

    devices.sort(key=lambda d: str(d.get("last_seen_at") or ""), reverse=True)
    chosen = devices[0]
    if len(devices) > 1:
        print(
            f"[info] Found {len(devices)} matching devices; selecting most recently active: "
            f"{chosen.get('name')} ({chosen.get('id')}, last_seen={chosen.get('last_seen_at')})",
            file=sys.stderr,
        )
    return str(chosen["id"]), chosen


def collect_telemetry(
    client: CloudflareClient,
    device_id: str,
    device_meta: Optional[Dict[str, Any]],
    hours: int,
    limit: int,
    enrich: bool,
) -> Dict[str, Any]:
    safe_id = sql_escape(device_id)
    email = ""
    if isinstance(device_meta, dict) and isinstance(device_meta.get("last_seen_user"), dict):
        email = str(device_meta["last_seen_user"].get("email") or "")
    safe_email = sql_escape(email)

    state_sql = (
        f"SELECT * FROM dex_device_state_events "
        f"WHERE timestamp >= now() - INTERVAL '{hours}' HOUR "
        f"AND deviceid = '{safe_id}' "
        f"ORDER BY timestamp DESC LIMIT {limit}"
    )
    state_events = client.query_sql(state_sql, label="dex_device_state_events")

    dex_tests: List[Dict[str, Any]] = []
    dex_test_catalog: Dict[str, Dict[str, Any]] = {}
    warp_toggles: List[Dict[str, Any]] = []
    warp_configs: List[Dict[str, Any]] = []
    posture_results: List[Dict[str, Any]] = []

    if enrich:
        dex_test_catalog = client.list_dex_tests()
        dex_tests_sql = (
            f"SELECT timestamp, testid, testtype, colocode, executioncontext, "
            f"httpurl, httpmethod, httpstatuscode, httperrormessage, "
            f"httpdomainlookupstartms, httpdomainlookupendms, httpconnectstartms, "
            f"httpconnectendms, httpsecureconnectionstartms, httprequeststartms, "
            f"httpresponsestartms, httpresponseendms, httpserveripaddress, httpserveripaso, "
            f"traceroutedestinationhostname, traceroutedestinationipaddress, "
            f"traceroutedestinationipaso, traceroutedurationms, tracerouteprotocol, traceroutehops "
            f"FROM dex_application_tests "
            f"WHERE timestamp >= now() - INTERVAL '{hours}' HOUR "
            f"AND deviceid = '{safe_id}' "
            f"ORDER BY timestamp DESC LIMIT 500"
        )
        dex_tests = client.query_sql(dex_tests_sql, label="dex_application_tests")

        toggle_filter = f"deviceid = '{safe_id}'" + (f" OR useremail = '{safe_email}'" if safe_email else "")
        warp_toggles = client.query_sql(
            f"SELECT timestamp, hostname, toggled, useremail, accountname "
            f"FROM warp_toggle_changes "
            f"WHERE timestamp >= now() - INTERVAL '{hours}' HOUR AND ({toggle_filter}) "
            f"ORDER BY timestamp DESC LIMIT 100",
            label="warp_toggle_changes",
        )
        warp_configs = client.query_sql(
            f"SELECT timestamp, hostname, confignamefrom, confignameto, accountnamefrom, accountnameto "
            f"FROM warp_config_changes "
            f"WHERE timestamp >= now() - INTERVAL '{hours}' HOUR AND ({toggle_filter}) "
            f"ORDER BY timestamp DESC LIMIT 100",
            label="warp_config_changes",
        )
        posture_results = client.query_sql(
            f"SELECT timestamp, posturecheckname, posturechecktype, postureevaluatedresult, clientversion, osversion "
            f"FROM device_posture_results "
            f"WHERE timestamp >= now() - INTERVAL '{hours}' HOUR AND deviceid = '{safe_id}' "
            f"ORDER BY timestamp DESC LIMIT 100",
            label="device_posture_results",
        )

    return {
        "meta": {
            "account_id": client.account_id,
            "device_id": device_id,
            "hours": hours,
            "mode": client.mode,
            "enriched": enrich,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "device": device_meta or {"id": device_id},
        "state_events": state_events,
        "dex_tests": dex_tests,
        "dex_test_catalog": dex_test_catalog,
        "warp_toggles": warp_toggles,
        "warp_configs": warp_configs,
        "posture_results": posture_results,
        "warnings": list(client.warnings),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect and visualize Cloudflare One (WARP / DEX) client device telemetry."
    )
    target = parser.add_argument_group("Target Selection")
    target.add_argument("--device-id", default="", help="Physical device UUID")
    target.add_argument("--email", default="", help="User email (resolves most recently active WARP device)")
    target.add_argument("--hostname", default="", help="Device hostname substring filter")
    target.add_argument("--list-devices", action="store_true", help="List WARP devices in the account and exit")
    target.add_argument("--from-json", default="", help="Render report/HTML offline from a previously saved JSON bundle")

    conn = parser.add_argument_group("Cloudflare Connection")
    conn.add_argument("--account-id", default="", help="Cloudflare Account ID (or set CLOUDFLARE_ACCOUNT_ID)")
    conn.add_argument("--mode", choices=["auto", "cli", "api"], default=os.environ.get("CF_MODE", "auto"), help="Execution mode (default: auto)")
    conn.add_argument("--profile", default="", help="Optional `cf` CLI auth profile name")
    conn.add_argument("--hours", type=int, default=24, help="Lookback window in hours (default: 24)")
    conn.add_argument("--limit", type=int, default=1500, help="Max dex_device_state_events rows (default: 1500)")
    conn.add_argument("--no-enrich", action="store_true", help="Skip DEX tests, WARP changes, and posture enrichment")

    out = parser.add_argument_group("Output Options")
    out.add_argument("--format", choices=["text", "markdown"], default="text", help="Stdout summary format (default: text)")
    out.add_argument("--html", default="", help="Write self-contained interactive HTML dashboard to path")
    out.add_argument("--open", action="store_true", help="Open generated HTML dashboard in default browser")
    out.add_argument("--json", dest="json_out", default="", help="Save raw collected telemetry bundle to JSON path")
    out.add_argument("--verbose", action="store_true", help="Log executed CLI/API calls to stderr")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.hours < 1 or args.limit < 1:
        print("Error: --hours and --limit must be positive integers.", file=sys.stderr)
        return 2

    if args.from_json:
        with open(args.from_json, "r", encoding="utf-8") as fh:
            bundle = json.load(fh)
    else:
        client = CloudflareClient(
            mode=args.mode,
            account_id=args.account_id,
            profile=args.profile,
            verbose=args.verbose,
        )
        client.discover_account_id()

        if args.list_devices:
            devices = client.list_devices(email=args.email)
            devices.sort(key=lambda d: str(d.get("last_seen_at") or ""), reverse=True)
            print(f"{'Device ID':<38} {'Name':<22} {'Platform':<10} {'WARP':<13} {'Last Seen':<22} User")
            print("-" * 130)
            for d in devices:
                u = d.get("last_seen_user") if isinstance(d.get("last_seen_user"), dict) else {}
                print(
                    f"{str(d.get('id', '')):<38} {str(d.get('name', ''))[:21]:<22} "
                    f"{str(d.get('device_type', '')):<10} {str(d.get('client_version', '')):<13} "
                    f"{str(d.get('last_seen_at', ''))[:20]:<22} {u.get('email', '—')}"
                )
            return 0

        if not (args.device_id or args.email or args.hostname):
            print("Error: Specify --device-id, --email, --hostname, --list-devices, or --from-json.", file=sys.stderr)
            return 2

        dev_id, dev_meta = resolve_device(client, args.device_id, args.email, args.hostname)
        bundle = collect_telemetry(
            client=client,
            device_id=dev_id,
            device_meta=dev_meta,
            hours=args.hours,
            limit=args.limit,
            enrich=not args.no_enrich,
        )
        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as fh:
                json.dump(bundle, fh, indent=2)
            print(f"[info] Saved telemetry JSON to {args.json_out}", file=sys.stderr)

    report = build_analytics(bundle)
    print(render_terminal_report(report, fmt=args.format))

    if args.html:
        html_doc = render_html_dashboard(report)
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(html_doc)
        abs_path = os.path.abspath(args.html)
        print(f"\n[info] Interactive HTML dashboard written to: {abs_path}", file=sys.stderr)
        if args.open:
            webbrowser.open(f"file://{abs_path}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
