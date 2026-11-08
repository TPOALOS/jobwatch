#!/usr/bin/env python3
"""jobwatch - find out when a cron job silently stops running.

Wrap the command:    jobwatch run nightly-backup -- /usr/local/bin/backup.sh
Then ask later:      jobwatch check nightly-backup --max-age 26h      (exit 1 = problem)
                     jobwatch status                                   (every job at a glance)

`run` passes the command's output and exit code straight through and records when it
started, finished and how it ended in a small JSON file. `check` fails when the job never
ran, last failed, is stuck, or has not succeeded recently, so a cron job or monitor can
alert you. Nothing is sent anywhere. Standard library only.
Exit codes: run = the command's own; check/status = 0 healthy, 1 problem; 2 usage error.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

__version__ = "1.0.0"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TIME_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
EXIT_TIMEOUT, EXIT_NOT_FOUND, EXIT_NOT_RUNNABLE, EXIT_INTERRUPTED, EXIT_OVERLAP = 124, 127, 126, 130, 75


def parse_duration(text):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*", str(text).lower())
    if not m:
        raise ValueError("not a duration: %r (use e.g. 90m, 26h, 2d)" % (text,))
    return float(m.group(1)) * TIME_UNITS[m.group(2) or "s"]


def human(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "%ds" % seconds
    if seconds < 5400:
        return "%dm" % round(seconds / 60)
    if seconds < 172800:
        return "%.1fh" % (seconds / 3600)
    return "%dd" % (seconds // 86400)


def state_dir(explicit=None):
    path = explicit or os.environ.get("JOBWATCH_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state"), "jobwatch")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def check_name(name):
    if not NAME_RE.match(name):
        raise ValueError("job names may use letters, digits, '.', '_' and '-' (max 64): %r" % name)
    return name


def state_path(directory, name):
    return os.path.join(directory, check_name(name) + ".json")


def load(directory, name):
    try:
        with open(state_path(directory, name), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save(directory, name, data):
    """Write atomically so a crash can never leave a half-written state file."""
    fd, tmp = tempfile.mkstemp(prefix=".%s." % name, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, state_path(directory, name))
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def new_state(name):
    return {"name": name, "runs": 0, "failures": 0, "consecutive_failures": 0, "running": False, "pid": None,
            "last_start": None, "last_end": None, "last_exit": None, "last_duration": None, "last_success": None}


def run_job(directory, name, command, timeout=None, no_overlap=False, now=time.time):
    """Run ``command`` (argument list, never a shell string) and record the outcome; returns its exit code."""
    state = load(directory, name) or new_state(name)
    if no_overlap and state.get("running") and pid_alive(state.get("pid")):
        print("jobwatch: %s is still running (pid %s since %s); not starting another" % (
            name, state.get("pid"), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state.get("last_start") or 0))),
            file=sys.stderr)
        return EXIT_OVERLAP
    started = now()
    state.update(running=True, pid=os.getpid(), last_start=started)
    save(directory, name, state)
    try:
        proc = subprocess.Popen(command)
    except FileNotFoundError:
        code = EXIT_NOT_FOUND
        print("jobwatch: command not found: %s" % command[0], file=sys.stderr)
    except OSError as exc:
        code = EXIT_NOT_RUNNABLE
        print("jobwatch: cannot run %s: %s" % (command[0], exc.strerror or exc), file=sys.stderr)
    else:
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            code = EXIT_TIMEOUT
            print("jobwatch: %s timed out after %s and was killed" % (name, human(timeout)), file=sys.stderr)
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            code = EXIT_INTERRUPTED
        else:
            if code < 0:  # killed by a signal: report like a shell does
                code = 128 - code
    ended = now()
    state = load(directory, name) or new_state(name)
    state.update(running=False, pid=None, last_start=started, last_end=ended, last_exit=code,
                 last_duration=round(ended - started, 3), runs=state.get("runs", 0) + 1)
    if code == 0:
        state.update(last_success=ended, consecutive_failures=0)
    else:
        state.update(failures=state.get("failures", 0) + 1, consecutive_failures=state.get("consecutive_failures", 0) + 1)
    save(directory, name, state)
    return code


def evaluate(state, max_age=None, now=None):
    """(status, message) for one job's state. status: ok / never-ran / failed / stale / hung / running."""
    now = time.time() if now is None else now
    if state is None or not state.get("runs") and not state.get("running"):
        return "never-ran", "no run has been recorded"
    if state.get("running") and pid_alive(state.get("pid")):
        age = now - (state.get("last_start") or now)
        if max_age is not None and age > max_age:
            return "hung", "still running after %s (limit %s)" % (human(age), human(max_age))
        return "running", "running for %s" % human(age)
    if state.get("running"):
        return "failed", "started %s ago but the wrapper died before the job finished" % human(now - (state.get("last_start") or now))
    if state.get("last_exit") != 0:
        return "failed", "last run exited with code %s %s ago (%d failure(s) in a row)" % (
            state.get("last_exit"), human(now - (state.get("last_end") or now)), state.get("consecutive_failures", 1))
    age = now - (state.get("last_success") or 0)
    if max_age is not None and age > max_age:
        return "stale", "last success was %s ago (limit %s)" % (human(age), human(max_age))
    return "ok", "last success %s ago, took %s" % (human(age), human(state.get("last_duration") or 0))


