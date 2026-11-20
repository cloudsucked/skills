# Cloudflare Log Explorer & Analytics SQL Reference for Zero Trust

This reference maps all Cloudflare Zero Trust datasets in Log Explorer (`cf logs query`), their exact lowercase column names, timestamp fields, user/device identifiers, TLS/certificate diagnostic values, and official documentation URLs.

---

## 1. Zero Trust Datasets Quick Lookup Table

> [!IMPORTANT]
> Every Log Explorer query **must** include a `WHERE <timestamp_field> >= ...` filter using the exact timestamp column listed below, and all column names must be **lowercase**.

| Dataset Name | Scope | Timestamp Column | User Email Column | User ID Column | Device ID Column | Primary Use Case |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `gateway_dns` | Account | `datetime` | `email` | `userid` | `deviceid` | DNS queries, blocked domains, resolver decisions, DNS policy matches |
| `gateway_http` | Account | `datetime` | `email` | `userid` | `deviceid` | Inline HTTP proxy inspection, URL/host blocks, DLP matches, `untrustedcertificateaction` |
| `gateway_network` | Account | `datetime` | `email` | `userid` | `deviceid` | L4 firewall (TCP/UDP), SNI routing, destination IP/port blocks, `sessionid` correlation |
| `zero_trust_network_sessions` | Account | `sessionendtime` | `email` | `userid` | `deviceid` | TCP/UDP & TLS session outcomes, client/origin cert & handshake errors, close reasons |
| `access_requests` | Account | `createdat` | `email` | `useruid` | *(none)* | Cloudflare Access app logins, allow/deny decisions, IdP/country checks |
| `device_posture_results` | Account | `timestamp` | `email` | `useruid` | `deviceid` | Periodic WARP posture evaluations (`true`/`false`), OS/file/service checks |
| `warp_toggle_changes` | Account | `timestamp` | `useremail` | *(none)* | `deviceid` | User turning the WARP client switch on or off (`toggled`) |
| `warp_config_changes` | Account | `timestamp` | `useremail` | *(none)* | `deviceid` | Device switching between WARP profiles/configs (`confignamefrom`/`to`) |
| `dex_device_state_events` | Account | `timestamp` | *(none)* | *(none)* | `deviceid` | Digital Experience (DEX) device health, ISP ASN/IP, CPU/RAM usage |
| `dex_application_tests` | Account | `timestamp` | `useremail` | `userid` | `deviceid` | DEX HTTP and Traceroute synthetic test results from user devices |
| `biso_user_actions` | Account | `timestamp` | `useremail` | `userid` | *(none)* | Browser Isolation (BISO) copy/paste, download, print, upload decisions |
| `casb_findings` | Account | `detectedtimestamp` | *(none)* | *(none)* | *(none)* | SaaS security posture findings via Cloudflare CASB |
| `audit_logs_v2` | Account | `actiontimestamp` | `actoremail` | `actorid` | *(none)* | Admin/user configuration and control-plane audit trail |

---

## 2. Detailed Dataset Columns (Verified Against Schema)

### `gateway_dns`
- **Timestamp:** `datetime`
- **Identity & Device:** `email`, `userid`, `deviceid`, `devicename`, `registrationid`, `srcip`, `srcport`, `location`, `locationid`
- **Query Details:** `queryname`, `querytypename`, `querytype`, `rcode`, `rdata`, `resolvedips`, `cnames`, `protocol`
- **Policy & Decision:** `resolverdecision` (e.g. `allowedOnNoPolicyMatch`, `allowedRule`, `blockedRule`), `policyid`, `policyname`, `matchedcategorynames`, `querycategorynames`, `applicationname`
- **Docs:** https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/gateway_dns/

### `gateway_http`
- **Timestamp:** `datetime`
- **Identity & Device:** `email`, `userid`, `deviceid`, `devicename`, `registrationid`, `sessionid`, `sourceip`, `sourceinternalip`, `useragent`
- **Request Details:** `httphost`, `httpmethod`, `httpstatuscode`, `httpversion`, `url`, `referer`, `destinationip`, `destinationport`
- **Policy, Certs, DLP & Security:** `action`, `policyid`, `policyname`, `categorynames`, `applicationnames`, `isisolated`, `untrustedcertificateaction`, `blockedfilename`, `blockedfilereason`, `downloadmatcheddlpprofiles`, `uploadmatcheddlpprofiles`
- **Docs:** https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/gateway_http/

### `gateway_network`
- **Timestamp:** `datetime`
- **Identity & Device:** `email`, `userid`, `deviceid`, `devicename`, `registrationid`, `sessionid`, `sourceip`, `sourceinternalip`, `sourceport`
- **Connection Details:** `destinationip`, `destinationport`, `overrideip`, `overrideport`, `sni`, `transport`, `transportprotocol`, `detectedprotocol`, `virtualnetworkid`, `virtualnetworkname`, `proxyendpoint`
- **Policy & Decision:** `action`, `policyid`, `policyname`, `applicationids`, `applicationnames`, `categoryids`, `categorynames`
- **Docs:** https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/gateway_network/

