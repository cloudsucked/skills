---
name: cf1-device-metrics
description: >-
  Collect, analyze, and visualize Cloudflare One (WARP / DEX) client device
  telemetry from `dex_device_state_events` enriched with Physical Device
  metadata, `dex_application_tests` (HTTP resource timing & traceroute hops),
  `warp_toggle_changes`, `warp_config_changes`, and `device_posture_results`
  using either the Cloudflare `cf` CLI or direct Cloudflare Client v4 REST API
  (`curl`/Python). Activate this skill when inspecting a user's WARP client
  health, MASQUE tunnel RTT/jitter/packet loss, Wi-Fi signal strength, CPU/RAM/
  disk/battery utilization, top resource-consuming apps, or generating a
  single-device metrics report or interactive HTML dashboard.
---

# Cloudflare One Client Device Metrics & Visualization Skill

Use this runbook to pull granular device telemetry from Cloudflare Digital Experience Monitoring (DEX) and Zero Trust—using **either** the Cloudflare `cf` CLI **or** the **Cloudflare Client v4 REST API**—and render both a **terminal/Markdown sparkline report** and an **offline-ready interactive HTML dashboard**.

---

## Execution Mode Selection (`cf` CLI vs. Direct Client API)

Check whether the Cloudflare `cf` CLI is installed:
```bash
command -v cf
```
- **Mode A — `cf` CLI Available:** Set `CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>"` (and optional `--profile <PROFILE>`) so commands run non-interactively.
- **Mode B — No `cf` CLI (Direct Client v4 REST API):** Query `https://api.cloudflare.com/client/v4` with:
  - **API Token (Recommended):** `CLOUDFLARE_API_TOKEN` (`-H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"`)
    - *Required Token Permissions:* `Account > Logs > Read` (or `Log Explorer > Read`), `Account > Zero Trust > Read`, `Account > Digital Experience Monitoring > Read`, `Account > Account Settings > Read`.
  - **Legacy Global API Key:** `CLOUDFLARE_EMAIL` + `CLOUDFLARE_API_KEY`.

---

## Critical Rules & Dataset Guardrails

1. **Always Scope to an Account ID**:
   - **`cf` CLI:** Without `CLOUDFLARE_ACCOUNT_ID`, `cf` launches an interactive account picker and hangs in non-interactive shells. Always export `CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>"` (or pass `--account-id` to the helper script).
   - **Client API:** Include `{account_id}` in `/client/v4/accounts/{account_id}/...`.

2. **Raw SQL Body for Log Explorer Queries**:
   - `cf logs query --body "<RAW_SQL>"` and `POST /accounts/{id}/logs/explorer/query/sql` (`Content-Type: text/plain`) expect **raw SQL text**—never wrap the SQL in a JSON object.
   - All column names are **all-lowercase** and every query **must** include `WHERE timestamp >= now() - INTERVAL '<N>' HOUR`.

3. **Verified `dex_device_state_events` Schema Gotchas**:
   - Use exact column names: `warpcolocode` (not `colo`), `tunneltype` (not `connectionprotocol`), `networkssid` (not `wifiessid`), and `wifistrengthdbm` (not `wifirssidbm`).
   - `cf logs datasets get dex_device_state_events` may return `404` even when `cf logs query` queries `dex_device_state_events` directly—query the table directly instead of gating on `datasets get`.
   - `dex_device_state_events` has **no** email, username, hostname, hardware model, or OS version columns—join with the Physical Devices API (`/devices/physical-devices`) via `deviceid`.

4. **Parse Nested JSON Strings & Normalize Units**:
   - `tunnelstatsdownstream`, `tunnelstatsupstream`, `cpupercentagebyapp`, and `ramusedpercentagebyapp` are returned as **JSON-encoded strings**.
   - `cpupercentage`, `ramusedpercentage`, `diskusagepercentage`, and `batterypercentage` are **`0.0`–`1.0` fractions** (× 100 for `%`).
   - `rttUs`, `minRttUs`, and `rttVarUs` inside `tunnelstats*` are in **microseconds** (÷ 1,000 for `ms`).
   - `ramavailablekb` is total installed RAM in **KB** (÷ 1,048,576 for GB); `network*bps` and `disk*bps` are in **Bytes/sec**.
   - Desktops report `batterypercentage = 0`, `batterycycles = 0`, `batterycharging = false`—treat as **AC / No Battery**, not a low-battery alert.
   - Multi-process apps (e.g., browser renderers) can appear multiple times in a single `*byapp` snapshot; sum duplicate process names per timestamp before aggregating.

