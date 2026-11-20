#!/usr/bin/env bash
# Automated Cloudflare Zero Trust User Troubleshooting Script
# Supports BOTH Cloudflare `cf` CLI and direct Cloudflare Client v4 REST API (`curl`).
#
# Usage:
#   ./investigate-user.sh <user_email> [account_id] [hours]
#
# Environment Variables:
#   CF_MODE              "auto" (default: uses `cf` if installed, else REST API), "cli", or "api"
#   CLOUDFLARE_ACCOUNT_ID Optional default account ID if not passed as $2
#   CLOUDFLARE_API_TOKEN  Bearer token for REST API mode (or `cf` token auth)
#   CLOUDFLARE_EMAIL      Legacy Global API Key email (fallback for REST API mode)
#   CLOUDFLARE_API_KEY    Legacy Global API Key (fallback for REST API mode)

set -euo pipefail

USER_EMAIL="${1:-}"
ACCOUNT_ID="${2:-${CLOUDFLARE_ACCOUNT_ID:-}}"
HOURS="${3:-24}"
CF_MODE="${CF_MODE:-auto}"
CF_API_BASE="${CF_API_BASE:-https://api.cloudflare.com/client/v4}"

if [[ -z "$USER_EMAIL" ]]; then
  echo "Usage: $0 <user_email> [account_id] [hours_lookback]"
  echo "Example (cf CLI):  $0 user@example.com 0123456789abcdef0123456789abcdef 24"
  echo "Example (REST API): CF_MODE=api CLOUDFLARE_API_TOKEN=<token> $0 user@example.com 0123456789abcdef0123456789abcdef 24"
  exit 1
fi

# Determine execution mode (cli vs api)
if [[ "$CF_MODE" == "auto" ]]; then
  if command -v cf >/dev/null 2>&1; then
    MODE="cli"
  else
    MODE="api"
  fi
elif [[ "$CF_MODE" == "cli" || "$CF_MODE" == "api" ]]; then
  MODE="$CF_MODE"
else
  echo "Error: Invalid CF_MODE '$CF_MODE' (expected: auto, cli, or api)" >&2
  exit 1
fi

# Validate credentials in REST API mode
AUTH_HEADERS=()
if [[ "$MODE" == "api" ]]; then
  if [[ -n "${CLOUDFLARE_API_TOKEN:-}" ]]; then
    AUTH_HEADERS=(-H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}")
  elif [[ -n "${CLOUDFLARE_EMAIL:-}" && -n "${CLOUDFLARE_API_KEY:-}" ]]; then
    AUTH_HEADERS=(-H "X-Auth-Email: ${CLOUDFLARE_EMAIL}" -H "X-Auth-Key: ${CLOUDFLARE_API_KEY}")
  else
    echo "Error: 'cf' CLI is not active (mode=$MODE) and no REST API credentials were found." >&2
    echo "Set CLOUDFLARE_API_TOKEN (with Account Logs:Read, Zero Trust:Read, Account Settings:Read)" >&2
    echo "or set CLOUDFLARE_EMAIL + CLOUDFLARE_API_KEY." >&2
    exit 1
  fi
fi

# Unwrap Cloudflare v4 REST API JSON envelope ({"result": ..., "success": ...})
unwrap_v4_response() {
  python3 -c '
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    print("[]")
    sys.exit(0)
try:
    payload = json.loads(raw)
except Exception:
    print(raw)
    sys.exit(0)
if isinstance(payload, dict) and "success" in payload:
    if not payload.get("success"):
        errs = payload.get("errors", [])
        sys.stderr.write(f"  [API Error] {json.dumps(errs)}\n")
        print("[]")
        sys.exit(0)
    print(json.dumps(payload.get("result", []), indent=2))
else:
    print(json.dumps(payload, indent=2))
'
}

list_accounts() {
  if [[ "$MODE" == "cli" ]]; then
    cf accounts list -q 2>/dev/null || cf accounts list
  else
    curl -sS "${CF_API_BASE}/accounts" "${AUTH_HEADERS[@]}" | unwrap_v4_response
  fi
}

list_user_devices() {
  local email="$1"
  if [[ "$MODE" == "cli" ]]; then
    cf zero-trust devices list --last-seen-user-email "$email" -q 2>/dev/null || cf zero-trust devices list --last-seen-user-email "$email"
  else
    curl -sS -G "${CF_API_BASE}/accounts/${ACCOUNT_ID}/devices/physical-devices" \
      "${AUTH_HEADERS[@]}" \
      --data-urlencode "last_seen_user.email=${email}" | unwrap_v4_response
  fi
}