def all_jobs(directory):
    jobs = []
    for fname in sorted(os.listdir(directory)):
        if fname.endswith(".json") and not fname.startswith("."):
            name = fname[:-5]
            if NAME_RE.match(name):
                jobs.append((name, load(directory, name)))
    return jobs


def split_command(argv):
    """Separate our options from the wrapped command at the first `--`."""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def build_parser():
    ap = argparse.ArgumentParser(prog="jobwatch", description="Notice when a cron job silently stops running.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", help="state folder (default: $JOBWATCH_DIR or ~/.local/state/jobwatch)")
    r = sub.add_parser("run", parents=[common], help="run a command and record the outcome (put the command after --)")
    r.add_argument("name")
    r.add_argument("--timeout", help="kill the command after this long, e.g. 30m (exit 124)")
    r.add_argument("--no-overlap", action="store_true", help="do not start if the previous run is still going (exit 75)")
    c = sub.add_parser("check", parents=[common], help="exit 1 if the job is unhealthy")
    c.add_argument("name")
    c.add_argument("--max-age", help="last success must be younger than this, e.g. 26h")
    c.add_argument("--json", action="store_true")
    s = sub.add_parser("status", parents=[common], help="show every job")
    s.add_argument("--max-age", help="mark jobs without a success this recent as stale (exit 1 if any problem)")
    s.add_argument("--json", action="store_true")
    f = sub.add_parser("forget", parents=[common], help="delete the record of a job")
    f.add_argument("name")
    return ap


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    ours, command = split_command(argv)
    args = build_parser().parse_args(ours)
    try:
        directory = state_dir(args.state)
        if args.cmd == "run":
            if not command:
                print("jobwatch: put the command after --, e.g. jobwatch run %s -- ./backup.sh" % args.name, file=sys.stderr)
                return 2
            timeout = parse_duration(args.timeout) if args.timeout else None
            return run_job(directory, check_name(args.name), command, timeout, args.no_overlap)
        max_age = parse_duration(args.max_age) if getattr(args, "max_age", None) else None
        if args.cmd == "forget":
            path = state_path(directory, args.name)
            if not os.path.exists(path):
                print("jobwatch: no record of %s" % args.name, file=sys.stderr)
                return 1
            os.remove(path)
            print("forgot %s" % args.name)
            return 0
        if args.cmd == "check":
            status, message = evaluate(load(directory, check_name(args.name)), max_age)
            if args.json:
                print(json.dumps({"name": args.name, "status": status, "message": message}, indent=2))
            else:
                print("%-8s %s: %s" % (status.upper(), args.name, message))
            return 0 if status in ("ok", "running") else 1
        rows, bad = [], False
        for name, state in all_jobs(directory):
            status, message = evaluate(state, max_age)
            rows.append({"name": name, "status": status, "message": message,
                         "runs": (state or {}).get("runs", 0), "last_exit": (state or {}).get("last_exit")})
            bad = bad or status not in ("ok", "running")
        if args.json:
            print(json.dumps(rows, indent=2))
        elif not rows:
            print("no jobs recorded yet (wrap one with: jobwatch run NAME -- command)")
        else:
            width = max(len(r["name"]) for r in rows)
            for r in rows:
                print("%-8s %-*s  %s" % (r["status"].upper(), width, r["name"], r["message"]))
        return 1 if bad else 0
    except ValueError as exc:
        print("jobwatch: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("jobwatch: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
