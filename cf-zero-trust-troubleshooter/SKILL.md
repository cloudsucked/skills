---
name: cf-zero-trust-troubleshooter
description: >-
  Troubleshoot Cloudflare Zero Trust (WARP, Gateway DNS/HTTP/Network, Access,
  Device Posture, DEX, and BISO) issues for a specific user or device using the
  Cloudflare `cf` CLI, Log Explorer SQL API (`cf logs`), and Analytics SQL API
  (`cf analytics sql`). Activate this skill whenever investigating user
  connectivity issues, blocked domains/apps, posture check failures, WARP
  disconnects, or auditing user activity in Cloudflare Zero Trust.
---

# Cloudflare Zero Trust User Troubleshooting Skill

Use this runbook to rapidly identify a user's devices and query Cloudflare Zero Trust logs and analytics via the `cf` CLI.

---

## Critical `cf` CLI Rules (Avoid Common Pitfalls)

1. **Always Pass `CLOUDFLARE_ACCOUNT_ID`**:
   Without `CLOUDFLARE_ACCOUNT_ID`, `cf` opens an interactive account picker (`◆ Select an account`) and hangs in non-interactive shells.
   - Discover accounts first with: `cf accounts list`
   - Prefix subsequent commands with `CLOUDFLARE_ACCOUNT_ID="<account_id>"`.

2. **Raw SQL vs. JSON Request Bodies**:
   - **`cf logs query --body` requires RAW SQL text** (do **not** wrap in JSON):
     ```bash
     CLOUDFLARE_ACCOUNT_ID="<ID>" cf logs query --body "SELECT datetime, queryname FROM gateway_dns WHERE datetime >= now() - INTERVAL '1' HOUR LIMIT 10"
     ```
   - **`cf analytics sql post --body` requires a JSON object**:
     ```bash
     cf analytics sql post --body '{"query": "SELECT ...", "scope-account-tag": "<ID>"}'
     ```

3. **All Column Names Are Lowercase**:
   Official Logpush documentation writes fields in `PascalCase` (e.g., `ResolverDecision`, `DeviceID`), but Log Explorer SQL requires **all-lowercase** (`resolverdecision`, `deviceid`).

4. **Dataset-Specific Timestamp & User Columns**:
   Different datasets use different timestamp and email column names (`datetime` vs `timestamp` vs `createdat` vs `sessionendtime`, and `email` vs `useremail` vs `actoremail`). Check [datasets-reference.md](./references/datasets-reference.md) before writing custom queries.

5. **Prevent Query Timeouts (30s API Limit)**:
   High-volume tables (`gateway_dns`, `gateway_network`) can time out over 24-hour scans.
   - Always filter on the dataset's timestamp field (`WHERE <ts> >= now() - INTERVAL '24' HOUR`).
   - Always filter by `deviceid` or `email`/`useremail`.
   - Start with `GROUP BY` aggregations or narrow time windows (`INTERVAL '1' HOUR`, `LIMIT 50`) before pulling raw rows.

---

## Step-by-Step Troubleshooting Workflow

### Fast Path: Automated Triage Script
Run the bundled helper script [investigate-user.sh](./scripts/investigate-user.sh) to automatically resolve a user's WARP devices and summarize their last 24 hours across Zero Trust datasets:
```bash
./.agents/skills/cf-zero-trust-troubleshooter/scripts/investigate-user.sh <user_email> [account_id] [hours]
```

---

### Manual Step 1: Identify Account, User, and WARP Devices
1. **List accounts** (if account ID is unknown):
   ```bash
   cf accounts list
   ```
2. **Find the user's registered WARP devices**:
   ```bash
   CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf zero-trust devices list --last-seen-user-email "<USER_EMAIL>"
   ```
   *Record from output:*
   - `id` (`deviceid`, e.g., `00000000-0000-0000-0000-000000000000`)
   - `last_seen_user.id` (`userid` / `useruid`)
   - `name` (`devicename` / `hostname`), `device_type`, `os_version`, `client_version`, `public_ip`, `last_seen_at`
3. **Check Zero Trust user record & seat/risk status** (optional):
   ```bash
   CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf zero-trust users list
   ```

---

### Manual Step 2: Check Enabled Log Explorer Datasets
Verify which Log Explorer datasets are enabled on the account before querying:
```bash
CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf logs datasets list
```
To inspect enabled fields for a specific dataset ID:
```bash
CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf logs datasets get <DATASET_ID>
```

---

### Manual Step 3: Run Targeted Diagnostic Queries

#### A. Gateway DNS Filtering (`gateway_dns`)
*Timestamp:* `datetime` | *User/Device:* `email`, `userid`, `deviceid`
1. **Summarize decisions (Allowed vs. Blocked)**:
   ```bash
   CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf logs query --body \
     "SELECT resolverdecision, policyname, count() AS total FROM gateway_dns WHERE datetime >= now() - INTERVAL '1' DAY AND (email = '<USER_EMAIL>' OR deviceid = '<DEVICE_ID>') GROUP BY resolverdecision, policyname ORDER BY total DESC"
   ```
