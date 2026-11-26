# Cloudflare One Device Telemetry & DEX Schema Reference

Verified schema reference for `dex_device_state_events`, enrichment datasets (`dex_application_tests`, `warp_toggle_changes`, `warp_config_changes`, `device_posture_results`), nested JSON payload formats, unit normalizations, health thresholds, and `cf` CLI ↔ Cloudflare Client v4 REST API mappings.

> [!IMPORTANT]
> All Log Explorer SQL column names are **all-lowercase** (even though Logpush documentation prints `PascalCase`), and every query **must** include a `WHERE <timestamp_column> >= ...` time filter.

---

## 1. Primary Dataset: `dex_device_state_events` (59 Columns)

- **Sampling cadence:** ~every **2 minutes** (`120s`) while the WARP client is active (~30 rows/hr, ~720 rows/day per device).
- **Timestamp column:** `timestamp` (ISO-8601 UTC string; partition column `date` is `YYYY-MM-DD`).
- **Device join key:** `deviceid` (matches `id` in `GET /accounts/{account_id}/devices/physical-devices`) and `deviceregistrationid`.
- **Important caveats:**
  - Contains **no** user email, username, hostname, hardware model, or OS version — join with the Physical Devices API to enrich identity metadata.
  - `cf logs datasets get dex_device_state_events` may return `404 Not Found` even when `cf logs query` succeeds directly against `dex_device_state_events`.

### 1.1 Identity, Client & Security Posture
| Column | Type | Example / Notes |
| :--- | :--- | :--- |
| `timestamp` | `string` | `2026-01-15T08:54:16Z` (required in `WHERE`) |
| `date` | `string` | `2026-01-15` (partition date) |
| `accountid` | `string` | Cloudflare account ID |
| `deviceid` | `string` | Physical device UUID |
| `deviceregistrationid` | `string` | Device registration UUID |
| `deviceregistrationprofileid` | `string` | Device profile ID or `"default"` |
| `clientplatform` | `string` | `macos`, `windows`, `linux`, `ios`, `android`, `chromeos` |
| `clientversion` | `string` | WARP client version (e.g. `2026.8.2115.0`) |
| `status` | `string` | `connected`, `paused`, `disconnected`, `connecting`, etc. |
| `mode` | `string` | `warp+doh`, `warp`, `doh`, `proxy`, `posture_only`, etc. |
| `tunneltype` | `string` | `masque`, `wireguard`, `http2`, or empty when disconnected |
| `dohsubdomain` | `string` | Gateway DoH subdomain (e.g. `<id>.cloudflare-gateway.com`) |
| `alwayson` | `bool` | Whether WARP daemon auto-reconnects |
| `switchlocked` | `bool` | Whether users are locked from disabling WARP |
| `firewallenabled` | `bool` | OS system-level firewall status |
| `appfirewallenabled` | `bool` | Application-level firewall status |
| `experimentalextra` | `string` | JSON string or `"null"`; unstructured experimental fields |

### 1.2 Cloudflare Edge & MASQUE/WARP Tunnel Quality
| Column | Type | Unit / Notes |
| :--- | :--- | :--- |
| `warpcolocode` | `string` | 3-letter Cloudflare edge airport code (e.g. `SYD`, `SJC`, `LHR`, or `none`) |
| `handshakelatencyms` | `int` | Estimated tunnel handshake latency in **ms** (`-1` when disconnected) |
| `tunnelstatsdownstream` | `string` | **JSON-encoded object** (see §1.5) — edge → device transport stats |
| `tunnelstatsupstream` | `string` | **JSON-encoded object** (see §1.5) — device → edge transport stats |

