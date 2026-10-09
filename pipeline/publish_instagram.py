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
       GET  /{video-id}?fields=status,permalink_url  (video_status, processing/publishing phases)
       Page Reels need a PAGE access token with pages_show_list, pages_read_engagement and
       pages_manage_posts (Meta "Reels Publishing API" docs, checked 2026-10-09).
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
# permissions the Page token needs (README "Credentials"); Facebook Page Reels need the second list
SCOPES_IG = ["instagram_basic", "instagram_content_publish", "instagram_manage_insights", "pages_read_engagement"]
SCOPES_FB = ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]
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
    missing = []
    if "scopes" in info:
        missing = [s for s in SCOPES_IG if s not in info["scopes"]]
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
    return {"level": level, "state": "ok", "detail": ", ".join(parts) + f" (dry_run={dry})", "missing": missing}


def check_facebook(force: bool = False) -> dict:
    """Page Reels readiness, read-only: the token is a valid PAGE token for FB_PAGE_ID with every
    permission in SCOPES_FB, and the Page answers. force=True checks even while facebook is off."""
    if not force and not get_config()["facebook"]["enabled"]:
        return {"level": "OK", "state": "disabled", "detail": "facebook.enabled is false"}
    page, tok = secret("FB_PAGE_ID"), secret("IG_ACCESS_TOKEN")
    if not (page and tok):
        return {"level": "WARN", "state": "missing", "detail": "FB_PAGE_ID / IG_ACCESS_TOKEN (Page token) missing"}
    try:
        info = token_info(tok)
    except Exception as e:  # noqa: BLE001 - without debug_token the permissions cannot be confirmed
        return {"level": "FAIL", "state": "error", "detail": f"permissions not checked: {e}"[:400]}
    if info.get("is_valid") is False:
        why = (info.get("error") or {}).get("message", "debug_token is_valid=false")
        return {"level": "FAIL", "state": "expired", "detail": f"IG_ACCESS_TOKEN no longer valid: {why}"[:400]}
    try:
        d = _check_json(requests.get(f"{base('facebook')}/{page}", params={"fields": "id,name", "access_token": tok},
                                     timeout=30), "FB page lookup")
    except Exception as e:  # noqa: BLE001
        state = "expired" if "'code': 190" in str(e) else "error"
        return {"level": "FAIL", "state": state, "detail": str(e)[:400]}
    problems = []
    if info.get("type") and info["type"] != "PAGE":
        problems.append(f"IG_ACCESS_TOKEN is a {info['type']} token; Page Reels need a Page access token")
    if info.get("profile_id") and str(info["profile_id"]) != str(page):
        problems.append(f"the token belongs to Page {info['profile_id']}, not FB_PAGE_ID {page}")
    missing = [s for s in SCOPES_FB if s not in info.get("scopes", [])]
    if missing:
        problems.append("missing permissions: " + ", ".join(missing))
    detail = f"Page {d.get('name')} ({d.get('id')})"
    if problems:
        return {"level": "FAIL", "state": "error", "detail": detail + ": " + "; ".join(problems), "missing": missing}
    return {"level": "OK", "state": "ok", "missing": [],
            "detail": detail + f", Page token with {', '.join(SCOPES_FB)} (dry_run={get_config()['dry_run']['facebook']})"}


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
    done = db.already_posted(run_id, "instagram", cfg["dry_run"]["instagram"])
    if done:
        return done
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
    done = db.already_posted(run_id, "facebook", cfg["dry_run"]["facebook"])
    if done:
        return done
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
    db.upsert_post(run_id, "facebook", "uploading", remote_id=vid, message="upload session started, uploading")
    with open(video, "rb") as f:
        up = _check_json(requests.post(f"https://rupload.facebook.com/video-upload/{fc['api_version']}/{vid}",
                                       headers={"Authorization": f"OAuth {tok}", "offset": "0",
                                                "file_size": str(video.stat().st_size)}, data=f, timeout=600), "FB rupload")
    if not up.get("success", True):
        raise RuntimeError(f"FB rupload: {up}")
    fin = _check_json(requests.post(f"{base('facebook')}/{page}/video_reels",
                                    data={"upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
                                          "description": desc, "access_token": tok}, timeout=60), "FB reels finish")
    if not fin.get("success", True):
        raise RuntimeError(f"FB reels finish: {fin}")
    db.upsert_post(run_id, "facebook", "uploading", remote_id=vid, message="published, Facebook is processing")
    # finish with video_state=PUBLISHED publishes it; follow processing so an error is reported, not hidden
    deadline = time.time() + fc.get("poll_max_minutes", 5) * 60
    link, note = None, "published (Facebook was still processing)"
    while time.time() < deadline:
        s = _check_json(requests.get(f"{base('facebook')}/{vid}", params={"fields": "status,permalink_url",
                                                                         "access_token": tok}, timeout=30), "FB reel status")
        st = s.get("status") or {}
        link = s.get("permalink_url") or link
        phases = [st.get(k) or {} for k in ("uploading_phase", "processing_phase", "publishing_phase")]
        if st.get("video_status") in ("error", "upload_failed", "expired") or any(ph.get("status") == "error" for ph in phases):
            errors = [ph.get("errors") for ph in phases if ph.get("errors")]
            db.upsert_post(run_id, "facebook", "failed", remote_id=vid,
                           message=f"Facebook reel {st.get('video_status')}: {errors or st}"[:500])
            return "failed"
        if (st.get("publishing_phase") or {}).get("publish_status") == "published" or st.get("video_status") == "ready":
            note = "published"
            break
        time.sleep(fc.get("poll_seconds", 10))
    url = ("https://www.facebook.com" + link if link and link.startswith("/") else link) or \
        f"https://www.facebook.com/reel/{vid}"
    db.upsert_post(run_id, "facebook", "live", remote_id=vid, url=url, message=note)
    return "live"