run_sql_query() {
  local sql="$1"
  if [[ "$MODE" == "cli" ]]; then
    cf logs query -q --body "$sql" 2>/dev/null || true
  else
    curl -sS --max-time 35 -X POST "${CF_API_BASE}/accounts/${ACCOUNT_ID}/logs/explorer/query/sql" \
      "${AUTH_HEADERS[@]}" \
      -H "Content-Type: text/plain" \
      --data-raw "$sql" | unwrap_v4_response || true
  fi
}

if [[ -z "$ACCOUNT_ID" ]]; then
  echo "==> Discovering default Cloudflare account (mode: $MODE)..."
  ACCOUNTS_JSON=$(list_accounts)
  ACCOUNT_ID=$(echo "$ACCOUNTS_JSON" | python3 -c "
import sys, json
raw = sys.stdin.read()
idx = raw.find('[')
data = json.loads(raw[idx:]) if idx != -1 else []
if not data:
    sys.exit(1)
print(data[0]['id'])
")
  echo "==> Selected Account ID: $ACCOUNT_ID"
fi

export CLOUDFLARE_ACCOUNT_ID="$ACCOUNT_ID"

SQL_EMAIL="${USER_EMAIL//\'/\'\'}"

echo "================================================================================"
echo " Cloudflare Zero Trust Triage for: $USER_EMAIL"
echo " Account ID: $ACCOUNT_ID | Lookback: ${HOURS}h | Mode: $MODE"
echo "================================================================================"

echo ""
echo "[1/8] Resolving WARP Devices for $USER_EMAIL..."
DEVICES_RAW=$(list_user_devices "$USER_EMAIL")
echo "$DEVICES_RAW"

PRIMARY_DEVICE_ID=$(echo "$DEVICES_RAW" | python3 -c "
import sys, json
raw = sys.stdin.read()
idx = raw.find('[')
data = json.loads(raw[idx:]) if idx != -1 else []
if data:
    # Sort by last_seen_at descending to pick the most recently active device
    data.sort(key=lambda d: d.get('last_seen_at', ''), reverse=True)
    print(data[0].get('id', ''))
" 2>/dev/null || true)

if [[ -n "$PRIMARY_DEVICE_ID" ]]; then
  SQL_DEVICE_ID="${PRIMARY_DEVICE_ID//\'/\'\'}"
  echo "==> Most recently active Device ID: $PRIMARY_DEVICE_ID"
  DEVICE_FILTER="(email = '$SQL_EMAIL' OR deviceid = '$SQL_DEVICE_ID')"
  TOGGLE_FILTER="(useremail = '$SQL_EMAIL' OR deviceid = '$SQL_DEVICE_ID')"
else
  echo "==> No active WARP device ID found; filtering by email only."
  DEVICE_FILTER="(email = '$SQL_EMAIL')"
  TOGGLE_FILTER="(useremail = '$SQL_EMAIL')"
fi

echo ""
echo "[2/8] Gateway DNS Decision Summary (Last ${HOURS}h)..."
run_sql_query "SELECT resolverdecision, policyname, count() AS total FROM gateway_dns WHERE datetime >= now() - INTERVAL '$HOURS' HOUR AND $DEVICE_FILTER GROUP BY resolverdecision, policyname ORDER BY total DESC"

echo ""
echo "[3/8] Recent Blocked Gateway DNS Queries (Last ${HOURS}h, top 15)..."
run_sql_query "SELECT datetime, queryname, querytypename, resolverdecision, policyname, devicename FROM gateway_dns WHERE datetime >= now() - INTERVAL '$HOURS' HOUR AND $DEVICE_FILTER AND resolverdecision LIKE '%block%' ORDER BY datetime DESC LIMIT 15"

echo ""
echo "[4/8] Gateway L4 Network Non-Allow / Blocked Connections (Last ${HOURS}h, top 20)..."
run_sql_query "SELECT action, policyname, sni, destinationip, destinationport, detectedprotocol, transportprotocol, count() AS total FROM gateway_network WHERE datetime >= now() - INTERVAL '$HOURS' HOUR AND $DEVICE_FILTER AND action != 'allow' GROUP BY action, policyname, sni, destinationip, destinationport, detectedprotocol, transportprotocol ORDER BY total DESC LIMIT 20"

echo ""
echo "[5/8] Network Sessions: TLS, Certificate & Bypassed Tunnel Handshake Errors (Last ${HOURS}h)..."
echo "  -> Inspected TLS & Connection Errors:"
run_sql_query "SELECT sni, resolvedfqdn, originip, originport, destinationtunnelid, connectionclosereason, origintlscertificatevalidationresult, origintlscertificateissuer, count() AS sessions FROM zero_trust_network_sessions WHERE sessionendtime >= now() - INTERVAL '$HOURS' HOUR AND $DEVICE_FILTER AND (connectionclosereason IN ('CLIENT_TLS_ERROR', 'ORIGIN_TLS_ERROR', 'CLIENT_ERROR', 'ORIGIN_ERROR', 'ORIGIN_UNREACHABLE', 'PROXY_CONN_REFUSED') OR origintlscertificatevalidationresult NOT IN ('VALID', 'NONE', '')) GROUP BY sni, resolvedfqdn, originip, originport, destinationtunnelid, connectionclosereason, origintlscertificatevalidationresult, origintlscertificateissuer ORDER BY sessions DESC LIMIT 20"
echo "  -> Bypassed TLS Aborts over Cloudflare Tunnels / Private Apps (Suspected Expired/Self-Signed Origin Cert):"
run_sql_query "SELECT sni, resolvedfqdn, originip, originport, destinationtunnelid, virtualnetworkid, connectionclosereason, origintlscertificatevalidationresult, count() AS aborted_handshakes, min(bytessent) AS min_sent, max(bytesreceived) AS max_rcvd FROM zero_trust_network_sessions WHERE sessionendtime >= now() - INTERVAL '$HOURS' HOUR AND $DEVICE_FILTER AND destinationtunnelid != '' AND origintlscertificatevalidationresult = 'NONE' AND (sni != '' OR detectedprotocol = 'tls' OR originport IN (443, 8443)) AND connectionclosereason IN ('CLIENT_CLOSED', 'CLIENT_ERROR') AND bytesreceived > 0 AND bytesreceived < 15000 AND bytessent < 5000 GROUP BY sni, resolvedfqdn, originip, originport, destinationtunnelid, virtualnetworkid, connectionclosereason, origintlscertificatevalidationresult ORDER BY aborted_handshakes DESC LIMIT 20"

echo ""
echo "[6/8] Device Posture Evaluations (Last ${HOURS}h, latest 10)..."
run_sql_query "SELECT timestamp, posturecheckname, posturechecktype, postureevaluatedresult, clientversion, osversion FROM device_posture_results WHERE timestamp >= now() - INTERVAL '$HOURS' HOUR AND $DEVICE_FILTER ORDER BY timestamp DESC LIMIT 10"

echo ""
echo "[7/8] WARP Client Toggles & Config Changes (Last ${HOURS}h)..."
run_sql_query "SELECT timestamp, hostname, toggled, useremail FROM warp_toggle_changes WHERE timestamp >= now() - INTERVAL '$HOURS' HOUR AND $TOGGLE_FILTER ORDER BY timestamp DESC LIMIT 10"
run_sql_query "SELECT timestamp, hostname, confignamefrom, confignameto FROM warp_config_changes WHERE timestamp >= now() - INTERVAL '$HOURS' HOUR AND $TOGGLE_FILTER ORDER BY timestamp DESC LIMIT 10"

echo ""
echo "[8/8] Gateway HTTP (incl. Untrusted Cert Actions) & Access Requests (Last ${HOURS}h, latest 10)..."
run_sql_query "SELECT datetime, action, httphost, httpstatuscode, policyname, untrustedcertificateaction, url FROM gateway_http WHERE datetime >= now() - INTERVAL '$HOURS' HOUR AND $DEVICE_FILTER ORDER BY datetime DESC LIMIT 10"
run_sql_query "SELECT createdat, action, allowed, appdomain, ipaddress FROM access_requests WHERE createdat >= now() - INTERVAL '$HOURS' HOUR AND email = '$SQL_EMAIL' ORDER BY createdat DESC LIMIT 10"

echo ""
echo "================================================================================"
echo " Triage complete."
echo "================================================================================"
