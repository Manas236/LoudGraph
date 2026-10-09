"""Read-only account readiness, without making API requests during page loads."""
from __future__ import annotations

from . import health
from .config import get_config, path, secret

PLATFORMS = {"youtube": "YouTube", "instagram": "Instagram", "facebook": "Facebook"}
SERVICES = {**PLATFORMS, "telegram": "Telegram", "gemini": "Gemini"}


def enabled_platforms() -> list[str]:
    cfg = get_config()
    return [p for p in PLATFORMS if cfg[p].get("enabled", p == "youtube")]


def test_mode() -> dict:
    """Which turned-on platforms are in test mode (dry_run) and which post for real.
    on: every turned-on platform is in test mode; testing/live/off: platform keys."""
    cfg = get_config()
    enabled = enabled_platforms()
    testing = [p for p in enabled if cfg["dry_run"].get(p, True)]
    live = [p for p in enabled if p not in testing]
    return {"on": bool(enabled) and not live, "testing": testing, "live": live,
            "off": [p for p in PLATFORMS if p not in enabled]}


def account_states() -> dict:
    configured = {
        "youtube": (path("tokens") / "youtube_token.json").exists(),
        "instagram": bool(secret("IG_USER_ID") and secret("IG_ACCESS_TOKEN")),
        "facebook": bool(secret("FB_PAGE_ID") and secret("IG_ACCESS_TOKEN")),
        "telegram": bool(secret("TELEGRAM_BOT_TOKEN") and secret("TELEGRAM_CHAT_ID")),
        "gemini": bool(secret("GEMINI_API_KEY")),
    }
    cached = {c["name"].lower(): c for c in (health._cached(health.K_CREDS) or {}).get("items", [])}
    out = {}
    for key, name in SERVICES.items():
        check = cached.get(key, {})
        state = check.get("state") if configured[key] else "missing"
        connected = configured[key] and state not in ("missing", "expired", "invalid", "error")
        out[key] = {"name": name, "connected": connected, "configured": configured[key],
                    "state": state or "unchecked",
                    "label": "Expired — reconnect" if state in ("expired", "invalid") else
                             "Connected" if connected else "Not connected",
                    "detail": check.get("detail", "")}
    return out


def destinations(run: dict) -> list[str]:
    import json
    chosen = json.loads(run["platforms"]) if run.get("platforms") is not None else enabled_platforms()
    return [p for p in enabled_platforms() if p in chosen]


def ready_destinations(run: dict) -> list[str]:
    states = account_states()
    return [p for p in destinations(run) if states[p]["connected"]]
