"""Instagram Reels (+ optional Facebook Page Reels) publishing (section 11).

Verified against the Meta docs on 2026-10-07 (examples use v25.0):
  IG:  POST /{ig-user-id}/media  media_type=REELS, upload_type=resumable, caption, share_to_feed
       POST https://rupload.facebook.com/ig-api-upload/{ver}/{container-id}
            headers: Authorization: OAuth <token>, offset: 0, file_size: <bytes>; body = file bytes
       GET  /{container-id}?fields=status_code   -> FINISHED | IN_PROGRESS | ERROR | EXPIRED | PUBLISHED
       POST /{ig-user-id}/media_publish  creation_id=<container-id>
       (accounts are limited to 100 API-published posts per 24 h)
  FB:  POST /{page-id}/video_reels upload_phase=start -> video_id, upload_url
       POST https://rupload.facebook.com/video-upload/{ver}/{video-id} (same headers)
       POST /{page-id}/video_reels upload_phase=finish, video_id, video_state=PUBLISHED, description
With Facebook Login both use a Page access token (IG_ACCESS_TOKEN).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import db
from .config import get_config, run_dir, secret

log = logging.getLogger(__name__)
DONE = {"uploaded", "live", "private_locked", "dry_run"}
# permissions the Page token needs (README "Credentials"); Facebook Page Reels add the second list
SCOPES_IG = ["instagram_basic", "instagram_content_publish", "instagram_manage_insights", "pages_read_engagement"]
SCOPES_FB = ["pages_show_list", "pages_manage_posts"]
EXPIRY_WARN_DAYS = 7


def _ic() -> dict:
    return get_config()["instagram"]


def base(section: str = "instagram") -> str:
    c = get_config()[section]
    return f"{c['graph_base']}/{c['api_version']}"


def _redact(d: dict) -> dict:
    return {k: ("<IG_ACCESS_TOKEN>" if k == "access_token" else v) for k, v in d.items()}


def _check_json(r: requests.Response, what: str) -> dict:
    try:
        d = r.json()
    except ValueError:
        raise RuntimeError(f"{what}: HTTP {r.status_code} {r.text[:300]}")
    if r.status_code != 200 or "error" in d:
        raise RuntimeError(f"{what}: HTTP {r.status_code} {d.get('error', d)}")
    return d


def needed_scopes() -> list[str]:
    return SCOPES_IG + (SCOPES_FB if get_config()["facebook"]["enabled"] else [])


def token_info(tok: str) -> dict:
    """GET /debug_token for the token itself: is_valid, expires_at (0 = never), scopes."""
    r = requests.get(f"{base()}/debug_token", params={"input_token": tok, "access_token": tok}, timeout=30)
    return _check_json(r, "debug_token").get("data", {})


def check() -> dict:
    """Doctor / dashboard health check: level (OK/WARN/FAIL), state (ok/missing/expired/error), detail."""
    dry = get_config()["dry_run"]["instagram"]
    ig, tok = secret("IG_USER_ID"), secret("IG_ACCESS_TOKEN")
    if not (ig and tok):
        return {"level": "WARN", "state": "missing", "detail": f"IG_USER_ID / IG_ACCESS_TOKEN missing (dry_run={dry})"}
    try:
        info = token_info(tok)
    except Exception as e:  # noqa: BLE001 - the lookup below still tells valid from invalid
        info = {"_error": str(e)[:150]}
    if info.get("is_valid") is False:
        why = (info.get("error") or {}).get("message", "debug_token is_valid=false")
        return {"level": "FAIL", "state": "expired", "detail": f"IG_ACCESS_TOKEN no longer valid: {why}"[:300]}
    try:
        d = _check_json(requests.get(f"{base()}/{ig}", params={"fields": "id,username", "access_token": tok},
                                     timeout=30), "IG user lookup")
    except Exception as e:  # noqa: BLE001
        state = "expired" if "'code': 190" in str(e) else "error"   # 190 = invalid / expired OAuth token
        return {"level": "FAIL", "state": state, "detail": str(e)[:300]}
    parts, level = [f"@{d.get('username')} ({d.get('id')})"], "OK"
    exp = info.get("expires_at")
    if exp:
        when = datetime.fromtimestamp(exp, timezone.utc)
        parts.append(f"token expires {when:%Y-%m-%d}")
        if (when - datetime.now(timezone.utc)).days < EXPIRY_WARN_DAYS:
            level = "WARN"
    elif exp == 0:
        parts.append("token never expires")
    if "scopes" in info:
        missing = [s for s in needed_scopes() if s not in info["scopes"]]
        if missing:
            parts.append("missing permissions: " + ", ".join(missing))
            level = "WARN"
    else:
        parts.append(f"permissions not checked ({info.get('_error', 'debug_token returned no scopes')})")
    try:
        lim = requests.get(f"{base()}/{ig}/content_publishing_limit", params={"access_token": tok}, timeout=30)
        if lim.status_code == 200:
            parts.append(f"publishing limit {lim.json().get('data')}")
    except requests.RequestException:
        pass
    return {"level": level, "state": "ok", "detail": ", ".join(parts) + f" (dry_run={dry})"}


def check_facebook() -> dict:
    if not get_config()["facebook"]["enabled"]:
        return {"level": "OK", "state": "disabled", "detail": "facebook.enabled is false"}
    page, tok = secret("FB_PAGE_ID"), secret("IG_ACCESS_TOKEN")
    if not (page and tok):
        return {"level": "WARN", "state": "missing", "detail": "FB_PAGE_ID / IG_ACCESS_TOKEN (Page token) missing"}
    try:
        d = _check_json(requests.get(f"{base('facebook')}/{page}", params={"fields": "id,name", "access_token": tok},
                                     timeout=30), "FB page lookup")
    except Exception as e:  # noqa: BLE001
        state = "expired" if "'code': 190" in str(e) else "error"
        return {"level": "FAIL", "state": state, "detail": str(e)[:300]}
    return {"level": "OK", "state": "ok",
            "detail": f"Page {d.get('name')} ({d.get('id')}) (dry_run={get_config()['dry_run']['facebook']})"}


def _plan(run_id: str, meta: dict, video: Path) -> tuple[dict, str | None]:
    ic = _ic()
    params = {"media_type": "REELS", "caption": meta["description_instagram"][:2200],
              "share_to_feed": "true" if ic["share_to_feed"] else "false"}
    blocked = None
    if ic["upload_mode"] == "resumable":
        params["upload_type"] = "resumable"
    elif ic["public_base_url"]:
        params["video_url"] = ic["public_base_url"].rstrip("/") + f"/{run_id}.mp4"
    else:
        blocked = "BLOCKED: upload_mode=video_url needs instagram.public_base_url (a public file host) in config.yaml"
    return params, blocked


def publish(run_id: str, meta: dict, video: Path) -> str:
    cfg = get_config()
    existing = {p["platform"]: p for p in db.get_posts(run_id)}.get("instagram")
    if existing and existing["status"] in DONE:
        return existing["status"]
    ic = _ic()
    ig = secret("IG_USER_ID") or "<IG_USER_ID>"
    params, blocked = _plan(run_id, meta, video)
    if cfg["dry_run"]["instagram"]:
        steps = [{"POST": f"{base()}/{ig}/media", "data": {**params, "access_token": "<IG_ACCESS_TOKEN>"}}]
        if params.get("upload_type") == "resumable":
            steps.append({"POST": f"https://rupload.facebook.com/ig-api-upload/{ic['api_version']}/<container-id>",
                          "headers": {"Authorization": "OAuth <IG_ACCESS_TOKEN>", "offset": "0",
                                      "file_size": str(video.stat().st_size)}, "body": str(video)})
        steps += [{"GET": f"{base()}/<container-id>?fields=status_code", "until": "FINISHED",
                   "every_s": ic["poll_seconds"], "max_min": ic["poll_max_minutes"]},
                  {"POST": f"{base()}/{ig}/media_publish", "data": {"creation_id": "<container-id>"}}]
        (run_dir(run_id) / "publish_instagram.json").write_text(
            json.dumps({"dry_run": True, "blocked": blocked, "steps": steps}, indent=1), encoding="utf-8")
        db.upsert_post(run_id, "instagram", "dry_run",
                       message="dry run: request written to publish_instagram.json" + (f"; {blocked}" if blocked else ""))
        return "dry_run"
    if blocked:
        db.upsert_post(run_id, "instagram", "failed", message=blocked)
        return "failed"
    tok = secret("IG_ACCESS_TOKEN")
    if not (secret("IG_USER_ID") and tok):
        db.upsert_post(run_id, "instagram", "failed", message="BLOCKED: IG_USER_ID / IG_ACCESS_TOKEN missing")
        return "failed"
    db.upsert_post(run_id, "instagram", "uploading", message="creating the REELS container")
    d = _check_json(requests.post(f"{base()}/{ig}/media", data={**params, "access_token": tok}, timeout=60),
                    "create REELS container")
    cid = d["id"]
    db.upsert_post(run_id, "instagram", "uploading", remote_id=cid, message="container created, uploading")
    if params.get("upload_type") == "resumable":
        size = video.stat().st_size
        with open(video, "rb") as f:
            r = requests.post(f"https://rupload.facebook.com/ig-api-upload/{ic['api_version']}/{cid}",
                              headers={"Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)},
                              data=f, timeout=600)
        up = _check_json(r, "rupload")
        if not up.get("success", True):
            raise RuntimeError(f"rupload: {up}")
        db.upsert_post(run_id, "instagram", "uploading", remote_id=cid, message="file uploaded, Instagram is processing")
    deadline = time.time() + ic["poll_max_minutes"] * 60
    status = None
    while time.time() < deadline:
        s = _check_json(requests.get(f"{base()}/{cid}", params={"fields": "status_code,status", "access_token": tok},
                                     timeout=30), "container status")
        status = s.get("status_code")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            db.upsert_post(run_id, "instagram", "failed", remote_id=cid, message=f"container {status}: {s.get('status')}")
            return "failed"
        time.sleep(ic["poll_seconds"])
    if status != "FINISHED":
        db.upsert_post(run_id, "instagram", "failed", remote_id=cid, message=f"container not FINISHED in time ({status})")
        return "failed"
    pub = _check_json(requests.post(f"{base()}/{ig}/media_publish", data={"creation_id": cid, "access_token": tok},
                                    timeout=60), "media_publish")
    mid = pub["id"]
    link = None
    try:
        link = requests.get(f"{base()}/{mid}", params={"fields": "permalink", "access_token": tok},
                            timeout=30).json().get("permalink")
    except Exception:  # noqa: BLE001 - the post is live either way
        pass
    db.upsert_post(run_id, "instagram", "live", remote_id=mid, url=link, message="published")
    return "live"


def publish_facebook(run_id: str, meta: dict, video: Path) -> str:
    cfg = get_config()
    existing = {p["platform"]: p for p in db.get_posts(run_id)}.get("facebook")
    if existing and existing["status"] in DONE:
        return existing["status"]
    fc = cfg["facebook"]
    page = secret("FB_PAGE_ID") or "<FB_PAGE_ID>"
    desc = meta["description_instagram"][:2200]
    if cfg["dry_run"]["facebook"]:
        steps = [{"POST": f"{base('facebook')}/{page}/video_reels", "data": {"upload_phase": "start",
                                                                             "access_token": "<IG_ACCESS_TOKEN>"}},
                 {"POST": f"https://rupload.facebook.com/video-upload/{fc['api_version']}/<video_id>",
                  "headers": {"Authorization": "OAuth <IG_ACCESS_TOKEN>", "offset": "0",
                              "file_size": str(video.stat().st_size)}},
                 {"POST": f"{base('facebook')}/{page}/video_reels",
                  "data": {"upload_phase": "finish", "video_id": "<video_id>", "video_state": "PUBLISHED",
                           "description": desc}}]
        (run_dir(run_id) / "publish_facebook.json").write_text(json.dumps({"dry_run": True, "steps": steps}, indent=1),
                                                               encoding="utf-8")
        db.upsert_post(run_id, "facebook", "dry_run", message="dry run: request written to publish_facebook.json")
        return "dry_run"
    tok = secret("IG_ACCESS_TOKEN")
    if not (secret("FB_PAGE_ID") and tok):
        db.upsert_post(run_id, "facebook", "failed", message="BLOCKED: FB_PAGE_ID / page token missing")
        return "failed"
    db.upsert_post(run_id, "facebook", "uploading", message="starting the Reels upload")
    start = _check_json(requests.post(f"{base('facebook')}/{page}/video_reels",
                                      data={"upload_phase": "start", "access_token": tok}, timeout=60), "FB reels start")
    vid = start["video_id"]
    with open(video, "rb") as f:
        _check_json(requests.post(f"https://rupload.facebook.com/video-upload/{fc['api_version']}/{vid}",
                                  headers={"Authorization": f"OAuth {tok}", "offset": "0",
                                           "file_size": str(video.stat().st_size)}, data=f, timeout=600), "FB rupload")
    _check_json(requests.post(f"{base('facebook')}/{page}/video_reels",
                              data={"upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
                                    "description": desc, "access_token": tok}, timeout=60), "FB reels finish")
    # finish with video_state=PUBLISHED publishes it; Facebook may still be processing for a few minutes
    db.upsert_post(run_id, "facebook", "live", remote_id=vid, url=f"https://www.facebook.com/reel/{vid}",
                   message="published (Facebook may still be processing)")
    return "live"
