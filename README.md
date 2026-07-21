# HR Assistant on Oracle HCM — Platform

An AI HR assistant on top of **Oracle HCM Cloud**, built as the redesign describes:
**two applications on one backend** (see `../HR_Assistant_Redesign_Plan.md`).

- **HR Assistant (End-User App)** — role-adaptive chat + pages for Employees, Managers, HR Admins.
- **Admin Console (Platform App)** — separate, gated, multi-page console for Platform Admins.

Both are served by a single FastAPI backend (`server.py`). End users never see admin;
non-admins hitting `/admin/*` are bounced to the admin sign-in.

## Security model
Each user connects to Oracle HCM **with their own account**, so **Oracle enforces data
access**. The app only **infers a role** (Employee / Manager / HR Admin) from what Oracle
returns and uses it to tailor the UX. It never re-implements permissions and never widens
access. See §9 of the plan.

## Run it

```bash
pip install -r requirements.txt
python server.py
```

- End-User App → http://localhost:8000/
- Admin Console → http://localhost:8000/admin

Out of the box it runs on **Mock data** with an **offline demo brain** (no API key, no
network) so the whole thing works immediately. Sign in as e.g. `jane.doe@example.com`
(Employee), `michael.chen@example.com` (Manager), or `sara.williams@example.com` (HR Admin).

### Go live on Oracle HCM
In **Admin → Data Sources**, add/edit the **Oracle HCM** connector (base URL + auth),
**Test** it, and **Make primary**. In **Admin → Roles & Access**, turn on **per-user Oracle
sign-in** so each user authenticates as themselves. In **Admin → AI Models**, pick a provider
(Google / OpenAI / Anthropic) and paste a key for a real LLM (otherwise the offline brain runs).

## Pages

**End-User** `/login` · `/` (role dashboard) · `/chat` · `/me` · `/team` (mgr/HR) ·
`/directory` · `/reports` (HR) · `/approvals` (mgr/HR) · `/policies`

**Admin** `/admin/login` · `/admin` (dashboard) · `/admin/sources` (+ new/edit) ·
`/admin/tools` (+ detail) · `/admin/models` · `/admin/roles` · `/admin/users` ·
`/admin/cache` · `/admin/audit` · `/admin/settings`

**JSON APIs** `/api/session` · `/api/chat` · `/api/me` · `/api/team` · `/api/directory` ·
`/api/reports` · `/api/admin/{sources,tools,models,roles,users,cache,audit}`

## MCP tools (governed)
Six core tools flow through the connector registry to the primary source, each **checked
against the tool registry (enabled + allowed roles) and written to the audit log**:
`search_workers`, `get_worker`, `get_assignment`, `get_direct_reports`,
`list_by_department`, `get_management_chain`. Two more ship **disabled** to demonstrate
governance/extension: `get_compensation` (HR-only) and `snow_raise_case` (ServiceNow,
namespaced). Add tools/sources from the Admin Console (config over code).

## File map
| File | Role |
|---|---|
| `server.py` | FastAPI backend — both apps, all routes + JSON APIs, sessions, admin gate |
| `webui.py` | Server-rendered UI (shell, role nav, tables, cards) |
| `agent.py` | Pydantic AI agent — model router + role-filtered toolset |
| `hcm_mcp_server.py` | In-process MCP server — governed + audited tools |
| `tools_registry.py` | Tool registry & governance (§8.4) |
| `sources.py` | Central dispatch to the primary connector |
| `connectors.py` / `conn_types.py` | Connector registry + type catalog |
| `hcm_client.py` | Oracle HCM REST client (per-user auth, caching) |
| `mock_data.py` | 9-person demo directory |
| `identity.py` / `session_ctx.py` | Sign-in → role inference; per-request user + auth |
| `role_rules.py` | Roles/capabilities + field policy (§6/§A5) |
| `users_store.py` | Users & sessions (§A6) |
| `audit_store.py` | Audit log + CSV export (§A8) |
| `config.py` / `cache.py` / `models.py` | Settings (SQLite) · TTL cache · model-list fetch |
| `app.py` | Legacy Chainlit single-page chat (superseded by `server.py`) |

## Notes
- Storage is **SQLite** (`hcm_agent.db`); use Postgres in production.
- The **offline demo brain** (`demo_model.py`) is a scripted stand-in so the pipeline runs
  with no LLM key. A real provider replaces it transparently.
- Production hardening (SSO/OBO, secrets vault, Postgres, SIEM audit) is the plan's P6.

## Operations & security
- Health: `/healthz` (liveness), `/readyz` (readiness — DB + primary connector).
- Binds `127.0.0.1` by default (`bind_host`); override with `HOST`/`PORT` env or Admin → Settings.
- Admin sign-in needs a password (set in Admin → Settings) **or** a local connection; rate-limited with lockout; CSRF tokens on every form; security headers + request IDs on every response.
- Secrets via `.env` (see `.env.example`) — never commit real secrets; `config.json`/`*.db` are git-ignored.
- Container: `docker compose up --build`.  CI runs the full test suite (`.github/workflows/ci.yml`).
