"""Tests map 1:1 to SPEC.md (E1..E10). Timing uses an injected fake clock: no real sleeps, except where a
real second thread or process is the point of the test."""
import io
import math
import multiprocessing
import os
import random
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ratekeeper as rk  # noqa: E402

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc).timestamp()  # noon UTC


class FakeClock:
    def __init__(self, t=T0):
        self.t, self.slept = t, []

    def __call__(self):
        return self.t

    def sleep(self, d):
        self.slept.append(d)
        self.t += d


def keeper(path=None, clock=None, **policy):
    clock = clock or FakeClock()
    p = {"per_minute": None, "per_hour": None, "per_day": None, "min_gap": 0.0, "jitter": 0.0}
    p.update(policy)
    path = path or os.path.join(tempfile.mkdtemp(prefix="ratekeeper-"), "rk.sqlite")
    return rk.Keeper(path, {"site.test": p}, clock=clock, sleep=clock.sleep, rng=random.Random(1), tz=timezone.utc), clock


def _grab(path, n, out):
    k = rk.Keeper(path, {"shared.test": {"per_minute": 5, "min_gap": 0.0, "jitter": 0.0}})
    out.put(sum(k.try_slot("shared.test") for _ in range(n)))
    k.close()


class TestPacing(unittest.TestCase):
    def test_e1_budget_and_jittered_gaps(self):
        k, c = keeper(per_minute=3, min_gap=5.0, jitter=0.5)
        times = []
        for _ in range(5):
            with k.slot("site.test") as t:
                times.append(t - T0)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(5.0 <= g for g in gaps[:2]), gaps)
        self.assertTrue(all(g <= 7.5 for g in gaps[:2]), gaps)
        self.assertGreaterEqual(times[3] - times[0], 60.0, "the 4th action waits until the 1st leaves the minute window")
        self.assertGreaterEqual(times[4] - times[1], 60.0)
        k.close()

    def test_e2_quiet_hours(self):
        c = FakeClock(datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc).timestamp())
        k, _ = keeper(clock=c, quiet="00:00-07:00", min_gap=10.0)
        with k.slot("site.test") as t:
            local = datetime.fromtimestamp(t, timezone.utc)
        self.assertEqual((local.hour, local.date().day), (7, 28))
        self.assertLessEqual(local.minute * 60 + local.second, 10)
        late = FakeClock(datetime(2026, 9, 28, 23, 30, tzinfo=timezone.utc).timestamp())
        k2, _ = keeper(clock=late, quiet="23:00-07:00")
        with k2.slot("site.test") as t:
            self.assertEqual(datetime.fromtimestamp(t, timezone.utc).strftime("%d %H"), "29 07")
        k.close()
        k2.close()

    def test_e9_dry_run_plans_without_touching_the_budget(self):
        k, c = keeper(per_minute=2, min_gap=1.0)
        path = k.path
        k.close()
        dry = rk.Keeper(path, {"site.test": {"per_minute": 2, "min_gap": 1.0, "jitter": 0.0}}, clock=c, dry_run=True, tz=timezone.utc)
        ran = []
        for i in range(3):
            dry.add("t%d" % i, "site.test")
        dry.drain("site.test", ran.append)
        self.assertEqual(ran, [])
        self.assertEqual([round(t - T0) for _, t in dry.plan], [0, 1, 60])
        self.assertEqual(c.slept, [], "a dry run never really sleeps")
        dry.close()
        real = rk.Keeper(path, clock=c)
        self.assertEqual(real.pending(), [], "tasks added in a dry run aren't in the real queue")
        self.assertEqual(real.status()["sites"], {})
        real.close()


