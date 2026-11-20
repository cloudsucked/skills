---
name: cf-zero-trust-troubleshooter
description: >-
  Troubleshoot Cloudflare Zero Trust (WARP, Gateway DNS/HTTP/Network, Network
  Sessions, TLS/certificate errors, Access, Device Posture, DEX, and BISO)
  issues for a specific user or device using either the Cloudflare `cf` CLI or
  the Cloudflare Client v4 REST API (`curl`/HTTP) across Log Explorer SQL and
  Analytics SQL. Activate this skill whenever investigating user connectivity
  issues, TLS handshake or certificate errors (cert pinning, missing Root CA,
  untrusted origin certs), blocked domains/IPs/apps, posture check failures,
  WARP disconnects, or auditing user activity in Cloudflare Zero Trust.
---

# Cloudflare Zero Trust User Troubleshooting Skill

Use this runbook to rapidly identify a user's devices and query Cloudflare Zero Trust logs and analytics using **either** the Cloudflare `cf` CLI **or** the **Cloudflare Client v4 REST API** directly (when `cf` is not installed).

---

## Execution Mode Selection (`cf` CLI vs. Direct Client API)

Before running commands, check what is available in the environment:
```bash
command -v cf
```
- **Mode A — `cf` CLI Available:** Use `cf` commands prefixed with `CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>"`.
- **Mode B — No `cf` CLI (Direct Client v4 REST API):** Use `curl` against `https://api.cloudflare.com/client/v4` authenticated via:
  - **API Token (Recommended):** `CLOUDFLARE_API_TOKEN` header `-H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"`
    - *Required Token Permissions:* `Account > Logs > Read` (or `Log Explorer > Read`), `Account > Zero Trust > Read`, `Account > Account Settings > Read`.
  - **Legacy Global API Key:** `-H "X-Auth-Email: $CLOUDFLARE_EMAIL" -H "X-Auth-Key: $CLOUDFLARE_API_KEY"`

---

## Critical Rules (Avoid Common Pitfalls)

1. **Always Scope to an Account ID**:
   - **`cf` CLI:** Without `CLOUDFLARE_ACCOUNT_ID`, `cf` opens an interactive TUI picker (`◆ Select an account`) and hangs in non-interactive shells. Always set `CLOUDFLARE_ACCOUNT_ID="<account_id>"`.
   - **Client API:** Pass `{account_id}` in the URL path (`/client/v4/accounts/{account_id}/...`).

2. **Raw SQL vs. JSON Request Bodies (Applies to BOTH CLI & REST API)**:
   - **Log Explorer SQL (`cf logs query` / `POST /accounts/{id}/logs/explorer/query/sql`) requires RAW SQL text** — do **not** wrap in JSON (`{"query": "..."}` fails with `400 [20001] sql parser error: Expected: an SQL statement, found: {`):
     ```bash
     # Mode A: cf CLI
     CLOUDFLARE_ACCOUNT_ID="<ID>" cf logs query --body "SELECT datetime, queryname FROM gateway_dns WHERE datetime >= now() - INTERVAL '1' HOUR LIMIT 10"

     # Mode B: Client v4 REST API (curl)
     curl -sS -X POST "https://api.cloudflare.com/client/v4/accounts/<ID>/logs/explorer/query/sql" \
       -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
       -H "Content-Type: text/plain" \
       --data-raw "SELECT datetime, queryname FROM gateway_dns WHERE datetime >= now() - INTERVAL '1' HOUR LIMIT 10"
     ```
   - **Analytics SQL (`cf analytics sql post` / `POST /analytics/sql`) requires a JSON object**:
     ```bash
     # Mode A: cf CLI
     cf analytics sql post --body '{"query": "SELECT ...", "scope-account-tag": "<ID>"}'

     # Mode B: Client v4 REST API (curl)
     curl -sS -X POST "https://api.cloudflare.com/client/v4/analytics/sql" \
       -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
       -H "Content-Type: application/json" \
       --data-raw '{"query": "SELECT ...", "scope-account-tag": "<ID>"}'
     ```

3. **Response Envelope Unwrapping (REST API vs. `cf` CLI)**:
   - `cf` CLI unwraps responses and prints the JSON array directly (`[...]`).
   - Direct Client v4 REST API calls return a wrapper envelope: `{"result": [...], "success": true, "errors": [], "messages": []}`. Always check `.success` and read `.result` (e.g., pipe to `jq '.result'` or `python3`).

