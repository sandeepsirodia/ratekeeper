# ratekeeper — SPEC

> Your browser agent is one CAPTCHA away from getting your account banned.

Per-site budgets for anything that automates a browser or an HTTP API: browser-use, Playwright, Stagehand, Selenium, or a plain `requests` loop. ratekeeper paces actions like a person would, notices when a site starts pushing back, backs off, and picks up where it stopped after a crash or a restart. When a run is *under* its budget, it reports that so the caller can find more work.

## The hook (README opens with this)
A GIF: an agent working through a queue; the site shows a CAPTCHA; ratekeeper pauses that site for 45 minutes, keeps working on other sites, and resumes the first one later. Caption: "It stopped before the site stopped it."

It's generic, so its audience is every browser-agent user, not just job seekers.

## Must have (v1)
1. **Budgets per site:** per minute, per hour and per day, plus quiet hours (e.g. no actions 00:00–07:00 local) and a minimum gap with random jitter. Budgets live in a small JSON/TOML file; defaults are deliberately conservative.
2. **`with keeper.slot("greenhouse.io"):`** blocks until an action is allowed. An async variant is included.
3. **Push-back detection:**
   - HTTP 429 and 503, and `Retry-After`
   - page signals, via `keeper.observe(site, html_or_text, url)`: CAPTCHA widgets (reCAPTCHA, hCaptcha, Turnstile), "unusual activity", "verify you are human", "too many requests", and a redirect to a login page mid-flow
   - each signal has its own response: honour `Retry-After`; exponential backoff with jitter; a **circuit breaker** that opens the site after N signals in a window; and "needs a human" for CAPTCHAs, which pauses the site and never tries to solve them
4. **Persistence:** state in SQLite (`~/.applyloop/ratekeeper.sqlite`), so a restart keeps counts, open breakers and pause timers. The work queue has checkpoints: `keeper.done(task_id)`, and on restart only unfinished tasks are returned.
5. **Under budget:** `keeper.status()` reports each site's used vs allowed, and an `on_idle(site, remaining)` hook fires when the queue for a site empties with budget left. applyloop uses this to ask whatshiring for more roles, within limits.
6. **Controls:** `pause(site | all)`, `resume(...)`, and a dry-run mode that logs what it would do. These are exposed to applyloop's UI.
7. **Examples:** Playwright and plain HTTP in `examples/` (browser-use and Stagehand work the same way: take a slot before each step, observe after it).
8. **Stdlib only.** Checked before building: PyrateLimiter 4.5 could handle the windows, but pybreaker keeps breaker state only in memory or Redis, and ratekeeper must survive restarts with nothing but SQLite. The windows are ~20 lines over SQLite, and SQLite transactions give cross-process safety.

## Won't do (v1)
- Solving CAPTCHAs, rotating proxies, spoofing fingerprints or anything else meant to evade a site's defences. ratekeeper exists to *respect* limits, and the README says so.

## Expectations → test cases
All timing tests use an injected fake clock; no real sleeps.

| ID | Given | Then |
|---|---|---|
| E1 | Budget 3/minute, 5 calls | Calls 4 and 5 wait until the window allows them; gaps include jitter within the bounds |
| E2 | Quiet hours 00:00–07:00, a call at 02:00 | Waits until 07:00 plus jitter |
| E3 | `observe()` sees a 429 with `Retry-After: 120` | That site pauses 120 s; other sites continue |
| E4 | A reCAPTCHA iframe in the page | The site is paused as "needs a human"; the event is emitted |
| E5 | 3 soft signals in 10 minutes (breaker N=3) | The breaker opens for the cooldown, then half-opens with a single trial call |
| E6 | Process killed mid-queue; restarted | Counts, pauses and breakers persist; only unfinished tasks come back |
| E7 | Queue empties with 30 of 40 daily actions left | `on_idle` fires with `remaining=30` |
| E8 | `pause("all")` from another thread | Every `slot()` blocks until `resume` |
| E9 | Dry-run | No action executes; the log shows the schedule |
| E10 | Two processes, one state file | Budgets hold across both (SQLite transactions) |

## Done when
E1–E10 pass, and the README GIF shows a real (local test site) CAPTCHA pause and resume.
