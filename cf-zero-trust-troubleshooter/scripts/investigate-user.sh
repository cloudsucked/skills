#!/usr/bin/env bash
# Automated Cloudflare Zero Trust User Troubleshooting Script
# Usage: ./investigate-user.sh <user_email> [account_id] [hours]

set -euo pipefail

USER_EMAIL="${1:-}"
ACCOUNT_ID="${2:-${CLOUDFLARE_ACCOUNT_ID:-}}"
HOURS="${3:-24}"

if [[ -z "$USER_EMAIL" ]]; then
  echo "Usage: $0 <user_email> [account_id] [hours_lookback]"
  echo "Example: $0 user@example.com 0123456789abcdef0123456789abcdef 24"
  exit 1
fi

if [[ -z "$ACCOUNT_ID" ]]; then
  echo "==> Discovering default Cloudflare Enterprise account..."
  ACCOUNTS_JSON=$(cf accounts list -q 2>/dev/null || cf accounts list)
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

echo "================================================================================"
echo " Cloudflare Zero Trust Triage for: $USER_EMAIL"
echo " Account ID: $ACCOUNT_ID | Lookback: ${HOURS}h"
echo "================================================================================"

echo ""
echo "[1/6] Resolving WARP Devices for $USER_EMAIL..."
DEVICES_RAW=$(cf zero-trust devices list --last-seen-user-email "$USER_EMAIL" -q 2>/dev/null || cf zero-trust devices list --last-seen-user-email "$USER_EMAIL")
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
  echo "==> Most recently active Device ID: $PRIMARY_DEVICE_ID"
  DEVICE_FILTER="(email = '$USER_EMAIL' OR deviceid = '$PRIMARY_DEVICE_ID')"
  TOGGLE_FILTER="(useremail = '$USER_EMAIL' OR deviceid = '$PRIMARY_DEVICE_ID')"
else
  echo "==> No active WARP device ID found; filtering by email only."
  DEVICE_FILTER="(email = '$USER_EMAIL')"
  TOGGLE_FILTER="(useremail = '$USER_EMAIL')"
fi

echo ""
echo "[2/6] Gateway DNS Decision Summary (Last ${HOURS}h)..."
cf logs query -q --body "SELECT resolverdecision, policyname, count() AS total FROM gateway_dns WHERE datetime >= now() - INTERVAL '$HOURS' HOUR AND $DEVICE_FILTER GROUP BY resolverdecision, policyname ORDER BY total DESC" 2>/dev/null || true

echo ""
echo "[3/6] Recent Blocked Gateway DNS Queries (Last ${HOURS}h, top 15)..."
cf logs query -q --body "SELECT datetime, queryname, querytypename, resolverdecision, policyname, devicename FROM gateway_dns WHERE datetime >= now() - INTERVAL '$HOURS' HOUR AND $DEVICE_FILTER AND resolverdecision LIKE '%block%' ORDER BY datetime DESC LIMIT 15" 2>/dev/null || true

echo ""
echo "[4/6] Device Posture Evaluations (Last ${HOURS}h, latest 10)..."
cf logs query -q --body "SELECT timestamp, posturecheckname, posturechecktype, postureevaluatedresult, clientversion, osversion FROM device_posture_results WHERE timestamp >= now() - INTERVAL '$HOURS' HOUR AND $DEVICE_FILTER ORDER BY timestamp DESC LIMIT 10" 2>/dev/null || true

echo ""
echo "[5/6] WARP Client Toggles & Config Changes (Last ${HOURS}h)..."
cf logs query -q --body "SELECT timestamp, hostname, toggled, useremail FROM warp_toggle_changes WHERE timestamp >= now() - INTERVAL '$HOURS' HOUR AND $TOGGLE_FILTER ORDER BY timestamp DESC LIMIT 10" 2>/dev/null || true
cf logs query -q --body "SELECT timestamp, hostname, confignamefrom, confignameto FROM warp_config_changes WHERE timestamp >= now() - INTERVAL '$HOURS' HOUR AND $TOGGLE_FILTER ORDER BY timestamp DESC LIMIT 10" 2>/dev/null || true

echo ""
echo "[6/6] Gateway HTTP & Access Requests (Last ${HOURS}h, latest 10)..."
cf logs query -q --body "SELECT datetime, action, httphost, httpstatuscode, policyname, url FROM gateway_http WHERE datetime >= now() - INTERVAL '$HOURS' HOUR AND $DEVICE_FILTER ORDER BY datetime DESC LIMIT 10" 2>/dev/null || true
cf logs query -q --body "SELECT createdat, action, allowed, appdomain, ipaddress FROM access_requests WHERE createdat >= now() - INTERVAL '$HOURS' HOUR AND email = '$USER_EMAIL' ORDER BY createdat DESC LIMIT 10" 2>/dev/null || true

echo ""
echo "================================================================================"
echo " Triage complete."
echo "================================================================================"