4. **All Column Names Are Lowercase & Vary by Dataset**:
   - Logpush docs write fields in `PascalCase` (`OriginTLSCertificateValidationResult`), but Log Explorer SQL requires **all-lowercase** (`origintlscertificatevalidationresult`).
   - Timestamps (`datetime`, `sessionendtime`, `timestamp`, `createdat`), user email (`email`, `useremail`, `actoremail`), and target IPs (`destinationip`/`destinationport` in `gateway_network` vs. `originip`/`originport` in `zero_trust_network_sessions`) vary by table. Check [datasets-reference.md](./references/datasets-reference.md).

5. **Prevent Query Timeouts (30s API Limit)**:
   High-volume tables (`gateway_dns`, `gateway_network`, `zero_trust_network_sessions`) can time out over 24-hour full scans.
   - Always filter on the dataset's timestamp field and user/device (`deviceid`, `email`/`useremail`).
   - Push selective predicates (`action != 'allow'`, `connectionclosereason LIKE '%TLS%'`) into `WHERE` and start with `GROUP BY` aggregations or narrower windows (`INTERVAL '4' HOUR`, `LIMIT 50`) before pulling raw rows.

---

## Step-by-Step Troubleshooting Workflow

### Fast Path: Automated Dual-Mode Triage Script
Run [investigate-user.sh](./scripts/investigate-user.sh). It **automatically detects** whether `cf` CLI is installed and falls back seamlessly to the **Cloudflare Client v4 REST API** (`curl` + `CLOUDFLARE_API_TOKEN`) when `cf` is absent (or when `CF_MODE=api` is set):
```bash
# Uses cf CLI if installed, otherwise uses CLOUDFLARE_API_TOKEN against Client v4 API
./.agents/skills/cf-zero-trust-troubleshooter/scripts/investigate-user.sh <user_email> [account_id] [hours]

# Force direct REST API mode even if cf is installed
CF_MODE=api CLOUDFLARE_API_TOKEN="<TOKEN>" ./.agents/skills/cf-zero-trust-troubleshooter/scripts/investigate-user.sh <user_email> [account_id] [hours]
```

---

### Manual Step 1: Identify Account, User, and WARP Devices
1. **List accounts** (if account ID is unknown):
   - **`cf` CLI:** `cf accounts list`
   - **REST API:**
     ```bash
     curl -sS "https://api.cloudflare.com/client/v4/accounts" \
       -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"
     ```
2. **Find the user's registered WARP devices**:
   - **`cf` CLI:**
     ```bash
     CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf zero-trust devices list --last-seen-user-email "<USER_EMAIL>"
     ```
   - **REST API:**
     ```bash
     curl -sS -G "https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/devices/physical-devices" \
       -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
       --data-urlencode "last_seen_user.email=<USER_EMAIL>"
     ```
   *Record from output (`.result` in REST API):*
   - `id` (`deviceid`, e.g., `00000000-0000-0000-0000-000000000000`)
   - `last_seen_user.id` (`userid` / `useruid`)
   - `name` (`devicename` / `hostname`), `device_type`, `os_version`, `client_version`, `public_ip`, `last_seen_at`
3. **Check Zero Trust user record & seat status** (optional):
   - **`cf` CLI:** `CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf zero-trust access users list`
   - **REST API:** `GET https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/access/users`

---

### Manual Step 2: Check Enabled Log Explorer Datasets
Verify which Log Explorer datasets are enabled on the account before querying:
- **`cf` CLI:**
  ```bash
  CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf logs datasets list
  CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf logs datasets get <DATASET_ID>
  ```
- **REST API:**
  ```bash
  curl -sS "https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/logs/explorer/datasets" \
    -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"
  ```

---

### Manual Step 3: Run Targeted Diagnostic Queries

Execute each SQL query below using **either** helper pattern:
```bash
# Option A (cf CLI):
CLOUDFLARE_ACCOUNT_ID="<ACCOUNT_ID>" cf logs query --body "<SQL>"

# Option B (Direct Client v4 REST API):
curl -sS -X POST "https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/logs/explorer/query/sql" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: text/plain" \
  --data-raw "<SQL>"
```

#### A. Gateway DNS Filtering (`gateway_dns`)
*Timestamp:* `datetime` | *User/Device:* `email`, `userid`, `deviceid`
1. **Summarize decisions (Allowed vs. Blocked)**:
   ```sql
   SELECT resolverdecision, policyname, count() AS total
   FROM gateway_dns
   WHERE datetime >= now() - INTERVAL '1' DAY
     AND (email = '<USER_EMAIL>' OR deviceid = '<DEVICE_ID>')
   GROUP BY resolverdecision, policyname
   ORDER BY total DESC
   ```
