"""YouTube Shorts upload (section 11) via google-api-python-client.

OAuth installed-app flow; the token lives in tokens/youtube_token.json. Authorise once with:
    python -m pipeline.publish_youtube --auth
A vertical video under 3 minutes is treated as a Short by YouTube.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

from . import db
from .config import ROOT, get_config, path, run_dir, secret

log = logging.getLogger(__name__)
DONE = {"uploaded", "live", "private_locked", "dry_run"}


class NotAuthorized(RuntimeError):
    pass


def client_secret_file() -> Path | None:
    v = secret("YT_CLIENT_SECRET_FILE")
    if not v:
        return None
    p = Path(v)
    return p if p.is_absolute() else ROOT / p


def token_file() -> Path:
    return path("tokens") / "youtube_token.json"


def get_credentials(interactive: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    scopes = get_config()["youtube"]["scopes"]
    creds = None
    tf = token_file()
    if tf.exists():
        creds = Credentials.from_authorized_user_file(str(tf), scopes)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        tf.write_text(creds.to_json(), encoding="utf-8")
    if creds and creds.valid:
        return creds
    if not interactive:
        raise NotAuthorized("no valid YouTube token; run: python -m pipeline.publish_youtube --auth")
    sf = client_secret_file()
    if not sf or not sf.exists():
        raise NotAuthorized(f"YT_CLIENT_SECRET_FILE not set or missing ({sf})")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(sf), scopes)
    creds = flow.run_local_server(port=8765, open_browser=True)
    tf.write_text(creds.to_json(), encoding="utf-8")
    return creds


def check() -> tuple[str, str]:
    dry = get_config()["dry_run"]["youtube"]
    sf = client_secret_file()
    if not sf:
        return "WARN", f"YT_CLIENT_SECRET_FILE missing (dry_run={dry})"
    if not sf.exists():
        return "FAIL", f"client secret file not found: {sf}"
    try:
        creds = get_credentials(interactive=False)
    except NotAuthorized as e:
        return "WARN", str(e)
    except Exception as e:  # noqa: BLE001
        return "FAIL", f"token invalid: {e}"
    try:
        from googleapiclient.discovery import build
        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        ch = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
        name = ch[0]["snippet"]["title"] if ch else "(no channel)"
        return "OK  ", f"token valid, channel {name} (dry_run={dry})"
    except Exception as e:  # noqa: BLE001
        return "FAIL", f"token present but API call failed: {e}"


def _body(meta: dict) -> dict:
    yc = get_config()["youtube"]
    return {
        "snippet": {
            "title": meta["title"][:100],
            "description": meta["description_youtube"][:5000],
            "tags": meta["tags"],
            "categoryId": yc["category_id"],
        },
        "status": {"privacyStatus": yc["privacy_status"], "selfDeclaredMadeForKids": False},
    }


def publish(run_id: str, meta: dict, video: Path) -> str:
    cfg = get_config()
    existing = {p["platform"]: p for p in db.get_posts(run_id)}.get("youtube")
    if existing and existing["status"] in DONE:
        return existing["status"]
    body = _body(meta)
    if cfg["dry_run"]["youtube"]:
        (run_dir(run_id) / "publish_youtube.json").write_text(json.dumps({
            "dry_run": True, "call": "youtube.videos.insert", "part": "snippet,status", "body": body,
            "media": {"file": str(video), "bytes": video.stat().st_size, "mimetype": "video/mp4", "resumable": True},
            "after_upload": "videos.list(part=status) to detect a forced-private lock",
        }, indent=1), encoding="utf-8")
        db.upsert_post(run_id, "youtube", "dry_run", message="dry run: request written to publish_youtube.json")
        return "dry_run"
    cap = cfg["youtube"]["max_uploads_per_day"]
    if db.uploads_today("youtube") >= cap:
        db.upsert_post(run_id, "youtube", "pending", message=f"daily cap of {cap} uploads reached")
        return "pending"
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    yt = build("youtube", "v3", credentials=get_credentials(), cache_discovery=False)
    media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response, errors = None, 0
    while response is None:
        try:
            _, response = req.next_chunk()
        except HttpError as e:
            reason = str(e)
            if e.resp.status in (500, 502, 503, 504) and errors < 5:
                errors += 1
                time.sleep(2 ** errors)
                continue
            if "quotaExceeded" in reason or "uploadLimitExceeded" in reason:
                db.upsert_post(run_id, "youtube", "failed", message=f"quota: {reason[:300]}")
                return "failed"
            raise
    vid = response["id"]
    url = f"https://youtube.com/shorts/{vid}"
    db.upsert_post(run_id, "youtube", "uploaded", remote_id=vid, url=url, message="uploaded")
    got = yt.videos().list(part="status", id=vid).execute().get("items", [])
    ps = got[0]["status"]["privacyStatus"] if got else None
    wanted = cfg["youtube"]["privacy_status"]
    if ps == "private" and wanted != "private":
        msg = ("privacyStatus forced to private: unverified API projects can only upload private videos. "
               "Request an API audit (YouTube API Services) or publish manually in Studio.")
        db.upsert_post(run_id, "youtube", "private_locked", remote_id=vid, url=url, message=msg)
        from .orchestrator import alert
        alert(f"⚠️ YouTube upload {vid} for {run_id} is locked private. {msg}")
        return "private_locked"
    db.upsert_post(run_id, "youtube", "live", remote_id=vid, url=url, message=f"privacyStatus={ps}")
    return "live"


if __name__ == "__main__":
    from .config import setup_logging
    setup_logging()
    if "--auth" in sys.argv:
        get_credentials(interactive=True)
        print("YouTube token saved to", token_file())
        print(check())
    else:
        print(check())
