"""`run.py doctor`: environment and credential checks. Never changes anything."""
from __future__ import annotations

import shutil
import subprocess
import tempfile

from .config import ROOT, get_config, path, secret, countries

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"


def _line(status: str, what: str, detail: str = "") -> tuple[str, str, str]:
    print(f"[{status}] {what}" + (f" - {detail}" if detail else ""))
    return status, what, detail


def check_ffmpeg() -> list:
    out = []
    for exe in ("ffmpeg", "ffprobe"):
        p = shutil.which(exe)
        if not p:
            out.append(_line(FAIL, exe, "not on PATH"))
            continue
        v = subprocess.run([p, "-version"], capture_output=True, text=True).stdout.splitlines()[0]
        out.append(_line(OK, exe, v[:70]))
    return out


def check_dirs() -> list:
    out = []
    for key in ("cache", "out", "tokens"):
        d = path(key)
        try:
            with tempfile.NamedTemporaryFile(dir=d, delete=True):
                pass
            out.append(_line(OK, f"dir {key}/", "writable"))
        except OSError as e:
            out.append(_line(FAIL, f"dir {key}/", str(e)))
    return out


def check_assets() -> list:
    out = []
    from . import assets
    fonts = assets.font_files()
    missing = [k for k, p in fonts.items() if not p.exists()]
    out.append(_line(FAIL if missing else OK, "fonts", f"missing {missing}" if missing else ", ".join(p.name for p in fonts.values())))
    flag_dir = path("flags")
    miss = [c["iso2"] for c in countries() if not (flag_dir / f"{c['iso2']}.png").exists()]
    out.append(_line(FAIL if miss else OK, "flags", f"missing {miss}" if miss else f"{len(countries())} PNG flags"))
    return out


def check_render_backend() -> list:
    from .canvas import pick_backend
    name, why = pick_backend(get_config()["render"]["backend"])
    return [_line(OK if name == "skia" else WARN, "render backend", f"{name} ({why})")]


def check_credentials() -> list:
    out = []
    # Gemini
    if not secret("GEMINI_API_KEY"):
        out.append(_line(WARN, "GEMINI_API_KEY", "missing: videos will have no turning-point labels"))
    else:
        from .labels import resolve_model
        try:
            m = resolve_model(refresh=True)
            out.append(_line(OK, "GEMINI_API_KEY", f"valid, model {m}"))
        except Exception as e:  # noqa: BLE001
            out.append(_line(FAIL, "GEMINI_API_KEY", f"present but failed: {e}"))
    # Telegram
    if not (secret("TELEGRAM_BOT_TOKEN") and secret("TELEGRAM_CHAT_ID")):
        out.append(_line(WARN, "Telegram", "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing: approval via dashboard only"))
    else:
        from .approve_telegram import check
        ok, detail = check()
        out.append(_line(OK if ok else FAIL, "Telegram", detail))
    # YouTube
    from .publish_youtube import check as yt_check
    st, detail = yt_check()
    out.append(_line(st, "YouTube", detail))
    # Instagram
    from .publish_instagram import check as ig_check
    st, detail = ig_check()
    out.append(_line(st, "Instagram", detail))
    if get_config()["facebook"]["enabled"]:
        st = OK if secret("FB_PAGE_ID") else WARN
        out.append(_line(st, "Facebook Page", "FB_PAGE_ID present" if secret("FB_PAGE_ID") else "FB_PAGE_ID missing"))
    dr = get_config()["dry_run"]
    out.append(_line(OK, "dry_run", ", ".join(f"{k}={v}" for k, v in dr.items())))
    return out


def doctor() -> int:
    print(f"Pipeline doctor  (root: {ROOT})")
    results = []
    for fn in (check_ffmpeg, check_dirs, check_assets, check_render_backend, check_credentials):
        try:
            results += fn()
        except Exception as e:  # noqa: BLE001 - doctor must report, not crash
            results.append(_line(FAIL, fn.__name__, f"{type(e).__name__}: {e}"))
    fails = sum(1 for r in results if r[0] == FAIL)
    warns = sum(1 for r in results if r[0] == WARN)
    print(f"\n{fails} failing, {warns} warnings")
    return 1 if fails else 0
