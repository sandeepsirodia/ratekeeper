"""ratekeeper: your browser agent is one CAPTCHA away from getting your account banned.

Per-site budgets for anything that automates a browser or an HTTP API (Playwright, browser-use, Stagehand,
Selenium, plain requests). ratekeeper paces actions like a person would, notices when a site pushes back
(429s, CAPTCHAs, "unusual activity", being logged out), backs off or waits for a human, and picks up where
it stopped after a crash or restart: all state is in one SQLite file, shared safely between processes.
It exists to *respect* limits: it never solves CAPTCHAs, rotates proxies or hides what it is.
"""
import argparse
import contextlib
import json
import math
import os
import random
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

__version__ = "0.1.0"
DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".applyloop", "ratekeeper.sqlite")
ALL = "*all*"
MINUTE, HOUR, DAY = 60.0, 3600.0, 86400.0

# Deliberately conservative: a person applying to jobs doesn't submit a form every few seconds.
DEFAULT_POLICY = {"per_minute": 4, "per_hour": 40, "per_day": 200, "min_gap": 8.0, "jitter": 0.5, "quiet": None,
                  "breaker_signals": 3, "breaker_window": 600.0, "cooldown": 1800.0, "backoff_base": 60.0, "backoff_max": 3600.0}

CAPTCHA_RE = re.compile(r"g-recaptcha|recaptcha/api|www\.google\.com/recaptcha|hcaptcha\.com|h-captcha|cf-turnstile|"
                        r"challenges\.cloudflare\.com|arkoselabs|funcaptcha|captcha-delivery|px-captcha", re.I)
SOFT_RE = re.compile(r"unusual (activity|traffic)|verify (that )?you(?:'re| are) (a )?human|are you a robot|"
                     r"too many requests|you(?:'ve| have) been (temporarily )?(blocked|rate.?limited)|access denied|"
                     r"automated (queries|requests|access)|suspicious activity|slow down", re.I)