class TestPushBack(unittest.TestCase):
    def test_e3_retry_after_pauses_only_that_site(self):
        k, c = keeper()
        self.assertEqual(k.observe("site.test", status=429, headers={"Retry-After": "120"}), "rate-limit")
        self.assertEqual(k.next_allowed("site.test"), T0 + 120)
        self.assertEqual(k.next_allowed("other.test"), T0)
        k.close()

    def test_e4_captcha_waits_for_a_human(self):
        k, c = keeper()
        page = '<div class="g-recaptcha" data-sitekey="x"></div>'
        self.assertEqual(k.observe("site.test", text=page), "captcha")
        self.assertEqual(k.next_allowed("site.test"), math.inf)
        st = k.status()["sites"]["site.test"]["paused"]
        self.assertEqual((st["needs_human"], st["reason"]), (True, "CAPTCHA shown: needs a human"))
        k.resume("site.test")
        self.assertEqual(k.next_allowed("site.test"), T0)
        k.close()

    def test_logged_out_mid_flow_waits_for_a_human(self):
        k, _ = keeper()
        self.assertEqual(k.observe("site.test", url="https://site.test/accounts/login?next=/apply"), "logged-out")
        self.assertEqual(k.next_allowed("site.test"), math.inf)
        k.close()

    def test_e5_breaker_opens_then_one_trial(self):
        k, c = keeper(breaker_signals=3, breaker_window=600.0, cooldown=900.0)
        for _ in range(2):
            k.observe("site.test", text="We detected unusual activity from your account")
            c.t += 60
        self.assertEqual(k.next_allowed("site.test"), c.t, "2 soft signals: still closed")
        k.observe("site.test", text="Please verify you are a human")
        self.assertEqual(k.status()["sites"]["site.test"]["breaker"]["state"], "open")
        self.assertEqual(k.next_allowed("site.test"), c.t + 900)
        c.t += 900
        self.assertTrue(k.try_slot("site.test"), "cooldown over: one trial action")
        self.assertEqual(k.next_allowed("site.test"), math.inf, "no second action until the trial's page is observed")
        k.observe("site.test", text="Too many requests")
        self.assertEqual(k.next_allowed("site.test"), c.t + 1800, "a failed trial reopens for twice as long")
        c.t += 1800
        self.assertTrue(k.try_slot("site.test"))
        self.assertIsNone(k.observe("site.test", text="<h1>Apply for Backend Engineer</h1>"))
        self.assertIsNone(k.status()["sites"]["site.test"]["breaker"], "a clean page after the trial closes it")
        k.close()

    def test_backoff_without_retry_after_grows(self):
        k, c = keeper(backoff_base=60.0)
        k.observe("site.test", status=429)
        first = k.next_allowed("site.test") - c.t
        c.t += first
        k.observe("site.test", status=429)
        second = k.next_allowed("site.test") - c.t
        self.assertTrue(60 <= first <= 75 and 120 <= second <= 150, (first, second))
        k.close()


class TestPersistenceAndControl(unittest.TestCase):
    def test_e6_restart_keeps_state_and_unfinished_tasks(self):
        k, c = keeper(per_hour=10)
        path = k.path
        for i in range(4):
            k.add("job-%d" % i, "site.test", {"n": i})
        with k.slot("site.test"):
            pass
        k.done("job-0")
        k.observe("site.test", text="unusual activity")
        k.pause("other.test", seconds=300)
        k.close()  # the process dies here
        k2 = rk.Keeper(path, {"site.test": {"per_hour": 10}}, clock=c)
        self.assertEqual([t["id"] for t in k2.pending()], ["job-1", "job-2", "job-3"])
        k2.add("job-0", "site.test")
        self.assertEqual(len(k2.pending()), 3, "re-adding a finished task doesn't resurrect it")
        st = k2.status()["sites"]
        self.assertEqual(st["site.test"]["used"]["per_hour"], 1)
        self.assertEqual(st["other.test"]["paused"]["until"], T0 + 300)
        k2.close()

    def test_e7_on_idle_reports_budget_left(self):
        k, _ = keeper(per_day=40)
        for i in range(10):
            k.add("job-%d" % i, "site.test")
        idle = []
        k.drain("site.test", lambda task: None, on_idle=lambda site, remaining: idle.append((site, remaining)))
        self.assertEqual(idle, [("site.test", 30)])
        self.assertEqual(k.pending("site.test"), [])
        k.close()

    def test_e8_pause_all_from_another_thread(self):
        path = os.path.join(tempfile.mkdtemp(prefix="ratekeeper-"), "rk.sqlite")
        k = rk.Keeper(path, {"site.test": {"min_gap": 0.0, "jitter": 0.0}}, poll=0.01)
        k.pause("all")
        got = []
        started = time.monotonic()

        def worker():
            with k.slot("site.test"):
                got.append(time.monotonic() - started)
        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.2)
        self.assertEqual(got, [], "blocked while everything is paused")
        k.resume("all")
        t.join(5)
        self.assertEqual(len(got), 1)
        self.assertGreaterEqual(got[0], 0.2)
        k.close()

    def test_e10_two_processes_share_one_budget(self):
        path = os.path.join(tempfile.mkdtemp(prefix="ratekeeper-"), "rk.sqlite")
        rk.Keeper(path).close()
        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        procs = [ctx.Process(target=_grab, args=(path, 5, q)) for _ in range(2)]
        for p in procs:
            p.start()
        taken = [q.get(timeout=30) for _ in procs]
        for p in procs:
            p.join(30)
        self.assertEqual(sum(taken), 5, taken)

    def test_cli_pause_status_resume(self):
        path = os.path.join(tempfile.mkdtemp(prefix="ratekeeper-"), "rk.sqlite")
        out = io.StringIO()
        rk.main(["--db", path, "pause", "greenhouse.io", "--for", "30m"], out)
        rk.main(["--db", path, "pause", "all"], out)
        rk.main(["--db", path, "status"], out)
        self.assertIn("ALL SITES PAUSED", out.getvalue())
        self.assertRegex(out.getvalue(), r"greenhouse\.io .*next: waiting for you  ⏸ paused by you")
        rk.main(["--db", path, "resume", "all"], out)
        self.assertIn("Resumed all", out.getvalue())


if __name__ == "__main__":
    unittest.main()
