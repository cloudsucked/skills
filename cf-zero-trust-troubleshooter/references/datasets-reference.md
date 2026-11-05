# Cloudflare Log Explorer & Analytics SQL Reference for Zero Trust

This reference maps all Cloudflare Zero Trust datasets in Log Explorer (`cf logs query`), their exact lowercase column names, timestamp fields, user/device identifiers, and official documentation URLs.

---

## 1. Zero Trust Datasets Quick Lookup Table

> [!IMPORTANT]
> Every Log Explorer query **must** include a `WHERE <timestamp_field> >= ...` filter using the exact timestamp column listed below, and all column names must be **lowercase**.

| Dataset Name | Scope | Timestamp Column | User Email Column | User ID Column | Device ID Column | Primary Use Case |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `gateway_dns` | Account | `datetime` | `email` | `userid` | `deviceid` | DNS queries, blocked domains, resolver decisions, DNS policy matches |
| `gateway_http` | Account | `datetime` | `email` | `userid` | `deviceid` | Inline HTTP proxy inspection, URL/host blocks, DLP matches, TLS/cert errors |
| `gateway_network` | Account | `datetime` | `email` | `userid` | `deviceid` | L4 firewall (TCP/UDP), SNI, destination IP/port blocks, virtual networks |
| `zero_trust_network_sessions` | Account | `sessionendtime` | `email` | `userid` | `deviceid` | Full network session lifecycles, bytes sent/received, close reasons |
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

## 2. Detailed Dataset Columns (Enabled by Default)

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
- **Policy, DLP & Security:** `action`, `policyid`, `policyname`, `categorynames`, `applicationnames`, `isisolated`, `untrustedcertificateaction`, `blockedfilename`, `blockedfilereason`, `downloadmatcheddlpprofiles`, `uploadmatcheddlpprofiles`
- **Docs:** https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/gateway_http/

### `gateway_network`
- **Timestamp:** `datetime`
- **Identity & Device:** `email`, `userid`, `deviceid`, `devicename`, `registrationid`, `sessionid`, `sourceip`, `sourceinternalip`, `sourceport`
- **Connection Details:** `destinationip`, `destinationport`, `sni`, `transport`, `transportprotocol`, `detectedprotocol`, `virtualnetworkid`, `virtualnetworkname`
- **Policy & Decision:** `action`, `policyid`, `policyname`, `applicationnames`, `categorynames`
- **Docs:** https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/gateway_network/

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

### `zero_trust_network_sessions`
- **Timestamp:** `sessionendtime` (also has `sessionstarttime`)
- **Identity & Device:** `email`, `userid`, `deviceid`, `devicename`, `sourceip`, `sourceinternalip`, `sourceport`
- **Session Details:** `sessionid`, `destinationip`, `destinationport`, `originip`, `originport`, `protocol`, `sni`, `virtualnetworkid`, `bytesreceived`, `bytessent`, `packetsreceived`, `packetssent`, `connectionclosereason`
- **Docs:** https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/account/zero_trust_network_sessions/

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

## 3. Useful `cf zero-trust` CLI Commands (Live State Inspection)

Combine historical Log Explorer queries with real-time state from `cf zero-trust`:

| Command | Description |
| :--- | :--- |
| `cf zero-trust devices list --last-seen-user-email <email>` | List WARP devices for a user (returns `id`, `model`, `os_version`, `client_version`, `public_ip`, `last_seen_at`) |
| `cf zero-trust devices get <device-id>` | Get full details of a specific WARP device |
| `cf zero-trust devices registrations list` | List active WARP client registrations |
| `cf zero-trust devices posture list` | List configured Device Posture rules on the account |
| `cf zero-trust devices profiles list` | List WARP device settings profiles (split tunnels, local domain fallback, mode) |
| `cf zero-trust gateway rules list` | List configured Gateway DNS, HTTP, and Network policies/rules |
| `cf zero-trust gateway categories list` | List Gateway content & security categories (maps `categoryids` to names) |
| `cf zero-trust users list` | List Zero Trust users and seat statuses |
| `cf zero-trust risk-scoring get <user-id>` | Retrieve a user's Zero Trust risk score |
| `cf zero-trust dex` | Inspect Digital Experience Monitoring tests and fleet metrics |

---

## 4. Log Explorer SQL (`cf logs query`) vs. Analytics SQL (`cf analytics sql`)

| Aspect | Log Explorer SQL (`cf logs query`) | Analytics SQL (`cf analytics sql post`) |
| :--- | :--- | :--- |
| **CLI Command** | `cf logs query --body "<RAW_SQL>"` | `cf analytics sql post --body '{"query":"<SQL>","scope-account-tag":"<ID>"}'` |
| **API Path** | `POST /accounts/{id}/logs/explorer/query/sql` | `POST /analytics/sql` |
| **Schema Discovery** | `cf logs datasets available`<br>`cf logs datasets get <dataset-id>` | `cf analytics sql introspection get` |
| **SQL Dialect** | Apache DataFusion / Log Explorer SQL<br>• `INTERVAL '1' DAY` / `INTERVAL '4' HOUR`<br>• `count()` | ClickHouse-style SQL<br>• Supports `FORMAT JSON`, `FORMAT TabSeparated`<br>• Namespaced tables (`events.httpRequests`, `logs.workersLogs`) |
| **Docs** | https://developers.cloudflare.com/logs/log-explorer/query-logs/ | https://developers.cloudflare.com/analytics/sql-api/ |
