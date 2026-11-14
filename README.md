# jobwatch

**Find out when a cron job silently stops running.** Cron tells you nothing when a job never starts, hangs, or fails
every night for a month. jobwatch wraps the command, remembers how it ended, and lets you ask later whether it is healthy.

```
$ jobwatch run nightly-backup -- /usr/local/bin/backup.sh
...the job's own output, and its own exit code...

$ jobwatch status --max-age 26h
OK       nightly-backup  last success 7.2h ago, took 4m
FAILED   cleanup-tmp     last run exited with code 1 2.0h ago (3 failure(s) in a row)
STALE    weekly-report   last success was 9d ago (limit 8d)
```

No server, no account, nothing sent anywhere: a small JSON file per job. One file, standard library only.

## Install and use

```bash
pip install git+https://github.com/TPOALOS/jobwatch          # or copy jobwatch.py
```

In your crontab, put `jobwatch run NAME --` in front of the command:

```
0 2 * * *   jobwatch run nightly-backup --timeout 2h --no-overlap -- /usr/local/bin/backup.sh
```

Then check from anywhere (another cron entry, a monitoring script, your shell prompt):

```bash
jobwatch check nightly-backup --max-age 26h          # exit 1 if it never ran, failed, hung or is stale
jobwatch status --max-age 26h                        # every job; exit 1 if any is unhealthy
jobwatch status --json
jobwatch forget nightly-backup                       # remove a job you retired
```

| Command | What it does |
|---|---|
| `run NAME [--timeout 30m] [--no-overlap] -- CMD...` | runs the command **without a shell**, passes its output and exit code through, records start, end, duration and result |
| `check NAME [--max-age 26h]` | `OK`, `RUNNING`, `NEVER-RAN`, `FAILED`, `STALE` or `HUNG`; exit 0 only for OK/RUNNING |
| `status [--max-age 26h]` | all jobs at a glance |
| `forget NAME` | delete a job's record |

Details worth knowing:

- `--timeout` kills a hung command and exits `124`; `--no-overlap` refuses to start while the previous run is alive (exit `75`).
- A command that is missing exits `127`, one that cannot be executed `126`, one killed by a signal `128+signal`, like a shell.
- The wrapper dying mid-run (reboot, `kill -9`) is detected: the job shows `FAILED`, not `RUNNING` forever.
- State lives in `$JOBWATCH_DIR`, or `~/.local/state/jobwatch` (`--state DIR` overrides), written atomically, folder mode `700`.
- Job names are restricted to letters, digits, `.`, `_`, `-`, so a name can never escape the state folder.

Exit codes: `run` returns the command's own; `check` / `status` return `0` healthy, `1` problem; `2` usage error.

## Limits

jobwatch sees only jobs you wrap. It cannot tell that the machine itself is down: run `check` from a different machine or
service if you need that. Times use the local clock.

## Tests

```bash
python3 -m unittest -v
```

## License

MIT (c) TPOALOS

---

**More tools by [TPOALOS](https://github.com/TPOALOS):** [diskwatch](https://github.com/TPOALOS/diskwatch) · [expirywatch](https://github.com/TPOALOS/expirywatch) · [certwatch](https://github.com/TPOALOS/certwatch) · [all 51 tools](https://tpoalos.github.io)
