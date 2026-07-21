"""Fetch the live list of models for a provider (auto-updates from the provider API)."""
from __future__ import annotations
import httpx

TIMEOUT = httpx.Timeout(12.0, connect=6.0)


async def list_models(provider: str, key: str) -> dict:
    provider = (provider or "").lower()
    if provider == "demo":
        return {"ok": True, "models": []}
    if not key:
        return {"ok": False, "error": "Enter and Save an API key first, then load models."}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            if provider == "google":
                r = await c.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": key, "pageSize": 200})
                r.raise_for_status()
                ms = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                      if "generateContent" in (m.get("supportedGenerationMethods") or [])]
            elif provider == "openai":
                r = await c.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"})
                r.raise_for_status()
                ms = sorted(m["id"] for m in r.json().get("data", [])
                            if any(m["id"].startswith(p) for p in ("gpt-", "o1", "o3", "o4", "chatgpt")))
            elif provider == "anthropic":
                r = await c.get("https://api.anthropic.com/v1/models",
                                headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
                r.raise_for_status()
                ms = [m["id"] for m in r.json().get("data", [])]
            else:
                return {"ok": False, "error": f"Unknown provider '{provider}'."}
        return {"ok": True, "models": ms}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"{e.response.status_code} from {provider} API — check the API key."}
    except Exception as e:
        msg = str(e)
        if "CERTIFICATE" in msg.upper() or "SSL" in msg.upper():
            msg = "SSL verification failed (corporate proxy/CA?). " + msg
        return {"ok": False, "error": f"{type(e).__name__}: {msg}"}
