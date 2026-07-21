# Tech stack

**Architecture:** one FastAPI backend serving two apps — the **HR Assistant** (end-user,
role-adaptive) and the **Admin Console** (platform admins) — see `HR_Assistant_Redesign_Plan.md`.

| Layer | Choice | Notes |
|---|---|---|
| Language / runtime | Python 3.10+ | |
| Web framework | **FastAPI + Uvicorn** | `server.py`; server-rendered UI in `webui.py` (no Node build) |
| AI agent | **Pydantic AI** (model-agnostic) | Google / OpenAI / Anthropic, or an offline demo brain (`demo_model.py`) |
| Tools | **FastMCP** (in-process MCP) | governed + audited; config-driven tools registered dynamically |
| Data source | **Oracle HCM Cloud REST** | via `hcm_client.py` (Basic/OAuth2, per-user auth); Mock connector for demo |
| Storage | **SQLite** (WAL) | `config`, `connectors`, `tools`, `users`, `role_rules`, `audit` — Postgres for prod |
| HTTP client | httpx (+ truststore for corporate CA) | |
| Security | session cookie + CSRF tokens, admin password gate, rate limiting, security headers, per-user Oracle auth | see `CODE_REVIEW.md` |

**Run:** `pip install -r requirements.txt` then `python server.py`
(End-User `http://localhost:8000/`, Admin `http://localhost:8000/admin`; health at `/healthz`, readiness at `/readyz`).
Container: `docker compose up --build`.

**Not used anymore:** Chainlit (the earlier single-page UI, `app.py`/`admin.py`, now retired).