2. **Inspect blocked DNS queries**:
   ```sql
   SELECT datetime, queryname, querytypename, resolverdecision, policyname, matchedcategorynames
   FROM gateway_dns
   WHERE datetime >= now() - INTERVAL '1' DAY
     AND (email = '<USER_EMAIL>' OR deviceid = '<DEVICE_ID>')
     AND resolverdecision LIKE '%block%'
   ORDER BY datetime DESC
   LIMIT 50
   ```

#### B. Gateway Layer-4 Network Logs (`gateway_network`)
*Timestamp:* `datetime` | *User/Device:* `email`, `userid`, `deviceid`
Use `gateway_network` to check L4 firewall policy blocks, SNI inspection routing, and non-HTTP TCP/UDP traffic.
1. **Check all blocked or non-allow L4 connections**:
   ```sql
   SELECT action, policyname, sni, destinationip, destinationport, detectedprotocol, transportprotocol, count() AS total
   FROM gateway_network
   WHERE datetime >= now() - INTERVAL '1' DAY
     AND (email = '<USER_EMAIL>' OR deviceid = '<DEVICE_ID>')
     AND action != 'allow'
   GROUP BY action, policyname, sni, destinationip, destinationport, detectedprotocol, transportprotocol
   ORDER BY total DESC
   LIMIT 50
   ```
2. **Inspect L4 policy decisions for a specific hostname/SNI or IP** (even when `action = 'allow'`, useful when correlating TLS failures via `sessionid`):
   ```sql
   SELECT datetime, sessionid, action, policyname, sni, destinationip, destinationport, detectedprotocol, applicationnames
   FROM gateway_network
   WHERE datetime >= now() - INTERVAL '4' HOUR
     AND (email = '<USER_EMAIL>' OR deviceid = '<DEVICE_ID>')
     AND sni LIKE '%<TARGET_DOMAIN>%'
   ORDER BY datetime DESC
   LIMIT 30
   ```

#### C. Zero Trust Network Sessions & Certificate/TLS Issues (`zero_trust_network_sessions`)
*Timestamp:* `sessionendtime` | *User/Device:* `email`, `userid`, `deviceid`
*(Note: Use `originip` / `originport` / `sni` / `resolvedfqdn`—this table does **not** have `destinationip` or `destinationport`.)*

> [!IMPORTANT]
> **Why Certificate Issues Often Never Appear in `gateway_http`:**
> When Gateway TLS decryption intercepts HTTPS and a client rejects Cloudflare's root CA (cert pinning, mTLS, or CLI tools like `git`/`npm`/`python`/`curl` missing the CA) **or** Cloudflare rejects an invalid origin certificate, the TLS handshake aborts **before** an HTTP request is ever sent—leaving `gateway_http` empty. Always query `zero_trust_network_sessions` and `gateway_network` to catch certificate and handshake failures.

1. **Find Inspected TLS Handshake & Origin Certificate Validation Failures**:
   ```sql
   SELECT sni, resolvedfqdn, originip, originport, destinationtunnelid, connectionclosereason, origintlscertificatevalidationresult, origintlscertificateissuer, clienttlsversion, origintlsversion, count() AS sessions
   FROM zero_trust_network_sessions
   WHERE sessionendtime >= now() - INTERVAL '1' DAY
     AND (deviceid = '<DEVICE_ID>' OR email = '<USER_EMAIL>')
     AND (connectionclosereason IN ('CLIENT_TLS_ERROR', 'ORIGIN_TLS_ERROR', 'CLIENT_ERROR', 'ORIGIN_ERROR', 'ORIGIN_UNREACHABLE', 'PROXY_CONN_REFUSED')
          OR origintlscertificatevalidationresult NOT IN ('VALID', 'NONE', ''))
   GROUP BY sni, resolvedfqdn, originip, originport, destinationtunnelid, connectionclosereason, origintlscertificatevalidationresult, origintlscertificateissuer, clienttlsversion, origintlsversion
   ORDER BY sessions DESC
   LIMIT 50
   ```
