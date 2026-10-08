"""Local visual QA with headless Edge and disposable SQLite snapshots.

Run with .venv\Scripts\python.exe scripts\dashboard_screenshots.py.
Uses websocket-client for the Chrome DevTools protocol (no Playwright or Node).
Nothing here can approve, publish, render, or change the real database.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests
import websocket

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import config

for name in config.SECRET_NAMES:  # QA never sees or calls an API with the owner's credentials
    os.environ.pop(name, None)
original_cfg = copy.deepcopy(config.get_config())
real_db = config.path("db")
scratch = Path(tempfile.mkdtemp(prefix="dashboard-v3-"))
cfg = copy.deepcopy(original_cfg)
cfg["paths"] = {key: str((ROOT / value).resolve()) for key, value in cfg["paths"].items()}
cfg["paths"]["db"] = str(scratch / "qa.db")
cfg["paths"]["cache"] = str(scratch / "cache")
config.get_config = lambda: cfg

from dashboard.app import app
from pipeline import actions, db
from werkzeug.serving import make_server

app.config["TESTING"] = True
logging.getLogger("werkzeug").setLevel(logging.ERROR)
actions.spawn = lambda *args: 0
out = ROOT / "out" / "dashboard_screens" / "v3"
out.mkdir(parents=True, exist_ok=True)


def snapshot(name):
    target = scratch / (name + ".db")
    with sqlite3.connect(f"file:{real_db.as_posix()}?mode=ro", uri=True) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
    cfg["paths"]["db"] = str(target)
    db.init()
    return sorted(db.list_runs(stage="awaiting_approval"), key=lambda r: r["ref"])


def fake_state(name):
    runs = snapshot(name)
    assert len(runs) == 8, "QA expects the real eight waiting videos"
    if name == "empty":
        with db.db() as c:
            c.execute("UPDATE runs SET stage='rejected' WHERE stage='awaiting_approval'")
    elif name == "making":
        with db.db() as c:
            c.execute("UPDATE runs SET stage='labelled',progress=.62,progress_at=?,updated_at=? WHERE id=?",
                      (db.now(), db.now(), runs[-1]["id"]))
    elif name == "attention":
        db.fail(runs[-1]["id"], "publish", "Instagram token expired; OAuth code 190")
        db.upsert_post(runs[-1]["id"], "instagram", "failed", message="The access token expired (code 190)")
    elif name == "library":
        for index, run in enumerate(runs):
            stage = "published" if index < 3 else "approved" if index < 6 else "rejected"
            db.transition(run["id"], stage, "Screenshot fixture only", reset=True)
            if index < 3:
                db.upsert_post(run["id"], "youtube", "live", remote_id="example", url="https://youtube.com/shorts/example")
                youtube = next(p for p in db.get_posts(run["id"]) if p["platform"] == "youtube")
                db.add_stats(youtube["id"], views=[1240, 2830, 891][index], likes=47, avg_view_pct=73.2)
                db.upsert_post(run["id"], "instagram", "failed" if index == 0 else "uploading",
                               message="token expired" if index == 0 else None)
            if index == 4:
                db.upsert_post(run["id"], "youtube", "dry_run")
            if index == 7:
                with db.db() as c:
                    c.execute("UPDATE runs SET replaced=1 WHERE id=?", (run["id"],))
    return runs


class CDP:
    def __init__(self, url):
        self.socket = websocket.create_connection(url, timeout=30, origin="http://127.0.0.1:9237")
        self.seq = 0

    def call(self, method, **params):
        self.seq += 1
        self.socket.send(json.dumps({"id": self.seq, "method": method, "params": params}))
        while True:
            response = json.loads(self.socket.recv())
            if response.get("id") == self.seq:
                if "error" in response:
                    raise RuntimeError(response["error"])
                return response.get("result", {})

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if result.get("exceptionDetails"):
            raise RuntimeError(result["exceptionDetails"])
        return result.get("result", {}).get("value")

    def visit(self, route):
        self.call("Page.navigate", url="http://127.0.0.1:5067" + route)
        for _ in range(100):
            try:
                if self.evaluate("document.readyState === 'complete' && !!document.querySelector('main')"):
                    break
            except Exception:
                pass
            time.sleep(.1)
        self.evaluate("Promise.all([...document.images].map(i => i.complete ? Promise.resolve() : new Promise(r => {i.onload=r;i.onerror=r})))")
        time.sleep(.5)

    def until(self, expression, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.evaluate(expression):
                return
            time.sleep(.1)
        raise AssertionError(f"Browser condition did not become true: {expression}")

    def capture(self, name, width, height, route, scroll=None):
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)
        self.visit(route)
        if "video=" in route:
            for _ in range(50):
                if self.evaluate("!!document.querySelector('#video-drawer[open] video')"):
                    break
                time.sleep(.1)
        if self.evaluate("!!document.querySelector('video')"):
            self.until("document.querySelector('video').readyState >= 2 || !!document.querySelector('video').error")
            assert not self.evaluate("!!document.querySelector('video').error"), "The real MP4 must play"
            self.evaluate("document.querySelector('video').currentTime = 2.5")
            self.until("!document.querySelector('video').seeking && document.querySelector('video').readyState >= 2")
            time.sleep(.6)
        if scroll:
            self.evaluate(scroll)
            time.sleep(.2)
        geometry = self.evaluate("JSON.stringify({width:innerWidth,height:innerHeight,overflow:document.documentElement.scrollWidth>innerWidth,status:document.querySelectorAll('#status-line .status').length,lanes:document.body.innerText.includes('Nothing here'), ids:/r\\d{8}-\\d{6}-[0-9a-f]{4}/.test(document.body.innerText),slug:/air_passengers|homicide_rate|life_expectancy|nuclear_share/.test(document.body.innerText)})")
        checks = json.loads(geometry)
        assert not checks["overflow"], (name, checks)
        assert checks["status"] == 1 and not checks["lanes"], (name, checks)
        assert not checks["ids"] and not checks["slug"], (name, checks)
        data = self.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)["data"]
        file = out / f"{name}_{width}x{height}.png"
        file.write_bytes(base64.b64decode(data))
        print(file.relative_to(ROOT), flush=True)
        return {"file": str(file.relative_to(ROOT)), **checks}


def real_state():
    with sqlite3.connect(f"file:{real_db.as_posix()}?mode=ro", uri=True) as conn:
        return conn.execute("SELECT id,stage,title,platforms,replaced FROM runs ORDER BY id").fetchall()


def main():
    before = real_state()
    server = make_server("127.0.0.1", 5067, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    process = subprocess.Popen([str(edge), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                                "--remote-debugging-port=9237", "--remote-allow-origins=http://127.0.0.1:9237",
                                "--user-data-dir=" + str(scratch / "edge-profile"), "about:blank"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    report = []
    try:
        pages = None
        for _ in range(100):
            try:
                pages = requests.get("http://127.0.0.1:9237/json", timeout=1).json()
                if pages:
                    break
            except requests.RequestException:
                pass
            time.sleep(.1)
        cdp = CDP(next(p["webSocketDebuggerUrl"] for p in pages if p["type"] == "page"))
        cdp.call("Page.enable")
        for name in ("queue", "empty", "making", "attention", "library", "settings"):
            runs = fake_state(name)
            routes = [("review_" + name, "/")] if name not in ("library", "settings") else (
                [("library_" + tab, "/library?tab=" + tab) for tab in ("posted", "approved", "rejected")] +
                [("library_drawer", f"/library?tab=posted&video={runs[0]['ref']}")] if name == "library" else [("settings", "/settings")])
            for label, route in routes:
                for width, height in ((1440, 900), (390, 844)):
                    report.append(cdp.capture(label, width, height, route))
                    if width == 390 and label == "review_queue":
                        report.append(cdp.capture("review_queue_panel", width, height, route,
                                                  "document.querySelector('.review-panel').scrollIntoView({block:'start'})"))
                    if label == "settings":
                        report.append(cdp.capture("settings_connect_youtube", width, height, route,
                                                  "const d=document.querySelector('#accounts details');d.open=true;d.scrollIntoView()"))
                        for section in ("posting", "topics", "advanced"):
                            report.append(cdp.capture("settings_" + section, width, height, route,
                                                      f"document.querySelector('#{section}').open=true;document.querySelector('#{section}').scrollIntoView()"))
        # Exercise actual browser interaction on a throwaway snapshot, without external processes.
        runs = fake_state("interaction")
        cdp.call("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)
        cdp.visit("/")
        first = cdp.evaluate("document.querySelector('.review-grid').dataset.current")
        cdp.evaluate("document.querySelector('[data-step=\"1\"]').click()")
        cdp.until(f"document.querySelector('.review-grid').dataset.current !== '{first}'")
        cdp.evaluate("document.querySelector('[data-action=\"approve\"]').click()")
        cdp.until("document.querySelectorAll('[data-select]').length === 7")
        assert len(db.list_runs(stage="approved")) == 1
        assert len(db.list_runs(stage="awaiting_approval")) == 7
        assert "Approved" in cdp.evaluate("document.querySelector('#toast').innerText")
        assert real_state() == before, "The real database must never change during screenshot QA"
        (out / "checks.json").write_text(json.dumps({"screenshots": report, "browser_interaction": "passed", "real_db_unchanged": True}, indent=2))
        print("Browser interaction passed; real DB unchanged.", flush=True)
        cdp.call("Browser.close")
    finally:
        process.terminate()
        server.shutdown()


if __name__ == "__main__":
    main()
