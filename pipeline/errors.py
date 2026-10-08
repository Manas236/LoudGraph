"""One owner-facing explanation for each known failure, shared by every page."""
from __future__ import annotations


def explain_error(error: str | None, stage: str = "publish", service: str | None = None) -> dict:
    raw = error or ""
    text = raw.lower()
    prefix = f"{service} upload failed: " if service else ""
    section = "accounts"
    if any(x in text for x in ("expired", "invalid_grant", "invalid token", "code': 190", '"code": 190', "unauthorized")):
        sentence, fix = "the access token expired.", "Reconnect the account in Settings, then try again."
    elif any(x in text for x in ("quota", "uploadlimit", "daily cap", "rate limit")):
        sentence, fix, section = "the daily posting limit was reached.", "Wait until the daily limit resets, then try again.", "posting"
    elif any(x in text for x in ("connection", "network", "timeout", "timed out", "name resolution")):
        sentence, fix, section = "the internet connection was interrupted.", "Check your internet connection, then try again.", "advanced"
    elif any(x in text for x in ("permission", "forbidden", "insufficient", "scope")):
        sentence, fix = "the account is missing permission to post.", "Reconnect the account with the permissions listed in Settings."
    elif any(x in text for x in ("processing", "container", "media error")):
        sentence, fix, section = "the service could not process this video.", "Try the upload again.", "posting"
    elif "ffmpeg" in text or "brokenpipe" in text:
        sentence, fix, section = "the video maker stopped unexpectedly.", "Close other heavy apps, then retry making this video.", "advanced"
        prefix = ""
    else:
        activity = {"fetch": "getting the data", "pick": "choosing countries", "label": "writing labels",
                    "render": "making the video", "notify": "sending the preview", "publish": "posting the video"}
        return {"sentence": f"Something went wrong while {activity.get(stage, 'making the video')}.",
                "fix": "Try again. If it happens again, check the details below.", "section": "advanced", "details": raw}
    sentence = prefix + sentence if prefix else sentence[0].upper() + sentence[1:]
    return {"sentence": sentence, "fix": fix, "section": section, "details": raw}
