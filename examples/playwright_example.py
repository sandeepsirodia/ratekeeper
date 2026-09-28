"""Pace a Playwright crawl and stop when the site pushes back. pip install playwright ratekeeper"""
from playwright.sync_api import sync_playwright

from ratekeeper import Keeper

keeper = Keeper(policies={"jobs.example.com": {"per_minute": 3, "per_hour": 30, "min_gap": 10, "quiet": "23:00-07:00"}})
urls = ["https://jobs.example.com/postings/%d" % i for i in range(20)]
for u in urls:
    keeper.add(u, "jobs.example.com", {"url": u})     # idempotent: safe to re-run after a crash

with sync_playwright() as p:
    page = p.chromium.launch().new_page()

    def visit(task):
        page.goto(task["payload"]["url"])
        signal = keeper.observe("jobs.example.com", text=page.content(), url=page.url)
        if signal:                                     # CAPTCHA, block page or logout: the site is now paused
            raise SystemExit("stopping: %s (see `ratekeeper status`)" % signal)

    keeper.drain("jobs.example.com", visit,
                 on_idle=lambda site, left: print("queue empty with %d actions left today" % left))