### 1.3 Local Network, Wi-Fi & ISP / Geolocation
| Column | Type | Unit / Notes |
| :--- | :--- | :--- |
| `connectiontype` | `string` | `wifi`, `ethernet`, `cellular` |
| `networkssid` | `string` | Connected Wi-Fi SSID (max 32 chars; empty on ethernet) |
| `wifistrengthdbm` | `int` | Wi-Fi RSSI in **dBm** (typically `-30` to `-90`; `0` when not on Wi-Fi) |
| `networkreceivedbps` | `int` | Interface ingress rate in **Bytes/sec** |
| `networksentbps` | `int` | Interface egress rate in **Bytes/sec** |
| `deviceipv4address`, `deviceipv4netmask` | `string` | Local private IPv4 and prefix length (e.g. `192.0.2.10`, `24`) |
| `deviceipv6address`, `deviceipv6netmask` | `string` | Local IPv6 and prefix length |
| `gatewayipv4address`, `gatewayipv4netmask` | `string` | Local router/gateway IPv4 and prefix length |
| `gatewayipv6address`, `gatewayipv6netmask` | `string` | Local router/gateway IPv6 and prefix length |
| `ispipv4address`, `ispipv4netmask` | `string` | Public ISP IPv4 address and mask |
| `ispipv4asn`, `ispipv4aso` | `int`, `string` | Public ISP ASN (e.g. `13335`) and organization name |
| `ispipv4city`, `ispipv4stateiso`, `ispipv4countryiso`, `ispipv4zip` | `string` | IPv4 ISP geolocation |
| `ispipv6address`, `ispipv6netmask`, `ispipv6asn`, `ispipv6aso`, `ispipv6city`, `ispipv6stateiso`, `ispipv6countryiso`, `ispipv6zip` | `string`/`int` | IPv6 ISP network & geolocation (`0` / `""` if IPv4-only) |

### 1.4 System Health & Top Resource Consumers
| Column | Type | Unit / Normalization |
| :--- | :--- | :--- |
| `cpupercentage` | `float` | `0.0` – `1.0` fraction (**multiply by 100** for %) |
| `cpupercentagebyapp` | `string` | **JSON-encoded array** of top 5 processes (see §1.5) |
| `ramusedpercentage` | `float` | `0.0` – `1.0` fraction (**multiply by 100** for %) |
| `ramavailablekb` | `int` | Total installed/available system RAM in **KB** (`÷ 1,048,576` = GB; multiply by `ramusedpercentage` for GB used) |
| `ramusedpercentagebyapp` | `string` | **JSON-encoded array** of top 5 processes (see §1.5) |
| `diskusagepercentage` | `float` | `0.0` – `1.0` fraction (**multiply by 100** for %) |
| `diskreadbps` | `int` | Disk read throughput in **Bytes/sec** (`÷ 1,048,576` = MB/s) |
| `diskwritebps` | `int` | Disk write throughput in **Bytes/sec** (`÷ 1,048,576` = MB/s) |
| `batterypercentage` | `float` | `0.0` – `1.0` fraction (`0` with `batterycycles = 0` & `batterycharging = false` indicates desktop / no battery) |
| `batterycharging` | `bool` | Whether the device is plugged in / charging |
| `batterycycles` | `int` | Battery charge cycle count (`0` on desktops or unsupported OS) |

### 1.5 Nested JSON-Encoded String Schemas

Log Explorer returns these four columns as **JSON strings** — parse them with `json.loads(...)` before plotting or aggregating:

**`tunnelstatsdownstream` / `tunnelstatsupstream`** (populated for MASQUE tunnels):
```json
{
  "rttUs": 7045,
  "minRttUs": 4598,
  "rttVarUs": 1616,
  "packetsSent": 1476,
  "packetsRecvd": 1339,
  "packetsLost": 0,
  "packetsRetransmitted": 0,
  "bytesSent": 560517,
  "bytesRecvd": 337155,
  "bytesLost": 0,
  "bytesRetransmitted": 0,
  "statsWindowMs": 14944
}
```
- `rttUs / 1000.0` → Smoothed RTT in **ms**
- `minRttUs / 1000.0` → Baseline minimum RTT in **ms**
- `rttVarUs / 1000.0` → RTT variance / **jitter** in **ms**
- `packetsLost / packetsSent * 100` → **Packet loss %** (guard against `packetsSent == 0`)
- `packetsRetransmitted / packetsSent * 100` → **Retransmission %**
- `(bytesSent + bytesRecvd) / (statsWindowMs / 1000.0)` → Tunnel throughput in **Bytes/sec**

**`cpupercentagebyapp` / `ramusedpercentagebyapp`**:
```json
[
  {"name": "Browser Helper (Renderer)", "percentage": 0.02},
  {"name": "CloudflareWARP", "percentage": 0.01}
]
```
- `percentage` is a `0.0`–`1.0` fraction (multiply by `100` for %).
- Multiple PIDs with the same process `name` (e.g. browser/Electron renderer helpers) can appear in the same snapshot — sum duplicate names within each timestamp before computing per-app average and peak across the window.

---

## 2. Enrichment Datasets (Verified Columns)

