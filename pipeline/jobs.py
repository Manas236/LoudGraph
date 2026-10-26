"""The autopilot's four jobs, as `python -m pipeline <job>` runs them (pipeline/__main__.py), each inside
autopilot.run_job (the MIGRATED_TO_SERVER refusal, the shared job lock, an alert on a crash):

  generate   03:00 IST  generator.generate: count pool losses, top the pool up to autopilot.pool_target
  post 1|2   14:30 / 16:00 IST  autopick.post: pick one reel by the breakout model and post it
  insights   every 6 h  analytics.collect (every platform; Instagram snapshots at ~24 h and ~72 h), then the alert
                        when Instagram insights have failed for autopilot.insights_fail_hours (2 days)
  health     09:00 IST  the Meta token (debug_token: invalid, or expiring within 7 days), free disk, the pool, insights,
                        and the media of reels posted over 14 days ago is deleted
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import alerts, db
from .autopilot import settings as ap_settings
from .config import secret

log = logging.getLogger(__name__)
K_INSIGHTS_OK, K_INSIGHTS_SINCE = "insights:last_ok", "insights:watch_since"


# ------------------------------------------------------------------ insights

def insights(now: datetime | None = None) -> int:
    from . import analytics
    now = now or datetime.now(timezone.utc)
    out = analytics.collect()
    if "skipped" in out:
        return 0
    ig = out.get("instagram")
    expected = len(analytics._live_posts("instagram"))
    ok = isinstance(ig, int) and (ig > 0 or expected == 0) and bool(secret("IG_ACCESS_TOKEN") or expected == 0)
    log.info("insights: %s (Instagram %s of %d posts)", out, ig, expected)
    if ok:
        db.kv_set(K_INSIGHTS_OK, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    insights_check(now)
    return 0


def insights_check(now: datetime | None = None) -> bool:
    """Alert when Instagram insights have not worked for autopilot.insights_fail_hours. True when healthy."""
    now = now or datetime.now(timezone.utc)
    hours = float(ap_settings().get("insights_fail_hours", 48))
    last = db.kv_get(K_INSIGHTS_OK)
    since = db.kv_get(K_INSIGHTS_SINCE)
    if not since:
        since = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        db.kv_set(K_INSIGHTS_SINCE, since)
    ref = db.parse_ts(last) if last else db.parse_ts(since)
    if now - ref >= timedelta(hours=hours):
        alerts.fire("insights", f"Instagram insights have failed for {(now - ref).total_seconds() / 3600:.0f} h "
                                f"(the picker learns from them)",
                    "the Meta token and the instagram_manage_insights permission (python -m pipeline health)")
        return False
    if last:
        alerts.clear("insights", "Instagram insights work again")
    return True


# ------------------------------------------------------------------ health

def check_token(now: datetime | None = None) -> bool:
    """The Meta token (IG_ACCESS_TOKEN, also the Page token) by debug_token: alert when it is invalid or it (or its
    data access) expires within autopilot.token_warn_days. True when fine."""
    from .publish_instagram import token_info
    now = now or datetime.now(timezone.utc)
    days = float(ap_settings().get("token_warn_days", 7))
    tok = secret("IG_ACCESS_TOKEN")
    where = "a new long-lived Page token from the Graph API Explorer into IG_ACCESS_TOKEN in .env on the server"
    if not tok:
        alerts.fire("token", "IG_ACCESS_TOKEN is missing: nothing can be posted", where)
        return False
    try:
        info = token_info(tok)
    except Exception as e:  # noqa: BLE001
        from .meta_api import redact
        alerts.fire("token", f"the Meta token could not be checked ({redact(str(e))[:160]})", where)
        return False
    if info.get("is_valid") is False:
        why = (info.get("error") or {}).get("message", "debug_token says is_valid=false")
        alerts.fire("token", f"the Meta token is no longer valid ({why[:160]})", where)
        return False
    ends = [(k, datetime.fromtimestamp(int(info[k]), timezone.utc)) for k in ("expires_at", "data_access_expires_at")
            if info.get(k)]
    soon = [(k, t) for k, t in ends if t - now < timedelta(days=days)]
    if soon:
        k, t = min(soon, key=lambda x: x[1])
        what = "expires" if k == "expires_at" else "loses data access"
        alerts.fire("token", f"the Meta token {what} on {t:%Y-%m-%d %H:%M} UTC (within {days:g} days)", where)
        return False
    alerts.clear("token", "the Meta token is valid again")
    return True


def health(now: datetime | None = None) -> int:
    from . import autopick, generator
    check_token(now)
    generator.check_disk()
    generator.check_pool()
    insights_check(now)
    autopick.cleanup_posted_media(now)
    return 0
