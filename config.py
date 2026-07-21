"""Runtime configuration — SQLite backend (hcm_agent.db). Editable at /admin.

Holds AI model settings, Oracle HCM connection (Basic or OAuth2), data source,
and cache settings. Secrets are local; use a vault in production.
"""
from __future__ import annotations
import json, os, sqlite3
from pathlib import Path

try:                                  # load a local .env if present (keeps secrets off the CLI/DB)
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("HCM_DB_PATH") or (HERE / "hcm_agent.db"))   # overridable for containers
LEGACY_JSON = HERE / "config.json"

DEFAULTS = {
    # --- AI model ---
    "ai_provider": "demo",           # demo | google | openai | anthropic
    "ai_model": "",
    "ai_api_key": "",
    # --- data source ---
    "data_source": "mock",           # mock | live
    # --- Oracle HCM connection ---
    "hcm_base_url": "",
    "hcm_auth": "basic",             # basic | oauth
    "hcm_username": "",              # basic
    "hcm_password": "",              # basic
    "hcm_client_id": "",            # oauth
    "hcm_client_secret": "",        # oauth
    "hcm_scope": "",                # oauth (optional)
    "hcm_token_url": "",            # oauth token endpoint
    # --- cache ---
    "cache_enabled": "true",         # "true" | "false"
    "cache_ttl": "300",             # seconds
    # --- roles / access ---
    "require_login": "true",         # "true" | "false" (ask who is using the chat)
    "hr_admin_emails": "",          # comma-separated emails / employee numbers — forces HR Admin (override / fallback)
    "per_user_auth": "false",        # "true" -> each user signs in with THEIR OWN Oracle account (Oracle enforces security)
    "hr_visibility_threshold": "1",  # min # of non-self, non-report workers whose SECURED data is visible to imply HR-level access
    # --- admin console / platform ---
    "platform_admin_emails": "",     # comma-separated — who may open the Admin Console
    "admin_password_hash": "",       # sha256(salt+password); if set, admin login requires the password
    "admin_salt": "",               # per-install salt for the admin password hash
    "bind_host": "127.0.0.1",        # interface uvicorn binds to (loopback by default)
    "cookie_secure": "false",        # set "true" behind HTTPS so the session cookie is Secure
    "app_name": "HR Assistant",      # branding
    "environment": "prototype",      # prototype | dev | prod (shown in admin)
    "oidc_issuer": "",              # SSO/OIDC (production) — displayed in Settings
    "oidc_client_id": "",
}
DEFAULT_MODELS = {"google": "gemini-2.0-flash", "openai": "gpt-4o", "anthropic": "claude-3-5-sonnet-latest", "demo": ""}
_SECRET_KEYS = ("ai_api_key", "hcm_password", "hcm_client_secret")


def tune(conn: sqlite3.Connection) -> sqlite3.Connection:
    """WAL + busy timeout so concurrent readers/writers don't hit 'database is locked'."""
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    return conn


def _connect() -> sqlite3.Connection:
    conn = tune(sqlite3.connect(DB_PATH, timeout=5))
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()  # persist the schema immediately (DDL isn't auto-committed)
    return conn


def _read_all(conn) -> dict:
    return {k: v for k, v in conn.execute("SELECT key, value FROM settings")}


def _write(conn, cfg: dict) -> None:
    for k in DEFAULTS:
        v = cfg.get(k, "")
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (k, "" if v is None else str(v)))
    conn.commit()


def _migrate(conn) -> None:
    if _read_all(conn):
        return
    if LEGACY_JSON.exists():
        try:
            legacy = json.loads(LEGACY_JSON.read_text(encoding="utf-8"))
            _write(conn, {**DEFAULTS, **{k: v for k, v in legacy.items() if k in DEFAULTS}})
        except Exception:
            pass


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        conn = _connect(); _migrate(conn)
        for k, v in _read_all(conn).items():
            if k in DEFAULTS:
                cfg[k] = v
        conn.close()
    except Exception:
        pass
    if not cfg.get("ai_api_key"):
        cfg["ai_api_key"] = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or ""
    if not cfg.get("hcm_base_url"):
        cfg["hcm_base_url"] = os.getenv("HCM_BASE_URL", "")
    return cfg


def save(updates: dict) -> dict:
    cfg = load()
    for k, v in (updates or {}).items():
        if k not in DEFAULTS:
            continue
        if k in _SECRET_KEYS and (v in ("", None) or set(str(v)) == {"*"}):
            continue
        cfg[k] = v
    conn = _connect(); _write(conn, cfg); conn.close()
    return cfg


def masked() -> dict:
    cfg = load(); out = dict(cfg)
    for k in _SECRET_KEYS:
        out[k] = "********" if cfg.get(k) else ""
    return out


def default_model(provider: str) -> str:
    return DEFAULT_MODELS.get(provider, "")


def as_bool(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def as_int(v, default=300) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default