### 2.1 `dex_application_tests`
- **Timestamp:** `timestamp` | **Device Key:** `deviceid`, `deviceregistrationid` *(no `email`/`userid` column)*
- **Common columns:** `testid`, `testtype` (`http` or `traceroute`), `colocode`, `executioncontext` (`inTunnel` or `outOfTunnel`), `clientplatform`, `clientversion`
- **HTTP test columns (`testtype = 'http'`):**
  - `httpurl`, `httpmethod`, `httpstatuscode`, `httperrormessage`
  - Resource Timing (ms elapsed from test start):
    - `httpdomainlookupstartms`, `httpdomainlookupendms` → **DNS time** = `end - start`
    - `httpconnectstartms`, `httpconnectendms`, `httpsecureconnectionstartms` → **TCP time** = `(secureStart or connectEnd) - connectStart`; **TLS time** = `connectEnd - secureStart` (when `secureStart > 0`)
    - `httprequeststartms`, `httpresponsestartms` → **TTFB / Wait** = `responseStart - requestStart`
    - `httpresponseendms` → **Total HTTP time**; **Download** = `responseEnd - responseStart`
  - `httpresponseheaderbytes`, `httpresponsebodybytes`
  - `httpserveripaddress`, `httpserveripasn`, `httpserveripaso`, `httpserveripcity`, `httpserveripcountryiso`
- **Traceroute test columns (`testtype = 'traceroute'`):**
  - `traceroutedestinationhostname`, `traceroutedestinationipaddress`, `traceroutedestinationipasn`, `traceroutedestinationipaso`, `traceroutedestinationipcity`, `traceroutedestinationipcountryiso`
  - `traceroutedurationms`, `tracerouteprotocol` (`icmp`, `udp`, `tcp`), `traceroutemaxttl`, `traceroutesize`
  - `traceroutehops`: JSON-encoded array of hop objects:
    ```json
    [
      {
        "ttl": 1,
        "name": "router.local",
        "pathID": 1,
        "sent": 3,
        "received": 3,
        "rtts": [2, 3, 2],
        "errors": [],
        "ip": {"address": "192.0.2.1", "asn": 0, "aso": "", "version": "v4", "location": {"city": "", "countryISO": ""}}
      }
    ]
    ```
- **Resolving `testid` to human-readable test names:** Call `cf zero-trust dex tests list` (`GET /accounts/{account_id}/dex/devices/dex_tests`).

### 2.2 `warp_toggle_changes` & `warp_config_changes`
- **Timestamp:** `timestamp` | **Device/User Keys:** `deviceid`, `deviceregistrationid`, `useremail`, `hostname`, `serialnumber`
- `warp_toggle_changes`: `toggled` (`true` = user turned WARP ON, `false` = user turned WARP OFF), `accountname`
- `warp_config_changes`: `confignamefrom`, `confignameto`, `accountidfrom`, `accountidto`, `accountnamefrom`, `accountnameto`

### 2.3 `device_posture_results`
- **Timestamp:** `timestamp` | **Device/User Keys:** `deviceid`, `registrationid`, `email`, `useruid`
- **Posture fields:** `posturecheckname`, `posturechecktype`, `postureevaluatedresult` (`true`/`false`), `policyid`, `clientversion`, `osversion`

---

## 3. Health & Anomaly Thresholds Used in Reports

| Metric | Healthy | Warning | Critical | Notes |
| :--- | :--- | :--- | :--- | :--- |
| **WARP Uptime (`status = 'connected'`)** | ≥ 99% | 95% – 99% | < 95% | Correlate drops with `warp_toggle_changes` |
| **Handshake Latency (`handshakelatencyms`)** | < 50 ms | 50 – 150 ms | > 150 ms | `-1` indicates disconnected |
| **MASQUE Tunnel RTT (`rttUs / 1000`)** | < 60 ms | 60 – 150 ms | > 150 ms | Compare against `minRttUs` to spot bufferbloat |
| **Tunnel Jitter (`rttVarUs / 1000`)** | < 10 ms | 10 – 30 ms | > 30 ms | High jitter degrades VoIP/video calls |
| **Tunnel Packet Loss (`packetsLost / packetsSent`)** | < 0.5% | 0.5% – 2.0% | > 2.0% | Check both upstream & downstream |
| **Wi-Fi RSSI (`wifistrengthdbm`)** | > -60 dBm (`> -50` Excellent) | -60 to -70 dBm (Fair) | < -70 dBm (Weak/Poor) | Only evaluated when `connectiontype = 'wifi'` and `< 0` |
| **CPU Utilization (`cpupercentage × 100`)** | < 70% | 70% – 85% | > 85% | Check `cpupercentagebyapp` for culprit |
| **RAM Utilization (`ramusedpercentage × 100`)** | < 75% | 75% – 85% | > 85% | Sustained >80% triggers swap pressure |
| **Disk Usage (`diskusagepercentage × 100`)** | < 80% | 80% – 90% | > 90% | Low free space degrades OS/SSD performance |
| **Battery Level (`batterypercentage × 100`)** | > 25% | 15% – 25% | < 15% (unplugged) | Ignored on desktop / AC-only devices (`0%`, `0` cycles, not charging) |
| **HTTP DEX Test (`httpresponseendms`)** | < 500 ms (2xx/3xx) | 500 – 1500 ms | > 1500 ms or 4xx/5xx/error | Inspect DNS/TCP/TLS/TTFB breakdown |