5. **Respect the 30-Second Query Timeout**:
   - `dex_device_state_events` emits ~1 row every **2 minutes** (~720 rows/day per device). Always filter by `deviceid = '<DEVICE_ID>'` and `timestamp >= ...`, and set `LIMIT 1500` (or widen carefully with `GROUP BY` aggregations for multi-day windows).

---

## Fast Path: Automated Collector & Visualizer Script

Run [device-metrics.py](./scripts/device-metrics.py) (zero external Python dependencies; uses Python 3 standard library only). It auto-detects `cf` CLI or falls back to the Client v4 REST API, enriches `dex_device_state_events` with Physical Device metadata, `dex_application_tests`, `warp_toggle_changes`, `warp_config_changes`, and `device_posture_results`, prints a sparkline summary, and optionally generates a **self-contained offline HTML dashboard**:

```bash
# 1. Discover devices on an account
./cf1-device-metrics/scripts/device-metrics.py --account-id <ACCOUNT_ID> --list-devices

# 2. Full enriched report + interactive HTML dashboard (by user email, last 24h)
./cf1-device-metrics/scripts/device-metrics.py \
  --account-id <ACCOUNT_ID> \
  --email user@example.com \
  --hours 24 \
  --html /tmp/device-metrics.html --open

# 3. Target a specific device UUID with Markdown output + JSON snapshot
./cf1-device-metrics/scripts/device-metrics.py \
  --account-id <ACCOUNT_ID> \
  --device-id 00000000-0000-0000-0000-000000000000 \
  --hours 12 \
  --format markdown \
  --json /tmp/device-snapshot.json \
  --html /tmp/device-metrics.html

# 4. Force direct Client v4 REST API mode (when cf CLI is not installed)
CLOUDFLARE_API_TOKEN="<TOKEN>" ./cf1-device-metrics/scripts/device-metrics.py \
  --mode api \
  --account-id <ACCOUNT_ID> \
  --email user@example.com \
  --html /tmp/device-metrics.html

# 5. Fast state-only mode (skip optional enrichment datasets)
./cf1-device-metrics/scripts/device-metrics.py \
  --account-id <ACCOUNT_ID> \
  --device-id 00000000-0000-0000-0000-000000000000 \
  --no-enrich

# 6. Re-render offline from a saved JSON snapshot (no API calls)
./cf1-device-metrics/scripts/device-metrics.py \
  --from-json /tmp/device-snapshot.json \
  --html /tmp/device-metrics.html
```

**What the interactive HTML dashboard renders:**
1. **Device & Posture Header Card:** User, hostname, model, OS, WARP version, edge colo, tunnel protocol/mode, ISP/ASN/geo, Wi-Fi SSID & dBm, and security flags (`Always-On`, `Switch Lock`, `System Firewall`, `App Firewall`).
2. **Automated Health Findings:** Threshold-based callouts (disconnects, MASQUE jitter/packet loss, weak Wi-Fi, sustained RAM/CPU/disk pressure, failing posture checks, HTTP DEX errors).
3. **KPI Strip & Synchronized Time-Series Charts** *(with range filters `All / 1h / 4h / 12h` and aligned hover crosshair)*:
   - **Connectivity & Path Ribbon:** WARP status, edge colo, network/SSID, and ISP transitions.
   - **Tunnel Quality:** Handshake latency, upstream/downstream RTT, max jitter (`rttVarUs`), and packet-loss markers.
   - **Wi-Fi Signal & Throughput:** RSSI (`dBm`) with quality bands (`>-60 / -60..-70 / <-70 dBm`) or Rx/Tx KB/s on ethernet.
   - **System Resources:** CPU %, RAM %, Disk %, and Battery % (auto-hidden on desktops).
4. **Top Resource-Consuming Apps:** Aggregated CPU & RAM process rankings with estimated MB/GB and sample presence %.
5. **Enriched Panels:** DEX HTTP resource-timing waterfalls (`DNS / TCP / TLS / TTFB / Download`), expandable Traceroute hop-by-hop tables, Device Posture pass/fail table, and WARP toggle/config change timeline.

---

## Manual Step-by-Step Queries

### Step 1: Identify Account & Resolve Device ID
- **`cf` CLI:**
  ```bash
  cf accounts list -q
  CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf zero-trust devices list --last-seen-user-email "user@example.com" -q
  ```