### `zero_trust_network_sessions`
> [!WARNING]
> Unlike `gateway_network` (which uses `destinationip` and `destinationport`), `zero_trust_network_sessions` uses `originip` and `originport` (plus `initialoriginip`, `egressip`, `egressport`, `sni`, and `resolvedfqdn`).

- **Timestamp:** `sessionendtime` (also has `sessionstarttime`)
- **Identity & Device:** `email`, `userid`, `deviceid`, `devicename`, `registrationid`, `sourceid`, `sourcename`, `sourceip`, `sourceinternalip`, `sourceport`
- **Target & Routing:** `sessionid`, `sni`, `resolvedfqdn`, `originip`, `originport`, `initialoriginip`, `egressip`, `egressport`, `egresscoloname`, `ingresscoloname`, `egressruleid`, `egressrulename`, `protocol`, `detectedprotocol`, `onramptype`, `offramp`, `virtualnetworkid`, `destinationtunnelid`
- **Traffic & Lifecycle:** `bytesreceived`, `bytessent`, `connectionclosereason`, `connectionreuse`, `clienttcphandshakedurationms`, `ruleevaluationdurationms`
- **TLS & Certificate Diagnostics:**
  - **Origin TLS:** `origintlscertificatevalidationresult`, `origintlscertificateissuer`, `origintlsversion`, `origintlscipher`, `origintlshandshakedurationms`
  - **Client TLS:** `clienttlsversion`, `clienttlscipher`, `clienttlshandshakedurationms`
- **Docs:** https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/zero_trust_network_sessions/

### `device_posture_results`
- **Timestamp:** `timestamp`
- **Identity & Device:** `email`, `useruid`, `deviceid`, `devicename`, `devicemodel`, `devicemanufacturer`, `deviceserialnumber`, `devicetype`, `osversion`, `clientversion`, `registrationid`
- **Posture Details:** `posturecheckname`, `posturechecktype`, `postureevaluatedresult` (`true`/`false`), `policyid`, `postureexpectedjson`, `posturereceivedjson`
- **Docs:** https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/device_posture_results/

### `access_requests`
- **Timestamp:** `createdat`
- **Identity & Context:** `email`, `useruid`, `ipaddress`, `country`, `rayid`
- **Access Decision:** `action`, `allowed` (`true`/`false`), `appdomain`, `appuuid`, `connection`, `purposejustificationprompt`, `purposejustificationresponse`
- **Docs:** https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/access_requests/

### `warp_toggle_changes` & `warp_config_changes`
- **Timestamp:** `timestamp`
- **Identity & Device:** `useremail`, `deviceid`, `deviceregistrationid`, `hostname`, `serialnumber`
- **Change Details:**
  - `warp_toggle_changes`: `toggled` (`true` = enabled, `false` = disabled)
  - `warp_config_changes`: `confignamefrom`, `confignameto`, `accountidfrom`, `accountidto`
- **Docs:**
  - https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/warp_toggle_changes/
  - https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/warp_config_changes/

### `dex_device_state_events` & `dex_application_tests`
- **Timestamp:** `timestamp`
- **Identity & Device:** `deviceid`, `deviceregistrationid` (plus `useremail`, `userid` on `dex_application_tests`)
- **DEX Details:**
  - `dex_device_state_events`: `status`, `connectionprotocol`, `mode`, `colo`, `deviceipv4address`, `ispipv4address`, `ispipv4aso`, `cpupercentagebyapp`, `ramusedpercentagebyapp`, `wifiessid`, `wifirssidbm`
  - `dex_application_tests`: `testId`, `testname`, `httpstatuscode`, `httptotaltime`, `httpdnstime`, `traceroutestatus`, `traceroutedestinationhostname`, `traceroutehops`
- **Docs:**
  - https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/dex_device_state_events/
  - https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/dex_application_tests/

---

## 3. TLS & Certificate Diagnostic Guide (`zero_trust_network_sessions` + `gateway_network`)

### Cross-Dataset Correlation via `sessionid`
`zero_trust_network_sessions`, `gateway_network`, and `gateway_http` all share a common **`sessionid`**:
1. **`gateway_network`** records the L4 policy match (`action`, `policyname`, `sni`, `destinationip`, `destinationport`, `applicationnames`).
2. **`zero_trust_network_sessions`** records what happened during the TCP & TLS handshakes (`connectionclosereason`, `origintlscertificatevalidationresult`, `origintlscertificateissuer`).
3. **`gateway_http`** only records rows **after** both TLS handshakes succeed and an HTTP request is parsed (unless HTTP **Do Not Inspect** / `off` bypassed decryption). If a cert check fails during the handshake, `gateway_http` will have **zero rows** for that session.

