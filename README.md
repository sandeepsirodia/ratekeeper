<h1 align="center">ratekeeper</h1>

<p align="center">
  <em>Your browser agent is one CAPTCHA away from getting your account banned.</em>
</p>

<p align="center">
  <a href="https://github.com/sandeepsirodia/ratekeeper/actions/workflows/ci.yml"><img src="https://github.com/sandeepsirodia/ratekeeper/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/dependencies-0-111111?style=flat-square" alt="Zero dependencies">
  <img src="https://img.shields.io/badge/solves%20CAPTCHAs-never-111111?style=flat-square" alt="Never solves CAPTCHAs">
  <img src="https://img.shields.io/badge/license-MIT-111111?style=flat-square" alt="MIT">
</p>

---

Browser agents are fast. That's the problem. A script that opens forty job postings a minute from your logged-in account looks exactly like what it is, and the site's answer is a CAPTCHA, then an "unusual activity" page, then a locked account.

Rate limiters count requests. **ratekeeper also watches what the site says back**, and stops before the site stops you:

```python
from ratekeeper import Keeper

keeper = Keeper(policies={"greenhouse.io": {"per_minute": 3, "per_day": 60, "min_gap": 10, "quiet": "23:00-07:00"}})

with keeper.slot("greenhouse.io"):          # waits until a human-like pace allows it
    page.goto(url)
signal = keeper.observe("greenhouse.io", text=page.content(), url=page.url)
# → None, or "captcha" / "blocked" / "rate-limit" / "logged-out", and that site is now paused
```

| The site says | ratekeeper does |
|---|---|
| `429` / `503` with `Retry-After: 120` | Pauses that site for exactly 120 s. Other sites keep going. |
| `429` without `Retry-After` | Exponential backoff with jitter (1 min, 2 min, 4 min… up to 1 h). |
| A CAPTCHA (reCAPTCHA, hCaptcha, Turnstile, Arkose…) | **Pauses the site until a human resumes it.** It never tries to solve one. |
| A redirect to a login page mid-flow | Pauses the site until you log in and resume. |
| "Unusual activity", "verify you are human", "access denied"… | Counts it. At 3 such signals in 10 minutes, a **circuit breaker** opens for 30 minutes, then allows exactly one trial action. If the trial page is clean the breaker closes; if not, it reopens for twice as long. |

## Pacing like a person

- **Budgets per site:** per minute, per hour and per day.
- **A minimum gap with random jitter,** so actions don't land every exactly 8.000 seconds.
- **Quiet hours,** e.g. nothing between 23:00 and 07:00 local time.
- **Conservative defaults** for any site you haven't configured: 4 a minute, 40 an hour, 200 a day, at least 8 s apart.

## Survives crashes and restarts

Everything lives in one SQLite file (`~/.applyloop/ratekeeper.sqlite`): action history, pauses, open breakers and a work queue with checkpoints.

```python
for url in urls:
    keeper.add(url, "greenhouse.io", {"url": url})   # idempotent: re-adding a finished task does nothing
keeper.drain("greenhouse.io", visit,                 # one slot per task, each marked done as it succeeds
             on_idle=lambda site, left: print(f"queue empty, {left} actions left today"))
```

Kill the process halfway through, start it again, and only the unfinished tasks run, against the same budgets. Two processes sharing the file share the budget too, because every slot is taken inside an SQLite write transaction. There's a test that runs two processes against one per-minute limit of 5: exactly 5 slots get taken.

**Under budget?** When a site's queue empties with budget left, `on_idle(site, remaining)` fires, so your agent can go and find more work instead of stopping early.

## Control it from the terminal

```console
$ ratekeeper status
greenhouse.io                2/3 minute · 14/30 hour · 41/60 day   next: in 9s
jobs.lever.co                0/4 minute · 3/40 hour · 3/200 day   next: waiting for you  ⏸ CAPTCHA shown: needs a human
$ ratekeeper resume jobs.lever.co      # after you've solved it yourself in the browser
$ ratekeeper pause all --for 2h
```

`Keeper(dry_run=True)` plans a whole queue against a copy of the state, with a virtual clock. It shows the schedule it *would* follow without touching the real budgets or sleeping.

## Try it

```bash
pip install ratekeeper      # standard library only
```

See `examples/` for [Playwright](examples/playwright_example.py) and [plain HTTP](examples/http_example.py). browser-use, Stagehand or Selenium work the same way: take a slot before each step, observe the page after it.

## Prior art

- **[PyrateLimiter](https://github.com/vutran1710/PyrateLimiter), [limits](https://github.com/alisaifee/limits):** excellent request-rate limiters. They count; they don't read the page, so they can't see a CAPTCHA or a block page.
- **[tenacity](https://github.com/jd/tenacity):** retries a failing call with backoff. ratekeeper schedules actions across sites and processes, and knows when *not* to retry.
- **[pybreaker](https://github.com/danielfm/pybreaker):** a circuit breaker whose state lives in memory or Redis. ratekeeper's breakers have to survive a restart with nothing but a file.
- **Scrapy's AutoThrottle** adapts to response latency inside Scrapy. ratekeeper is framework-agnostic and made for logged-in browser sessions.

## Won't do

Solve CAPTCHAs, rotate proxies, spoof fingerprints, or anything else meant to get around a site's defences. ratekeeper exists to respect limits. If a site says stop, it stops.

## Honest limits

- **Detection is pattern-based.** A site with its own wording for "we think you're a bot" needs its phrase added (PRs welcome), and a false positive pauses a site that was fine. That's the safer mistake.
- **Budgets are per site name you choose.** Use the same name for the same site everywhere.
- **Quiet hours use your machine's time zone** unless you pass `tz`.

## License

MIT
