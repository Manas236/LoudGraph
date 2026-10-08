"""Cache an actual chart frame; never use or overwrite the renderer's end-card thumbnail."""
import json
import subprocess
import threading

from pipeline.config import path

_lock = threading.Lock()


def chart_thumbnail(run):
    folder = path("out") / run["id"]
    video = folder / "video.mp4"
    target = folder / "review-thumb.jpg"
    if not video.exists():
        return None
    timeline = folder / "timeline.json"
    modified = max(video.stat().st_mtime, timeline.stat().st_mtime if timeline.exists() else 0)
    with _lock:
        if target.exists() and target.stat().st_mtime >= modified:
            return target
        try:
            slot = json.loads(timeline.read_text(encoding="utf-8"))["slots"][0]
            time = (slot["draw_start"] + slot["draw_end"]) / 2
        except (OSError, ValueError, KeyError, IndexError):
            time = 2.5
        temp = target.with_name("review-thumb.tmp.jpg")
        try:
            result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(time),
                                     "-i", str(video), "-frames:v", "1", "-vf", "scale=405:720", str(temp)],
                                    capture_output=True, timeout=25,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0 and temp.exists():
                temp.replace(target)
                return target
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None
