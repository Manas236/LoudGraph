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
from pathlib import Path

import requests

from . import db
from .config import get_config, run_dir, secret

log = logging.getLogger(__name__)
DONE = {"uploaded", "live", "private_locked", "dry_run"}


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


def check() -> tuple[str, str]:
    dry = get_config()["dry_run"]["instagram"]
    ig, tok = secret("IG_USER_ID"), secret("IG_ACCESS_TOKEN")
    if not (ig and tok):
        return "WARN", f"IG_USER_ID / IG_ACCESS_TOKEN missing (dry_run={dry})"
    try:
        r = requests.get(f"{base()}/{ig}", params={"fields": "id,username", "access_token": tok}, timeout=30)
        d = _check_json(r, "IG user lookup")
        lim = requests.get(f"{base()}/{ig}/content_publishing_limit", params={"access_token": tok}, timeout=30)
        extra = f", publishing limit {lim.json().get('data')}" if lim.status_code == 200 else ""
        return "OK  ", f"@{d.get('username')} ({d.get('id')}){extra} (dry_run={dry})"
    except Exception as e:  # noqa: BLE001
        return "FAIL", str(e)[:300]


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
    d = _check_json(requests.post(f"{base()}/{ig}/media", data={**params, "access_token": tok}, timeout=60),
                    "create REELS container")
    cid = d["id"]
    db.upsert_post(run_id, "instagram", "pending", remote_id=cid, message="container created")
    if params.get("upload_type") == "resumable":
        size = video.stat().st_size
        with open(video, "rb") as f:
            r = requests.post(f"https://rupload.facebook.com/ig-api-upload/{ic['api_version']}/{cid}",
                              headers={"Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)},
                              data=f, timeout=600)
        up = _check_json(r, "rupload")
        if not up.get("success", True):
            raise RuntimeError(f"rupload: {up}")
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
    db.upsert_post(run_id, "facebook", "uploaded", remote_id=vid, message="published (processing on Facebook)")
    return "uploaded"
