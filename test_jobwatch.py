import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import jobwatch as jw

PY = sys.executable


def py(code):
    return [PY, "-c", code]


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = self._t.name

    def tearDown(self):
        self._t.cleanup()

    def run_job(self, name, command, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = jw.run_job(self.d, name, command, **kw)
        return code, err.getvalue()


class ParseTests(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(jw.parse_duration("26h"), 93600)
        self.assertEqual(jw.parse_duration("90"), 90)
        with self.assertRaises(ValueError):
            jw.parse_duration("later")

    def test_human(self):
        self.assertEqual(jw.human(45), "45s")
        self.assertEqual(jw.human(7200), "2.0h")
        self.assertEqual(jw.human(3 * 86400), "3d")

    def test_names(self):
        for ok in ("backup", "db-dump_2", "a.b", "X1"):
            self.assertEqual(jw.check_name(ok), ok)
        for bad in ("", "../x", "a/b", "-x", ".hidden", "a b", "x" * 65, "a;b", "ünï"):
            with self.assertRaises(ValueError, msg=bad):
                jw.check_name(bad)

    def test_split_command(self):
        self.assertEqual(jw.split_command(["run", "n", "--timeout", "5", "--", "ls", "-l", "--", "x"]),
                         (["run", "n", "--timeout", "5"], ["ls", "-l", "--", "x"]))
        self.assertEqual(jw.split_command(["status"]), (["status"], []))


class RunTests(Base):
    def test_success_is_recorded(self):
        code, _ = self.run_job("ok", py("pass"))
        s = jw.load(self.d, "ok")
        self.assertEqual(code, 0)
        self.assertEqual((s["runs"], s["failures"], s["last_exit"], s["running"]), (1, 0, 0, False))
        self.assertIsNotNone(s["last_success"])
        self.assertIsNone(s["pid"])

    def test_exit_code_passes_through_and_counts_failures(self):
        code, _ = self.run_job("bad", py("import sys; sys.exit(3)"))
        self.assertEqual(code, 3)
        code, _ = self.run_job("bad", py("import sys; sys.exit(4)"))
        s = jw.load(self.d, "bad")
        self.assertEqual((code, s["failures"], s["consecutive_failures"], s["last_exit"]), (4, 2, 2, 4))
        self.assertIsNone(s["last_success"])

    def test_success_resets_consecutive_failures(self):
        self.run_job("j", py("import sys; sys.exit(1)"))
        self.run_job("j", py("pass"))
        s = jw.load(self.d, "j")
        self.assertEqual((s["consecutive_failures"], s["failures"], s["runs"]), (0, 1, 2))

    def test_output_passes_through(self):
        r = subprocess.run([PY, os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobwatch.py"), "run", "echoer",
                            "--state", self.d, "--", PY, "-c", "print('hello from the job')"],
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "hello from the job"))

    def test_command_not_found(self):
        code, err = self.run_job("nf", ["/nonexistent/binary-xyz"])
        self.assertEqual(code, 127)
        self.assertIn("command not found", err)
        self.assertEqual(jw.load(self.d, "nf")["last_exit"], 127)

    def test_not_executable(self):
        p = os.path.join(self.d, "plain.txt")
        with open(p, "w") as fh:
            fh.write("x")
        code, err = self.run_job("ne", [p])
        self.assertEqual(code, 126)

    def test_timeout_kills_and_records(self):
        started = time.time()
        code, err = self.run_job("slow", py("import time; time.sleep(30)"), timeout=0.5)
        self.assertEqual(code, 124)
        self.assertLess(time.time() - started, 10)
        self.assertIn("timed out", err)
        self.assertEqual(jw.load(self.d, "slow")["last_exit"], 124)

    def test_killed_by_signal_reports_shell_style_code(self):
        code, _ = self.run_job("sig", py("import os, signal; os.kill(os.getpid(), signal.SIGKILL)"))
        self.assertEqual(code, 137)

    def test_duration_recorded(self):
        clock = iter([100.0, 107.5, 107.5, 107.5])
        self.run_job("d", py("pass"), now=lambda: next(clock))
        self.assertEqual(jw.load(self.d, "d")["last_duration"], 7.5)

    def test_state_file_is_private_and_atomic(self):
        self.run_job("p", py("pass"))
        self.assertEqual([n for n in os.listdir(self.d) if n.endswith(".tmp")], [])
        self.assertEqual(os.listdir(self.d), ["p.json"])

    def test_no_overlap_refuses_when_previous_is_alive(self):
        state = jw.new_state("busy")
        state.update(running=True, pid=os.getpid(), last_start=time.time())  # our own pid is certainly alive
        jw.save(self.d, "busy", state)
        code, err = self.run_job("busy", py("pass"), no_overlap=True)
        self.assertEqual(code, 75)
        self.assertIn("still running", err)
        self.assertEqual(jw.load(self.d, "busy")["runs"], 0)  # nothing was started or recorded

    def test_no_overlap_ignores_a_dead_previous_run(self):
        state = jw.new_state("stale-lock")
        state.update(running=True, pid=2 ** 22 + 12345, last_start=time.time() - 3600)
        jw.save(self.d, "stale-lock", state)
        code, _ = self.run_job("stale-lock", py("pass"), no_overlap=True)
        self.assertEqual(code, 0)

    def test_overlap_allowed_without_flag(self):
        state = jw.new_state("x")
        state.update(running=True, pid=os.getpid(), last_start=time.time())
        jw.save(self.d, "x", state)
        self.assertEqual(self.run_job("x", py("pass"))[0], 0)

    def test_command_is_never_run_through_a_shell(self):
        marker = os.path.join(self.d, "pwned")
        self.run_job("inj", [PY, "-c", "import sys; print(sys.argv[1])", "x; touch " + marker])
        self.assertFalse(os.path.exists(marker))

    def test_corrupt_state_file_is_replaced(self):
        with open(os.path.join(self.d, "c.json"), "w") as fh:
            fh.write("{ not json")
        self.assertEqual(self.run_job("c", py("pass"))[0], 0)
        self.assertEqual(jw.load(self.d, "c")["runs"], 1)


class EvaluateTests(unittest.TestCase):
    NOW = 1_900_000_000.0

    def state(self, **kw):
        s = jw.new_state("j")
        s.update(runs=1, last_start=self.NOW - 3700, last_end=self.NOW - 3600, last_exit=0,
                 last_success=self.NOW - 3600, last_duration=100)
        s.update(kw)
        return s

    def ev(self, state, max_age=None):
        return jw.evaluate(state, max_age, self.NOW)

    def test_never_ran(self):
        self.assertEqual(self.ev(None)[0], "never-ran")
        self.assertEqual(self.ev(jw.new_state("j"))[0], "never-ran")

    def test_ok(self):
        status, msg = self.ev(self.state(), 7200)
        self.assertEqual(status, "ok")
        self.assertIn("60m ago", msg)

    def test_stale(self):
        self.assertEqual(self.ev(self.state(), 1800)[0], "stale")
        self.assertEqual(self.ev(self.state())[0], "ok")  # no limit given

    def test_failed(self):
        status, msg = self.ev(self.state(last_exit=2, consecutive_failures=3, last_success=self.NOW - 99999), 7200)
        self.assertEqual(status, "failed")
        self.assertIn("code 2", msg)
        self.assertIn("3 failure(s) in a row", msg)

    def test_failed_even_if_an_older_success_is_recent(self):
        self.assertEqual(self.ev(self.state(last_exit=1), 99999)[0], "failed")

    def test_running_and_hung(self):
        s = self.state(running=True, pid=os.getpid(), last_start=self.NOW - 100)
        self.assertEqual(self.ev(s, 3600)[0], "running")
        s["last_start"] = self.NOW - 7200
        self.assertEqual(self.ev(s, 3600)[0], "hung")

    def test_wrapper_died(self):
        s = self.state(running=True, pid=2 ** 22 + 999, last_start=self.NOW - 500)
        status, msg = self.ev(s)
        self.assertEqual(status, "failed")
        self.assertIn("wrapper died", msg)

    def test_only_a_failure_recorded_so_far(self):
        s = jw.new_state("j")
        s.update(runs=1, last_exit=1, last_end=self.NOW - 10, consecutive_failures=1)
        self.assertEqual(self.ev(s)[0], "failed")


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = jw.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(Base):
    def run_(self, *argv):
        return run_main(*argv, "--state", self.d) if "--" not in argv else run_main(*argv[:argv.index("--")], "--state", self.d, *argv[argv.index("--"):])

    def test_run_then_check_ok(self):
        self.assertEqual(self.run_("run", "nightly", "--", PY, "-c", "pass")[0], 0)
        code, out, _ = self.run_("check", "nightly", "--max-age", "1h")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("OK"))

    def test_check_unknown_job(self):
        code, out, _ = self.run_("check", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("NEVER-RAN", out)

    def test_check_failed_job(self):
        self.run_("run", "j", "--", PY, "-c", "import sys; sys.exit(9)")
        code, out, _ = self.run_("check", "j")
        self.assertEqual(code, 1)
        self.assertIn("code 9", out)

    def test_check_stale_job(self):
        self.run_("run", "j", "--", PY, "-c", "pass")
        state = jw.load(self.d, "j")
        state["last_success"] -= 3 * 86400
        jw.save(self.d, "j", state)
        self.assertEqual(self.run_("check", "j", "--max-age", "26h")[0], 1)
        self.assertEqual(self.run_("check", "j", "--max-age", "7d")[0], 0)

    def test_check_json(self):
        self.run_("run", "j", "--", PY, "-c", "pass")
        _, out, _ = self.run_("check", "j", "--json", "--max-age", "1h")
        self.assertEqual(json.loads(out)["status"], "ok")

    def test_status_lists_all_and_exits_on_problems(self):
        self.run_("run", "good", "--", PY, "-c", "pass")
        self.run_("run", "bad", "--", PY, "-c", "import sys; sys.exit(1)")
        code, out, _ = self.run_("status")
        self.assertEqual(code, 1)
        self.assertIn("OK       good", out)
        self.assertIn("FAILED   bad", out)

    def test_status_all_healthy(self):
        self.run_("run", "good", "--", PY, "-c", "pass")
        self.assertEqual(self.run_("status", "--max-age", "1h")[0], 0)

    def test_status_json_and_empty(self):
        code, out, _ = self.run_("status")
        self.assertEqual((code, "no jobs" in out), (0, True))
        self.run_("run", "a", "--", PY, "-c", "pass")
        data = json.loads(self.run_("status", "--json")[1])
        self.assertEqual((data[0]["name"], data[0]["status"], data[0]["runs"]), ("a", "ok", 1))

    def test_forget(self):
        self.run_("run", "a", "--", PY, "-c", "pass")
        self.assertEqual(self.run_("forget", "a")[0], 0)
        self.assertIsNone(jw.load(self.d, "a"))
        self.assertEqual(self.run_("forget", "a")[0], 1)

    def test_run_without_command_is_usage_error(self):
        code, _, err = self.run_("run", "x")
        self.assertEqual(code, 2)
        self.assertIn("after --", err)

    def test_bad_arguments(self):
        self.assertEqual(self.run_("run", "../etc/passwd", "--", PY, "-c", "pass")[0], 2)
        self.assertEqual(self.run_("check", "x", "--max-age", "soon")[0], 2)
        self.assertEqual(self.run_("run", "x", "--timeout", "nope", "--", PY, "-c", "pass")[0], 2)

    def test_timeout_flag(self):
        code, _, err = self.run_("run", "slow", "--timeout", "0.3", "--", PY, "-c", "import time; time.sleep(20)")
        self.assertEqual(code, 124)

    def test_state_dir_from_environment(self):
        with mock.patch.dict(os.environ, {"JOBWATCH_DIR": os.path.join(self.d, "envdir")}):
            self.assertEqual(run_main("run", "e", "--", PY, "-c", "pass")[0], 0)
        self.assertTrue(os.path.exists(os.path.join(self.d, "envdir", "e.json")))

    def test_state_dir_is_private(self):
        target = os.path.join(self.d, "fresh")
        run_main("run", "e", "--state", target, "--", PY, "-c", "pass")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