2. **Find Bypassed TLS Certificate Rejections on Cloudflare Tunnels / Access Private Apps**:
   When Gateway **bypasses** TLS decryption (`Do Not Inspect` / `action = 'off'` or private IP pass-through) to an internal server behind a **Cloudflare Tunnel** (`destinationtunnelid != ''`), Gateway does **not** terminate TLS—so `origintlscertificatevalidationresult` is `'NONE'` and `connectionclosereason` shows `'CLIENT_CLOSED'` or `'CLIENT_ERROR'` (not `ORIGIN_TLS_ERROR` or `CLIENT_TLS_ERROR`) right after the client receives the server's expired/self-signed cert (`bytesreceived` ~500–15,000 B, `bytessent` < 5,000 B):
   ```sql
   SELECT sni, resolvedfqdn, originip, originport, destinationtunnelid, virtualnetworkid, connectionclosereason, origintlscertificatevalidationresult, count() AS aborted_handshakes, min(bytessent) AS min_sent, max(bytesreceived) AS max_rcvd
   FROM zero_trust_network_sessions
   WHERE sessionendtime >= now() - INTERVAL '1' DAY
     AND (deviceid = '<DEVICE_ID>' OR email = '<USER_EMAIL>')
     AND destinationtunnelid != ''
     AND origintlscertificatevalidationresult = 'NONE'
     AND (sni != '' OR detectedprotocol = 'tls' OR originport IN (443, 8443))
     AND connectionclosereason IN ('CLIENT_CLOSED', 'CLIENT_ERROR')
     AND bytesreceived > 0 AND bytesreceived < 15000
     AND bytessent < 5000
   GROUP BY sni, resolvedfqdn, originip, originport, destinationtunnelid, virtualnetworkid, connectionclosereason, origintlscertificatevalidationresult
   ORDER BY aborted_handshakes DESC
   LIMIT 30
   ```
3. **Summarize all session close reasons** (spots systemic `CLIENT_TLS_ERROR`, `ORIGIN_TLS_ERROR`, `ORIGIN_UNREACHABLE`, or rate-limit drops):
   ```sql
   SELECT connectionclosereason, origintlscertificatevalidationresult, count() AS total
   FROM zero_trust_network_sessions
   WHERE sessionendtime >= now() - INTERVAL '4' HOUR
     AND (deviceid = '<DEVICE_ID>' OR email = '<USER_EMAIL>')
   GROUP BY connectionclosereason, origintlscertificatevalidationresult
   ORDER BY total DESC
   ```
4. **Inspect individual sessions & correlate with `gateway_network` / `gateway_http` via `sessionid`**:
   ```sql
   SELECT sessionstarttime, sessionendtime, sessionid, sni, resolvedfqdn, originip, originport, destinationtunnelid, bytesreceived, bytessent, connectionclosereason, origintlscertificatevalidationresult, origintlscertificateissuer
   FROM zero_trust_network_sessions
   WHERE sessionendtime >= now() - INTERVAL '4' HOUR
     AND (deviceid = '<DEVICE_ID>' OR email = '<USER_EMAIL>')
     AND (sni LIKE '%<TARGET_DOMAIN>%' OR originip = '<TARGET_IP>')
   ORDER BY sessionendtime DESC
   LIMIT 30
   ```

**Interpreting the 3 Certificate Failure Modes:**
1. **Inspected TLS — Client Rejects Cloudflare Cert (`CLIENT_TLS_ERROR` + `origintlscertificatevalidationresult = 'VALID'`)**:
   The origin cert is valid, so the **client application rejected Cloudflare's TLS interception certificate**. Common causes: **certificate pinning**, **mTLS**, or a CLI/runtime (`python`, `node`, `java`, `curl`, Docker) not using the OS trust store.
   *Remediation:* Configure the runtime to trust the Cloudflare Root CA (`NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`) or create a Gateway HTTP **Do Not Inspect** (`action = 'off'`) policy for that `sni` / application.
2. **Inspected TLS — Cloudflare Rejects Origin Cert (`ORIGIN_TLS_ERROR` or `origintlscertificatevalidationresult` in `('EXPIRED', 'REVOKED', 'HOSTNAME_MISMATCH', 'UNKNOWN')`)**:
   Gateway attempted TLS inspection, and the **origin server** presented an expired, self-signed, untrusted internal CA, or mismatched certificate (inspect `origintlscertificateissuer`).
   *Remediation:* Renew/fix the origin's certificate, upload your private enterprise CA to Cloudflare Zero Trust, or configure Gateway's **Untrusted Certificates** action (`passThrough` vs `block`).