---

## 4. `cf` CLI ↔ Cloudflare Client v4 REST API Mapping

Base URL for all REST endpoints: `https://api.cloudflare.com/client/v4`

### Authentication & Required API Token Permissions
- **Bearer Token (Preferred):** `-H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"`
  - `Account` → `Logs` → `Read` *(or `Log Explorer` → `Read`)*
  - `Account` → `Zero Trust` → `Read`
  - `Account` → `Digital Experience Monitoring` → `Read` *(for DEX API endpoints)*
  - `Account` → `Account Settings` → `Read` *(for account discovery via `GET /accounts`)*
- **Legacy Global API Key:** `-H "X-Auth-Email: $CLOUDFLARE_EMAIL" -H "X-Auth-Key: $CLOUDFLARE_API_KEY"`
- **Envelope Unwrapping:** `cf` CLI outputs `.result` directly; direct Client v4 REST responses wrap data in `{"result": ..., "success": true, "errors": [], "messages": []}`.

### Endpoint Mapping Table
| Operation | `cf` CLI Command | Client v4 REST API Method & Path |
| :--- | :--- | :--- |
| **List Accounts** | `cf accounts list -q` | `GET /accounts` |
| **List Physical Devices (by email)** | `cf zero-trust devices list --last-seen-user-email <email> -q` | `GET /accounts/{account_id}/devices/physical-devices?last_seen_user.email={email}` |
| **List All Physical Devices** | `cf zero-trust devices list -q` | `GET /accounts/{account_id}/devices/physical-devices` |
| **Get Physical Device Details** | `cf zero-trust devices get <device-id> -q` | `GET /accounts/{account_id}/devices/physical-devices/{device_id}` |
| **List Device Registrations** | `cf zero-trust devices registrations list -q` | `GET /accounts/{account_id}/devices/registrations?device.id={device_id}` |
| **List DEX Synthetic Tests** | `cf zero-trust dex tests list -q` | `GET /accounts/{account_id}/dex/devices/dex_tests` |
| **Get DEX Live Device Status** | `cf zero-trust dex devices get <device-id> --since-minutes 60 -q` | `GET /accounts/{account_id}/dex/devices/{device_id}/fleet-status/live?since_minutes=60` |
| **Get DEX Status Over Time** | `cf zero-trust dex devices over-time get <device-id> --from <iso> --to <iso> --interval minute -q` | `GET /accounts/{account_id}/dex/devices/{device_id}/fleet-status/over-time?from={iso}&to={iso}&interval=minute` |
| **List DEX Fleet Devices** | `cf zero-trust dex devices list --from <iso> --to <iso> --page 1 --per-page 50 --source raw -q` | `GET /accounts/{account_id}/dex/fleet-status/devices?from={iso}&to={iso}&page=1&per_page=50&source=raw` |
| **List DEX WARP Change Events** | `cf zero-trust dex warp-change-events list --from <iso> --to <iso> --page 1 --per-page 50 -q` | `GET /accounts/{account_id}/dex/warp-change-events?from={iso}&to={iso}&page=1&per_page=50` |
| **Run Log Explorer SQL Query** | `cf logs query -q --body "<RAW_SQL>"` | `POST /accounts/{account_id}/logs/explorer/query/sql` *(raw SQL body, `Content-Type: text/plain`)* |

---

## 5. Official Documentation Links
- **DEX Device State Events Dataset:** https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/dex_device_state_events/
- **DEX Application Tests Dataset:** https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/dex_application_tests/
- **WARP Toggle Changes Dataset:** https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/warp_toggle_changes/
- **WARP Config Changes Dataset:** https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/warp_config_changes/
- **Device Posture Results Dataset:** https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/device_posture_results/
- **Cloudflare DEX Overview:** https://developers.cloudflare.com/cloudflare-one/insights/dex/
- **Log Explorer SQL Query Guide:** https://developers.cloudflare.com/logs/log-explorer/query-logs/