- **Client v4 REST API:**
  ```bash
  curl -sS "https://api.cloudflare.com/client/v4/accounts" \
    -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"

  curl -sS -G "https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/devices/physical-devices" \
    -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
    --data-urlencode "last_seen_user.email=user@example.com"
  ```

### Step 2: Query `dex_device_state_events`
Execute any SQL below via `cf logs query -q --body "<SQL>"` or `POST /client/v4/accounts/<ACCOUNT_ID>/logs/explorer/query/sql` (`Content-Type: text/plain`):

1. **Pull full chronological telemetry for a single device (last 24h):**
   ```sql
   SELECT *
   FROM dex_device_state_events
   WHERE timestamp >= now() - INTERVAL '24' HOUR
     AND deviceid = '<DEVICE_ID>'
   ORDER BY timestamp DESC
   LIMIT 1000
   ```
2. **Quick device health summary (status, colo, ISP, Wi-Fi, CPU/RAM/disk averages):**
   ```sql
   SELECT
     status,
     mode,
     tunneltype,
     warpcolocode,
     connectiontype,
     ispipv4aso,
     count() AS samples,
     round(avg(handshakelatencyms), 1) AS avg_handshake_ms,
     round(avg(cpupercentage) * 100, 1) AS avg_cpu_pct,
     round(avg(ramusedpercentage) * 100, 1) AS avg_ram_pct,
     round(avg(diskusagepercentage) * 100, 1) AS avg_disk_pct,
     min(wifistrengthdbm) AS min_wifi_dbm
   FROM dex_device_state_events
   WHERE timestamp >= now() - INTERVAL '24' HOUR
     AND deviceid = '<DEVICE_ID>'
   GROUP BY status, mode, tunneltype, warpcolocode, connectiontype, ispipv4aso
   ORDER BY samples DESC
   ```

### Step 3: Enrich with DEX Tests, WARP Changes & Posture
1. **Resolve DEX test names** (`testid` → human name):
   - **`cf` CLI:** `CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf zero-trust dex tests list -q`
   - **REST API:** `GET /client/v4/accounts/<ACCOUNT_ID>/dex/devices/dex_tests`
2. **Query DEX synthetic HTTP & Traceroute results (`dex_application_tests`):**
   ```sql
   SELECT timestamp, testid, testtype, colocode, executioncontext,
          httpurl, httpstatuscode, httperrormessage,
          httpdomainlookupstartms, httpdomainlookupendms,
          httpconnectstartms, httpconnectendms, httpsecureconnectionstartms,
          httprequeststartms, httpresponsestartms, httpresponseendms,
          traceroutedestinationhostname, traceroutedurationms, tracerouteprotocol, traceroutehops
   FROM dex_application_tests
   WHERE timestamp >= now() - INTERVAL '24' HOUR
     AND deviceid = '<DEVICE_ID>'
   ORDER BY timestamp DESC
   LIMIT 200
   ```
3. **Correlate user WARP toggles, profile switches & posture checks:**
   ```sql
   SELECT timestamp, hostname, toggled, useremail
   FROM warp_toggle_changes
   WHERE timestamp >= now() - INTERVAL '24' HOUR
     AND deviceid = '<DEVICE_ID>'
   ORDER BY timestamp DESC LIMIT 50;

   SELECT timestamp, hostname, confignamefrom, confignameto
   FROM warp_config_changes
   WHERE timestamp >= now() - INTERVAL '24' HOUR
     AND deviceid = '<DEVICE_ID>'
   ORDER BY timestamp DESC LIMIT 50;

   SELECT timestamp, posturecheckname, posturechecktype, postureevaluatedresult
   FROM device_posture_results
   WHERE timestamp >= now() - INTERVAL '24' HOUR
     AND deviceid = '<DEVICE_ID>'
   ORDER BY timestamp DESC LIMIT 50;
   ```

---

## Reference Documentation
- **Local Verified Schema, JSON Payloads, Thresholds & API Mapping:** [dex-schema-reference.md](./references/dex-schema-reference.md)
- **DEX Device State Events Fields:** https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/dex_device_state_events/
- **DEX Application Tests Fields:** https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/dex_application_tests/
- **Cloudflare DEX Documentation:** https://developers.cloudflare.com/cloudflare-one/insights/dex/
- **Cloudflare Log Explorer SQL Guide:** https://developers.cloudflare.com/logs/log-explorer/query-logs/
