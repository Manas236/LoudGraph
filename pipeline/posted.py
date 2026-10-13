"""A reel that went live although its publisher never recorded it.

A publisher can die after the service published the reel and before the `live` status was saved:
the post then reads `uploading` or `failed` while the reel is public. Before uploading again, the
orchestrator asks the service about that earlier attempt (its id is in posts.remote_id) and records
it instead of posting a second copy. Read-only: nothing is uploaded or published here.

  IG:  GET /{container-id}?fields=status_code          PUBLISHED = media_publish went through
       GET /{ig-user-id}/media?fields=id,caption,...   the reel itself (a container has no media id)
  FB:  GET /{video-id}?fields=status,permalink_url     publishing_phase.publish_status == published
"""
from __future__ import annotations

from . import config, db, meta_api
from .config import secret
from .publish_instagram import _check_json, base

GONE = 100   # Graph API error code: the object does not exist (any more) or cannot be read
RECENT = 25  # reels looked through for the one an earlier attempt published


def _get(url: str, what: str, **params) -> dict | None:
    """GET a Graph object. None when it is gone; every other error is raised."""
    r = meta_api.get(url, params={**params, "access_token": secret("IG_ACCESS_TOKEN")}, timeout=30)
    try:
        if (r.json().get("error") or {}).get("code") == GONE:
            return None
    except ValueError:
        pass
    return _check_json(r, what)


def _instagram(post: dict, meta: dict) -> dict | None:
    box = _get(f"{base()}/{post['remote_id']}", "container status", fields="status_code")
    if box and box.get("status_code") != "PUBLISHED":
        return None   # in progress, finished, failed or expired: it was never published
    caption = meta["description_instagram"][:2200].strip()
    recent = _get(f"{base()}/{secret('IG_USER_ID')}/media", "recent media",
                  fields="id,caption,permalink,timestamp", limit=RECENT) or {}
    for m in recent.get("data", []):
        # both are UTC: "2026-10-09T11:27:17+0000" (Instagram) and "2026-10-09T11:26:50Z" (ours)
        if (m.get("caption") or "").strip() == caption and (m.get("timestamp") or "")[:19] >= post["created_at"][:19]:
            return {"remote_id": m["id"], "url": m.get("permalink")}
    return {} if box else None   # published but not among the recent reels: live, link unknown


def _facebook(post: dict, meta: dict) -> dict | None:
    reel = _get(f"{base('facebook')}/{post['remote_id']}", "FB reel status", fields="status,permalink_url") or {}
    if ((reel.get("status") or {}).get("publishing_phase") or {}).get("publish_status") != "published":
        return None
    link = reel.get("permalink_url")
    return {"url": ("https://www.facebook.com" + link if link and link.startswith("/") else link) or
            f"https://www.facebook.com/reel/{post['remote_id']}"}


CHECKS = {"instagram": _instagram, "facebook": _facebook}


def earlier_attempt_live(run_id: str, platform: str, meta: dict) -> str | None:
    """"live" (and the post is recorded as live) when an earlier, unfinished attempt at this post
    is public after all; None when a new upload is the right thing to do."""
    post = next((p for p in db.get_posts(run_id) if p["platform"] == platform), None)
    # config.get_config, looked up on each call: this module is first imported in the middle of a publish
    if platform not in CHECKS or config.get_config()["dry_run"].get(platform, True) or not secret("IG_ACCESS_TOKEN") \
            or not post or post["status"] not in ("uploading", "failed") or not post["remote_id"]:
        return None
    found = CHECKS[platform](post, meta)
    if found is None:
        return None
    db.upsert_post(run_id, platform, "live", remote_id=found.get("remote_id"), url=found.get("url"),
                   message="published (an earlier upload had already gone live; nothing was uploaded again)")
    return "live"