2. **Inspect blocked DNS queries**:
   ```bash
   CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf logs query --body \
     "SELECT datetime, queryname, querytypename, resolverdecision, policyname, matchedcategorynames FROM gateway_dns WHERE datetime >= now() - INTERVAL '1' DAY AND (email = '<USER_EMAIL>' OR deviceid = '<DEVICE_ID>') AND resolverdecision LIKE '%block%' ORDER BY datetime DESC LIMIT 50"
   ```

#### B. Gateway HTTP Inspection (`gateway_http`)
*Timestamp:* `datetime` | *User/Device:* `email`, `userid`, `deviceid`
```bash
CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf logs query --body \
  "SELECT datetime, action, httphost, httpmethod, httpstatuscode, policyname, url, untrustedcertificateaction FROM gateway_http WHERE datetime >= now() - INTERVAL '1' DAY AND (email = '<USER_EMAIL>' OR deviceid = '<DEVICE_ID>') ORDER BY datetime DESC LIMIT 50"
```

#### C. Gateway Layer-4 Network Logs (`gateway_network`)
*Timestamp:* `datetime` | *User/Device:* `email`, `userid`, `deviceid`
*(Tip: Use a 1-4 hour window or aggregate first, as `gateway_network` is high-volume.)*
```bash
CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf logs query --body \
  "SELECT action, policyname, destinationip, destinationport, sni, transportprotocol, count() AS total FROM gateway_network WHERE datetime >= now() - INTERVAL '4' HOUR AND (email = '<USER_EMAIL>' OR deviceid = '<DEVICE_ID>') GROUP BY action, policyname, destinationip, destinationport, sni, transportprotocol ORDER BY total DESC LIMIT 30"
```

#### D. Device Posture Failures (`device_posture_results`)
*Timestamp:* `timestamp` | *User/Device:* `email`, `useruid`, `deviceid`
```bash
CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf logs query --body \
  "SELECT timestamp, posturecheckname, posturechecktype, postureevaluatedresult, clientversion, osversion FROM device_posture_results WHERE timestamp >= now() - INTERVAL '1' DAY AND (deviceid = '<DEVICE_ID>' OR email = '<USER_EMAIL>') ORDER BY timestamp DESC LIMIT 25"
```

#### E. Access Application Authentication (`access_requests`)
*Timestamp:* `createdat` | *User/Device:* `email`, `useruid`
```bash
CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf logs query --body \
  "SELECT createdat, action, allowed, appdomain, ipaddress, country FROM access_requests WHERE createdat >= now() - INTERVAL '1' DAY AND email = '<USER_EMAIL>' ORDER BY createdat DESC LIMIT 50"
```

#### F. WARP Client Toggles & Profile Changes (`warp_toggle_changes`, `warp_config_changes`)
*Timestamp:* `timestamp` | *User/Device:* `useremail`, `deviceid`
```bash
# Check if user toggled WARP off/on
CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf logs query --body \
  "SELECT timestamp, hostname, toggled, useremail FROM warp_toggle_changes WHERE timestamp >= now() - INTERVAL '1' DAY AND (deviceid = '<DEVICE_ID>' OR useremail = '<USER_EMAIL>') ORDER BY timestamp DESC LIMIT 25"

# Check if WARP profile/config switched
CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf logs query --body \
  "SELECT timestamp, hostname, confignamefrom, confignameto FROM warp_config_changes WHERE timestamp >= now() - INTERVAL '1' DAY AND (deviceid = '<DEVICE_ID>' OR useremail = '<USER_EMAIL>') ORDER BY timestamp DESC LIMIT 25"
```

#### G. Zero Trust Network Sessions (`zero_trust_network_sessions`)
*Timestamp:* `sessionendtime` | *User/Device:* `email`, `userid`, `deviceid`
```bash
CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf logs query --body \
  "SELECT sessionstarttime, sessionendtime, destinationip, destinationport, originip, bytesreceived, bytessent, connectionclosereason FROM zero_trust_network_sessions WHERE sessionendtime >= now() - INTERVAL '4' HOUR AND (deviceid = '<DEVICE_ID>' OR email = '<USER_EMAIL>') ORDER BY sessionendtime DESC LIMIT 30"
```

---

## Reference Documentation
- **Local Dataset & Schema Cheat Sheet**: [datasets-reference.md](./references/datasets-reference.md)
- **Cloudflare Log Explorer Documentation**: https://developers.cloudflare.com/logs/log-explorer/
- **Log Explorer SQL Query Guide**: https://developers.cloudflare.com/logs/log-explorer/query-logs/
- **Logpush & Log Explorer Dataset Fields Reference**: https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/
- **Cloudflare Analytics SQL API Documentation**: https://developers.cloudflare.com/analytics/sql-api/
- **Cloudflare Zero Trust Documentation**: https://developers.cloudflare.com/cloudflare-one/