LOGIN_RE = re.compile(r"/(login|log-in|signin|sign-in|sign_in|auth/|session/new|checkpoint|accounts/login)", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (site TEXT, ts REAL, gap REAL);
CREATE INDEX IF NOT EXISTS events_site_ts ON events(site, ts);
CREATE TABLE IF NOT EXISTS pauses (site TEXT PRIMARY KEY, until REAL, reason TEXT, needs_human INTEGER);
CREATE TABLE IF NOT EXISTS signals (site TEXT, ts REAL, kind TEXT);
CREATE TABLE IF NOT EXISTS breakers (site TEXT PRIMARY KEY, state TEXT, until REAL, cooldown REAL);
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, site TEXT, payload TEXT, done INTEGER DEFAULT 0, added REAL);
"""


def parse_duration(s):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", s.strip())
    if not m:
        raise ValueError("durations look like 90s, 30m, 2h or 1d")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


class Keeper:
    def __init__(self, path=DEFAULT_DB, policies=None, clock=time.time, sleep=time.sleep, rng=None, tz=None,
                 dry_run=False, poll=0.5, log=None):
        self.policies = {"*": dict(DEFAULT_POLICY)}
        for site, p in (policies or {}).items():
            self.policies[site] = dict(self.policies.get(site, DEFAULT_POLICY), **p)
        self.clock, self.sleep, self.rng, self.tz = clock, sleep, rng or random.Random(), tz
        self.poll, self.log, self.dry_run, self.plan = poll, log, dry_run, []
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.con = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self.con.executescript(SCHEMA)
        if dry_run:
            # Plan against a private copy: a dry run never touches the real budgets.
            mem = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
            self.con.backup(mem)
            self.con.close()
            self.con = mem
            self._vnow = clock()
            self.clock, self.sleep = (lambda: self._vnow), self._advance
        self.lock = threading.RLock()

    def _advance(self, seconds):
        self._vnow += seconds

    def close(self):
        self.con.close()

    def policy(self, site):
        return self.policies.get(site) or self.policies["*"]

    def _q(self, sql, *args):
        with self.lock:
            return self.con.execute(sql, args).fetchall()

    # -------------------------------------------------- when may the next action happen?

    def _quiet_end(self, site, t):
        q = self.policy(site).get("quiet")
        if not q:
            return None
        start_s, end_s = q.split("-")
        local = datetime.fromtimestamp(t, self.tz or datetime.now().astimezone().tzinfo)
        sh, sm = map(int, start_s.split(":"))
        eh, em = map(int, end_s.split(":"))
        start = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end = local.replace(hour=eh, minute=em, second=0, microsecond=0)
        if start <= end:
            inside = start <= local < end
        else:  # spans midnight, e.g. 23:00-07:00
            inside = local >= start or local < end
            if local >= start:
                end += timedelta(days=1)
        return end.timestamp() if inside else None

    def next_allowed(self, site, now=None):
        """The earliest time an action on `site` is allowed, math.inf while it waits for a human, or `now`."""
        now = self.clock() if now is None else now
        p = self.policy(site)
        t = now
        for s in (ALL, site):
            row = self._q("SELECT until, needs_human FROM pauses WHERE site=?", s)
            if row:
                until, human = row[0]
                if human or until is None:
                    return math.inf
                t = max(t, until)
        br = self._q("SELECT state, until FROM breakers WHERE site=?", site)
        if br:
            state, until = br[0]
            if state == "open":
                t = max(t, until)
            elif state == "trial":
                return math.inf  # one trial action is out; wait for its verdict (observe) before another
        last = self._q("SELECT ts, gap FROM events WHERE site=? ORDER BY ts DESC LIMIT 1", site)
        if last:
            t = max(t, last[0][0] + last[0][1])
        for key, window in (("per_minute", MINUTE), ("per_hour", HOUR), ("per_day", DAY)):
            limit = p.get(key)
            if not limit:
                continue
            rows = self._q("SELECT ts FROM events WHERE site=? AND ts > ? ORDER BY ts DESC LIMIT ?", site, t - window, limit)
            if len(rows) >= limit:
                t = max(t, rows[-1][0] + window)  # when the oldest of the last `limit` actions leaves the window
        q = self._quiet_end(site, t)
        if q:
            t = max(t, q + self.rng.uniform(0, p["min_gap"]))
        return t

    @contextlib.contextmanager
    def slot(self, site):
        """Blocks until an action on `site` is allowed, then records it. Use one slot per page load or submit."""
        while True:
            now = self.clock()
            t = self.next_allowed(site, now)
            if t == math.inf:
                if self.dry_run:
                    raise RuntimeError("%s is waiting for a human; a dry run can't continue past it" % site)
                self.sleep(self.poll)
                continue
            if t > now:
                self.sleep(t - now)
                continue
            if self._record(site, now):
                break
        if self.dry_run:
            self.plan.append((site, now))
        if self.log:
            self.log("%s %s" % (datetime.fromtimestamp(now, self.tz or timezone.utc).isoformat(timespec="seconds"), site))
        yield now

    def try_slot(self, site):
        """Take a slot only if one is free right now. Returns True if taken."""
        now = self.clock()
        return self.next_allowed(site, now) <= now and self._record(site, now)

    def _record(self, site, now):
        """Re-check and insert in one write transaction, so two processes can't both take the last slot."""
        p = self.policy(site)
        gap = p["min_gap"] * (1 + p["jitter"] * self.rng.random())
        with self.lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                if self.next_allowed(site, now) > now:
                    self.con.execute("ROLLBACK")
                    return False
                self.con.execute("INSERT INTO events VALUES (?,?,?)", (site, now, gap))
                br = self.con.execute("SELECT state FROM breakers WHERE site=?", (site,)).fetchone()
                if br and br[0] == "open":  # cooldown over: this is the single trial action
                    self.con.execute("UPDATE breakers SET state='trial' WHERE site=?", (site,))
                self.con.execute("COMMIT")
                return True
            except BaseException:
                self.con.execute("ROLLBACK")
                raise

    # -------------------------------------------------- noticing push-back

    def observe(self, site, text=None, url=None, status=None, headers=None, login_urls=None):
        """Call after every page or response. Returns the signal seen ('captcha', 'rate-limit', 'blocked',
        'logged-out') or None. A clean page after a breaker's trial action closes the breaker."""
        now, p = self.clock(), self.policy(site)
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        if status in (429, 503):
            ra = headers.get("retry-after")
            if ra and re.fullmatch(r"\d+", str(ra).strip()):
                wait = float(ra)
            else:
                recent = len(self._q("SELECT 1 FROM signals WHERE site=? AND kind='rate-limit' AND ts > ?", site, now - HOUR))
                wait = min(p["backoff_max"], p["backoff_base"] * 2 ** recent) * (1 + 0.25 * self.rng.random())
            self._signal(site, "rate-limit", now)
            self.pause(site, seconds=wait, reason="HTTP %s, waiting %ds" % (status, wait))
            return "rate-limit"
        body = text or ""
        if CAPTCHA_RE.search(body):
            self._signal(site, "captcha", now)
            self.pause(site, reason="CAPTCHA shown: needs a human", needs_human=True)
            return "captcha"
        if url and (LOGIN_RE.search(url) or any(u in url for u in (login_urls or []))):
            self._signal(site, "logged-out", now)
            self.pause(site, reason="redirected to a login page: log in, then resume", needs_human=True)
            return "logged-out"
        if SOFT_RE.search(body):
            self._signal(site, "blocked", now)
            self._trip(site, now)
            return "blocked"
        br = self._q("SELECT state, cooldown FROM breakers WHERE site=?", site)
        if br and br[0][0] == "trial":
            with self.lock:
                self.con.execute("DELETE FROM breakers WHERE site=?", (site,))
            self._say("%s: trial action went through; breaker closed" % site)
        return None

    def _signal(self, site, kind, now):
        with self.lock:
            self.con.execute("INSERT INTO signals VALUES (?,?,?)", (site, now, kind))
        self._say("%s: %s" % (site, kind))

    def _trip(self, site, now):
        p = self.policy(site)
        br = self._q("SELECT state, cooldown FROM breakers WHERE site=?", site)
        if br and br[0][0] == "trial":  # the trial failed: open again, for twice as long
            cooldown = min(br[0][1] * 2, 7 * DAY)
        else:
            n = len(self._q("SELECT 1 FROM signals WHERE site=? AND kind='blocked' AND ts > ?", site, now - p["breaker_window"]))
            if n < p["breaker_signals"]:
                return
            cooldown = p["cooldown"]
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO breakers VALUES (?,?,?,?)", (site, "open", now + cooldown, cooldown))
        self._say("%s: breaker open for %dm" % (site, cooldown // 60))

    def _say(self, msg):
        if self.log:
            self.log(msg)

    # -------------------------------------------------- controls

    def pause(self, site=ALL, seconds=None, reason="paused by you", needs_human=False):
        site = ALL if site == "all" else site
        until = None if seconds is None else self.clock() + seconds
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO pauses VALUES (?,?,?,?)", (site, until, reason, int(needs_human)))

    def resume(self, site=ALL):
        site = ALL if site == "all" else site
        with self.lock:
            if site == ALL:
                self.con.execute("DELETE FROM pauses WHERE site=?", (ALL,))
            else:
                self.con.execute("DELETE FROM pauses WHERE site=?", (site,))
                self.con.execute("DELETE FROM breakers WHERE site=?", (site,))

    def status(self):
        now = self.clock()
        sites = {r[0] for r in self._q("SELECT DISTINCT site FROM events")} | {r[0] for r in self._q("SELECT site FROM pauses")} | \
                {r[0] for r in self._q("SELECT site FROM breakers")}
        out = {}
        for site in sorted(sites - {ALL}):
            p = self.policy(site)
            used = {k: self._q("SELECT COUNT(*) FROM events WHERE site=? AND ts > ?", site, now - w)[0][0]
                    for k, w in (("per_minute", MINUTE), ("per_hour", HOUR), ("per_day", DAY))}
            pause = self._q("SELECT until, reason, needs_human FROM pauses WHERE site=?", site)
            br = self._q("SELECT state, until FROM breakers WHERE site=?", site)
            out[site] = {"used": used, "limits": {k: p.get(k) for k in used},
                         "paused": {"until": pause[0][0], "reason": pause[0][1], "needs_human": bool(pause[0][2])} if pause else None,
                         "breaker": {"state": br[0][0], "until": br[0][1]} if br else None,
                         "next_allowed": self.next_allowed(site, now)}
        allp = self._q("SELECT until, reason FROM pauses WHERE site=?", ALL)
        return {"all_paused": {"until": allp[0][0], "reason": allp[0][1]} if allp else None, "sites": out}

    # -------------------------------------------------- work queue with checkpoints

    def add(self, task_id, site, payload=None):
        """Idempotent: re-adding a known task (done or not) changes nothing."""
        with self.lock:
            self.con.execute("INSERT OR IGNORE INTO tasks VALUES (?,?,?,0,?)", (task_id, site, json.dumps(payload), self.clock()))

    def done(self, task_id):
        with self.lock:
            self.con.execute("UPDATE tasks SET done=1 WHERE id=?", (task_id,))

    def pending(self, site=None):
        rows = self._q("SELECT id, site, payload FROM tasks WHERE done=0 %s ORDER BY added, id" % ("AND site=?" if site else ""),
                       *([site] if site else []))
        return [{"id": r[0], "site": r[1], "payload": json.loads(r[2])} for r in rows]

    def drain(self, site, handler, on_idle=None):
        """Run `handler(task)` for each unfinished task on `site`, one slot each, marking each done as it
        succeeds. When the queue is empty with budget left, `on_idle(site, remaining_today)` is called so the
        caller can find more work. In a dry run, nothing is executed: the schedule is logged in `plan`."""
        for task in self.pending(site):
            with self.slot(site):
                if not self.dry_run:
                    handler(task)
                    self.done(task["id"])
        used = self._q("SELECT COUNT(*) FROM events WHERE site=? AND ts > ?", site, self.clock() - DAY)[0][0]
        remaining = (self.policy(site).get("per_day") or 0) - used
        if on_idle and remaining > 0:
            on_idle(site, remaining)
        return remaining


# ------------------------------------------------------------------ CLI

def load_policies(path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def main(argv=None, out=None):
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="ratekeeper", description="Per-site budgets and push-back detection for browser agents.")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--policies", default=os.path.join(os.path.expanduser("~"), ".applyloop", "ratekeeper.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("pause")
    p.add_argument("site", help="a site, or 'all'")
    p.add_argument("--for", dest="duration", help="e.g. 30m, 2h (default: until you resume)")
    r = sub.add_parser("resume")
    r.add_argument("site", help="a site, or 'all'")
    a = ap.parse_args(argv)
    k = Keeper(a.db, load_policies(a.policies))
    try:
        site = getattr(a, "site", None)
        if a.cmd == "pause":
            k.pause(site, parse_duration(a.duration) if a.duration else None)
            out.write("Paused %s%s\n" % (a.site, " for " + a.duration if a.duration else " until you resume"))
        elif a.cmd == "resume":
            k.resume(site)
            out.write("Resumed %s\n" % a.site)
        else:
            st = k.status()
            if st["all_paused"]:
                out.write("ALL SITES PAUSED: %s\n" % st["all_paused"]["reason"])
            if not st["sites"]:
                out.write("No activity yet.\n")
            now = time.time()
            for s, v in st["sites"].items():
                used = " · ".join("%d/%s %s" % (v["used"][k_], v["limits"][k_] or "∞", k_.split("_")[1]) for k_ in v["used"])
                state = ""
                if v["paused"]:
                    state = "  ⏸ %s" % v["paused"]["reason"]
                elif v["breaker"]:
                    state = "  ⛔ breaker %s" % v["breaker"]["state"]
                wait = v["next_allowed"] - now
                nxt = "waiting for you" if wait == math.inf else ("now" if wait <= 0 else "in %ds" % wait)
                out.write("%-28s %s   next: %s%s\n" % (s, used, nxt, state))
    finally:
        k.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