### `origintlscertificatevalidationresult` Values (`zero_trust_network_sessions`)
- `VALID` — Origin server's certificate is valid and trusted by Cloudflare.
- `EXPIRED` — Origin certificate's validity window has expired.
- `REVOKED` — Origin certificate was revoked.
- `HOSTNAME_MISMATCH` — Origin certificate SAN/CN does not match the requested SNI/hostname.
- `NONE` — No TLS was terminated by Cloudflare on the session—either plain TCP/UDP **or Gateway bypassed TLS decryption** (`Do Not Inspect` / `action = 'off'` or private-IP L4 pass-through).
  - **Subtle Cert Failure Pattern (Bypassed Tunnel / Access Private App):** When `destinationtunnelid != ''` and `origintlscertificatevalidationresult = 'NONE'` on a TLS session (`sni != ''` or `detectedprotocol = 'tls'` or `originport IN (443, 8443)`), TLS runs end-to-end between the client and the internal server. If the internal server presents an **expired or self-signed cert** that the client does not trust, Gateway never sees a TLS error code—instead, the session logs `connectionclosereason = 'CLIENT_CLOSED'` (or `'CLIENT_ERROR'`) after exchanging only handshake-sized byte counts (`bytesreceived` ~500–15,000 B, `bytessent` < 5,000 B).
- `UNKNOWN` — Validation could not be completed (commonly self-signed or private/untrusted internal CA when TLS inspection *is* active; check `origintlscertificateissuer`).

