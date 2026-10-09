"""`python run.py go-live`: take Instagram and Facebook out of test mode, safely.

1. Run the same read-only checks as the Settings page Test buttons, for Instagram and Facebook
   (Facebook is checked even while it is off). Nothing is posted.
2. Both fail: change nothing. Otherwise, BEFORE test mode goes off, every video that was approved
   or queued for posting while in test mode goes back to review (review.reset_test_approvals), so
   nothing posts live without a fresh approval.
3. Turn on the platforms that passed (test mode off for them), leave a failed one off, and turn
   YouTube off (its credentials and connection stay).
"""
from __future__ import annotations

PLATFORMS = ("instagram", "facebook")


def passed(result: dict) -> bool:
    return result.get("state") == "ok" and not result.get("missing")


def check() -> dict[str, dict]:
    from . import actions
    return {p: actions.test_account(p, force=True) for p in PLATFORMS}


def run(apply: bool = True) -> dict:
    from . import review, settings
    checks = check()
    good = [p for p in PLATFORMS if passed(checks[p])]
    report = {"checks": checks, "passed": good, "failed": [p for p in PLATFORMS if p not in good],
              "reset": [], "changed": False}
    if not good or not apply:
        return report
    report["reset"] = review.reset_test_approvals("go-live")
    settings.go_live(good)
    report["changed"] = True
    return report


def print_report(report: dict) -> None:
    for p in PLATFORMS:
        c = report["checks"][p]
        print(f"{p}: {'PASS' if p in report['passed'] else 'FAIL'} ({c.get('state')}) {c.get('detail')}")
    if not report["changed"]:
        print("No change: test mode and platforms are as they were.")
        return
    print(f"Live now: {', '.join(report['passed'])}. Off: youtube"
          + "".join(f", {p}" for p in report["failed"]) + ". Test mode is off for the live platforms.")
    print(f"Back to review ({len(report['reset'])}):")
    for r in report["reset"]:
        print(f"  {r['id']}  {r['topic_id']}  was {r['was']}  \"{r['title']}\"")
