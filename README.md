# Agent Skills Catalog

A curated collection of reusable skills, runbooks, schemas, and helper scripts for AI coding and operations agents. Each skill is structured for **progressive disclosure**—keeping the main instructions concise while providing deep reference documentation and automation scripts on demand.

---

## Available Skills

| Skill | Description | Prerequisites |
| :--- | :--- | :--- |
| **[`cf-zero-trust-troubleshooter`](./cf-zero-trust-troubleshooter/SKILL.md)** | Troubleshoot Cloudflare Zero Trust issues (WARP connectivity, Gateway DNS/HTTP/Network blocks, Network Session TLS/certificate errors, Device Posture failures, Access logins, DEX telemetry, and BISO events) for a specific user or device using **either** the Cloudflare `cf` CLI **or** the Cloudflare Client v4 REST API (`curl`). | • Either Cloudflare `cf` CLI (`cf auth`) **or** `CLOUDFLARE_API_TOKEN` + `curl`<br>• `python3` (for JSON parsing in helper script) |

---

## Skill Details

### 1. Cloudflare Zero Trust Troubleshooter (`cf-zero-trust-troubleshooter`)

Designed to help agents and engineers rapidly investigate user-reported connectivity, certificate/TLS inspection, or security policy issues in Cloudflare Zero Trust—working seamlessly with **either** the `cf` CLI or direct **Cloudflare Client v4 REST API** calls when `cf` is not installed.

- **[`SKILL.md`](./cf-zero-trust-troubleshooter/SKILL.md):** Core troubleshooting playbook supporting both `cf` CLI and `curl` REST API execution, guardrails (account scoping, raw SQL vs. JSON bodies, response envelope unwrapping, lowercase column enforcement, timeout prevention), TLS/certificate failure interpretation (`CLIENT_TLS_ERROR` vs. `ORIGIN_TLS_ERROR` & `origintlscertificatevalidationresult`), and targeted diagnostic queries.
- **[`references/datasets-reference.md`](./cf-zero-trust-troubleshooter/references/datasets-reference.md):** Quick-lookup schema table mapping every Zero Trust Log Explorer dataset (`gateway_dns`, `gateway_http`, `gateway_network`, `zero_trust_network_sessions`, `device_posture_results`, `access_requests`, `warp_toggle_changes`, `warp_config_changes`, `dex_*`, `biso_user_actions`, `audit_logs_v2`), TLS diagnostic enum values, `cf` CLI ↔ Client v4 REST API endpoint mappings, and required API Token permissions.
- **[`scripts/investigate-user.sh`](./cf-zero-trust-troubleshooter/scripts/investigate-user.sh):** Standalone one-command 8-step triage script that auto-detects `cf` CLI or falls back to the Client v4 REST API (`CLOUDFLARE_API_TOKEN`) to query a user's active WARP devices, DNS blocks, L4 network blocks, Network Session TLS/certificate errors, posture evaluations, WARP toggles, HTTP inspection, and Access requests.

---

## Installation & Usage

### Option A: Workspace Installation (Project-Scoped)
Clone this repository directly into your project's `.agents/skills` directory so agents automatically discover the skills when working in that workspace:

```bash
mkdir -p .agents
git clone <repo-url> .agents/skills
```

### Option B: Global Installation (Machine-Wide)
Copy or symlink individual skill folders into your global configuration directory (`~/.gemini/config/skills/`) to make them available across all workspaces:

```bash
mkdir -p ~/.gemini/config/skills
ln -s "$(pwd)/cf-zero-trust-troubleshooter" ~/.gemini/config/skills/cf-zero-trust-troubleshooter
```

### Invoking a Skill
Once installed, agents will automatically activate a skill when your request matches its description, or you can invoke it explicitly via slash command in chat:

```text
/cf-zero-trust-troubleshooter investigate user@example.com for the last 24h
```

---

## Repository Structure & Conventions

Each skill lives in its own top-level directory and follows a standard layout:

```text
<skill-name>/
├── SKILL.md          # Required: YAML frontmatter (name, description) + step-by-step runbook
├── references/       # Optional: Detailed schemas, API cheat sheets, and reference manuals
├── scripts/          # Optional: Executable helper scripts for common multi-step workflows
└── examples/         # Optional: Sample inputs, outputs, or reference configurations
```

### Guidelines for Adding New Skills
1. **Clear Frontmatter:** Always include `name` (lowercase, hyphenated) and a specific third-person `description` in `SKILL.md` stating *what* the skill does and *when* to activate it.
2. **Progressive Disclosure:** Keep `SKILL.md` focused on workflow and guardrails; offload large tables or schemas into `references/` and link to them relatively.
3. **No Secrets or PII:** Use generic placeholders (`user@example.com`, `00000000-0000-0000-0000-000000000000`, `<ACCOUNT_ID>`) in all examples and scripts.