3. **Bypassed TLS over Cloudflare Tunnel / Access Private App — Client Rejects Internal Server's Expired or Self-Signed Cert (`destinationtunnelid != ''` + `origintlscertificatevalidationresult = 'NONE'` + `CLIENT_CLOSED`/`CLIENT_ERROR` with tiny handshake bytes)**:
   Gateway bypassed TLS decryption (`Do Not Inspect` / `gateway_http.action = 'off'` with `privateappaud`, or private IP L4 pass-through) over a Cloudflare Tunnel. Because TLS ran end-to-end between the user's device and the internal server, Gateway logged `origintlscertificatevalidationresult = 'NONE'` while the **client directly saw and rejected the internal server's expired or self-signed certificate** and closed the socket.
   *(Tip: Resolve `destinationtunnelid` via `cf tunnels get <id>` / `GET /accounts/{id}/cfd_tunnel/{id}` and check Access Private Apps via `cf zero-trust access applications list` / `GET /accounts/{id}/access/apps`.)*
   *Remediation:*
   - Renew the expired cert or install a valid cert matching the internal SAN/hostname on the origin server; **or**
   - Install the internal private CA into the client device's OS/browser/CLI trust store; **or**
   - Expose the app via a Cloudflare Tunnel **Public Hostname** (protected by Cloudflare Access) with `noTLSVerify` on the tunnel ingress rule so clients validate Cloudflare's edge cert instead of the internal self-signed cert.

#### D. Gateway HTTP Inspection (`gateway_http`)
*Timestamp:* `datetime` | *User/Device:* `email`, `userid`, `deviceid`
*(Check `untrustedcertificateaction` for HTTP-layer untrusted cert blocks/pass-throughs, and `action = 'off'` / `privateappaud` for bypassed Access Private Apps.)*
```sql
SELECT datetime, sessionid, action, httphost, httpmethod, httpstatuscode, policyname, privateappaud, url, untrustedcertificateaction
FROM gateway_http
WHERE datetime >= now() - INTERVAL '1' DAY
  AND (email = '<USER_EMAIL>' OR deviceid = '<DEVICE_ID>')
ORDER BY datetime DESC
LIMIT 50
```

#### E. Device Posture Failures (`device_posture_results`)
*Timestamp:* `timestamp` | *User/Device:* `email`, `useruid`, `deviceid`
```sql
SELECT timestamp, posturecheckname, posturechecktype, postureevaluatedresult, clientversion, osversion
FROM device_posture_results
WHERE timestamp >= now() - INTERVAL '1' DAY
  AND (deviceid = '<DEVICE_ID>' OR email = '<USER_EMAIL>')
ORDER BY timestamp DESC
LIMIT 25
```

#### F. Access Application Authentication (`access_requests`)
*Timestamp:* `createdat` | *User/Device:* `email`, `useruid`
```sql
SELECT createdat, action, allowed, appdomain, ipaddress, country
FROM access_requests
WHERE createdat >= now() - INTERVAL '1' DAY
  AND email = '<USER_EMAIL>'
ORDER BY createdat DESC
LIMIT 50
```

#### G. WARP Client Toggles & Profile Changes (`warp_toggle_changes`, `warp_config_changes`)
*Timestamp:* `timestamp` | *User/Device:* `useremail`, `deviceid`
```sql
-- Check if user toggled WARP off/on
SELECT timestamp, hostname, toggled, useremail
FROM warp_toggle_changes
WHERE timestamp >= now() - INTERVAL '1' DAY
  AND (deviceid = '<DEVICE_ID>' OR useremail = '<USER_EMAIL>')
ORDER BY timestamp DESC
LIMIT 25;

-- Check if WARP profile/config switched
SELECT timestamp, hostname, confignamefrom, confignameto
FROM warp_config_changes
WHERE timestamp >= now() - INTERVAL '1' DAY
  AND (deviceid = '<DEVICE_ID>' OR useremail = '<USER_EMAIL>')
ORDER BY timestamp DESC
LIMIT 25;
```

---

## Reference Documentation
- **Local Dataset, Schema & REST API Endpoint Cheat Sheet**: [datasets-reference.md](./references/datasets-reference.md)
- **Cloudflare Client v4 API Fundamentals & Auth**: https://developers.cloudflare.com/fundamentals/api/
- **Cloudflare Log Explorer Documentation**: https://developers.cloudflare.com/logs/log-explorer/
- **Log Explorer SQL Query Guide**: https://developers.cloudflare.com/logs/log-explorer/query-logs/
- **Logpush & Log Explorer Dataset Fields Reference**: https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/
- **Gateway TLS Decryption & Do Not Inspect Docs**: https://developers.cloudflare.com/cloudflare-one/policies/gateway/http-policies/tls-decryption/
- **Cloudflare Analytics SQL API Documentation**: https://developers.cloudflare.com/analytics/sql-api/
- **Cloudflare Zero Trust Documentation**: https://developers.cloudflare.com/cloudflare-one/
