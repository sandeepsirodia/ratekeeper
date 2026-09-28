"""Pace plain HTTP calls and honour 429 / Retry-After. Standard library only."""
import urllib.error
import urllib.request

from ratekeeper import Keeper

keeper = Keeper(policies={"api.example.com": {"per_minute": 30, "min_gap": 1}})
for n in range(100):
    with keeper.slot("api.example.com"):
        try:
            with urllib.request.urlopen("https://api.example.com/items/%d" % n) as r:
                keeper.observe("api.example.com", status=r.status)
        except urllib.error.HTTPError as e:
            keeper.observe("api.example.com", status=e.code, headers=dict(e.headers))   # 429 → site paused for Retry-After