### `connectionclosereason` Values (`zero_trust_network_sessions`)
- **`CLIENT_TLS_ERROR`** — Client aborted or failed the TLS handshake with Cloudflare Gateway (only logged when Gateway terminates TLS). When paired with `origintlscertificatevalidationresult = 'VALID'`, this almost always indicates **certificate pinning**, **mTLS**, or a **missing Cloudflare Root CA** in the client's runtime trust store (`git`, `npm`, `python`, `curl`, Java, Docker).
- **`ORIGIN_TLS_ERROR`** — TLS handshake failed between Cloudflare Gateway and the origin server (untrusted/self-signed/expired cert, unsupported cipher/TLS version, or origin requiring client mTLS cert when Gateway terminates TLS).
- **`CLIENT_CLOSED` / `ORIGIN_CLOSED`** — Normal TCP FIN/close initiated by client or origin. *(Note: On TLS-bypassed Cloudflare Tunnel sessions where `origintlscertificatevalidationresult = 'NONE'`, repeated `CLIENT_CLOSED` with <15 KB `bytesreceived` indicates the client rejected the internal origin's expired/self-signed certificate during the end-to-end handshake.)*
- **`CLIENT_IDLE_TIMEOUT`** — Session closed due to inactivity.
- **`CLIENT_ERROR` / `ORIGIN_ERROR`** — General TCP/protocol reset or error on client or origin side.
- **`ORIGIN_UNREACHABLE` / `ORIGIN_UNROUTABLE` / `PROXY_CONN_REFUSED`** — Routing or connectivity failure reaching the origin (e.g., down service, cloudflared tunnel offline, firewall drop).
- **`TOO_MANY_ACTIVE_SESSIONS_FOR_USER` / `TOO_MANY_NEW_SESSIONS_FOR_USER`** (and `_FOR_ACCOUNT`) — Session concurrency or rate limit exceeded.

### Certificate Troubleshooting Docs
- **TLS Decryption & Inspection Limitations:** https://developers.cloudflare.com/cloudflare-one/policies/gateway/http-policies/tls-decryption/
- **Install Cloudflare Root CA on Devices & Runtimes:** https://developers.cloudflare.com/cloudflare-one/connections/connect-devices/user-side-certificates/

---

## 4. `cf` CLI ↔ Cloudflare Client v4 REST API Mapping

Base URL for all REST endpoints below: `https://api.cloudflare.com/client/v4`

### Authentication & Required API Token Permissions
- **Bearer Token (Preferred):** `-H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"`
  - `Account` → `Logs` → `Read` *(or `Log Explorer` → `Read`)*
  - `Account` → `Zero Trust` → `Read`
  - `Account` → `Account Settings` → `Read` *(required for `GET /accounts` discovery)*
- **Legacy Global API Key:** `-H "X-Auth-Email: $CLOUDFLARE_EMAIL" -H "X-Auth-Key: $CLOUDFLARE_API_KEY"`
- **Response Format:** Direct REST calls return `{"result": [...], "success": true, "errors": [], "messages": []}` (whereas `cf` CLI outputs `.result` directly).

### Endpoint Mapping Table

| Operation | `cf` CLI Command | Client v4 REST API Method & Path |
| :--- | :--- | :--- |
| **List Accounts** | `cf accounts list` | `GET /accounts` |
| **List User's WARP Devices** | `cf zero-trust devices list --last-seen-user-email <email>` | `GET /accounts/{account_id}/devices/physical-devices?last_seen_user.email={url_encoded_email}` |
| **Get Device Details** | `cf zero-trust devices get <device-id>` | `GET /accounts/{account_id}/devices/physical-devices/{device_id}` |
| **List Active Registrations** | `cf zero-trust devices registrations list` | `GET /accounts/{account_id}/devices/registrations` |
| **List Posture Rules** | `cf zero-trust devices posture rules list` | `GET /accounts/{account_id}/devices/posture` |
| **Get Default WARP Profile** | `cf zero-trust devices profiles default get` | `GET /accounts/{account_id}/devices/policy` |
| **List Custom WARP Profiles** | `cf zero-trust devices profiles custom list` | `GET /accounts/{account_id}/devices/policies` |
| **Get Gateway TLS/Global Config** | `cf zero-trust gateway configurations get` | `GET /accounts/{account_id}/gateway/configuration` |
| **List Gateway Root Certificates** | `cf zero-trust gateway certificates list` | `GET /accounts/{account_id}/gateway/certificates` |
| **List Gateway Policies/Rules** | `cf zero-trust gateway rules list` | `GET /accounts/{account_id}/gateway/rules` |
| **List Gateway Categories** | `cf zero-trust gateway categories list` | `GET /accounts/{account_id}/gateway/categories` |
| **List Cloudflare Tunnels** | `cf tunnels list` | `GET /accounts/{account_id}/cfd_tunnel` |
| **Get Tunnel Details (`destinationtunnelid`)** | `cf tunnels get <tunnel-id>` | `GET /accounts/{account_id}/cfd_tunnel/{tunnel_id}` |
| **List Access Apps (incl. Private Apps)** | `cf zero-trust access applications list` | `GET /accounts/{account_id}/access/apps` |
| **List Zero Trust Access Users** | `cf zero-trust access users list` | `GET /accounts/{account_id}/access/users` |
| **Get User Risk Score** | `cf zero-trust risk-scoring get <user-id>` | `GET /accounts/{account_id}/zt_risk_scoring/{user_id}` |
| **List Enabled Log Datasets** | `cf logs datasets list` | `GET /accounts/{account_id}/logs/explorer/datasets` |
| **Get Dataset Enabled Fields** | `cf logs datasets get <dataset-id>` | `GET /accounts/{account_id}/logs/explorer/datasets/{dataset_id}` |
| **List All Dataset Schemas** | `cf logs datasets available` | `GET /accounts/{account_id}/logs/explorer/datasets/available` |
| **Run Log Explorer SQL Query** | `cf logs query --body "<RAW_SQL>"` | `POST /accounts/{account_id}/logs/explorer/query/sql` *(raw SQL body, `Content-Type: text/plain`)* |
| **Run Analytics SQL Query** | `cf analytics sql post --body '<JSON>'` | `POST /analytics/sql` *(JSON body, `Content-Type: application/json`)* |
| **Inspect Analytics SQL Tables** | `cf analytics sql introspection get` | `GET /analytics/sql/introspection` |

---

## 5. Log Explorer SQL vs. Analytics SQL (CLI & REST API)

| Aspect | Log Explorer SQL | Analytics SQL |
| :--- | :--- | :--- |
| **`cf` CLI Command** | `cf logs query --body "<RAW_SQL>"` | `cf analytics sql post --body '{"query":"<SQL>","scope-account-tag":"<ID>"}'` |
| **Client v4 REST Endpoint** | `POST /client/v4/accounts/{id}/logs/explorer/query/sql` | `POST /client/v4/analytics/sql` |
| **REST Request Body** | **Raw SQL string** (`-H "Content-Type: text/plain" --data-raw "<SQL>"`) | **JSON object** (`-H "Content-Type: application/json" --data-raw '{"query":"...","scope-account-tag":"..."}'`) |
| **Schema Discovery** | `cf logs datasets available` or `GET /accounts/{id}/logs/explorer/datasets/available` | `cf analytics sql introspection get` or `GET /analytics/sql/introspection` |
| **SQL Dialect** | Apache DataFusion / Log Explorer SQL<br>• `INTERVAL '1' DAY` / `INTERVAL '4' HOUR`<br>• `count()` | ClickHouse-style SQL<br>• Supports `FORMAT JSON`, `FORMAT TabSeparated`<br>• Namespaced tables (`events.httpRequests`, `logs.workersLogs`) |
| **Docs** | https://developers.cloudflare.com/logs/log-explorer/query-logs/ | https://developers.cloudflare.com/analytics/sql-api/ |
