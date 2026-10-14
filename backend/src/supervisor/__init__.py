"""The exit-only supervisor: the PAPER exit job, kept running for a position's whole life.

A BUY in split operation rests on recent evidence that the exit job works
(`src.orchestration.jobruns`). That evidence goes stale after
`PAPER_EXIT_LIVENESS_MAX_AGE_SECONDS`, while an early position may stay open for
72 hours. Something has to start `--exits-once` again and again for as long as
anything is held — and say loudly when it cannot.

This package is that something, and nothing more:

- `tick` — one supervised pass: start `python -m src.runner.main --exits-once`
  as a child, bounded by a hard wall clock, classify what it reported, and
  record the result in a local state file. launchd starts a tick every 60
  seconds (`ops/exit-supervisor`); launchd never starts a second tick while
  one runs, and the exit job's own advisory lock keeps any other exit run out.
- `health` — a read-only verdict: HEALTHY, DEGRADED, CRITICAL or STOPPED, from
  the service status, the state file and the database's own exit evidence.
- `stop-check` / `deploy-check` — read-only guards an operator script must pass
  before a regular stop or a code deploy.

It never starts an entry, a scout or a combined run, never opens a case and
never buys: the only command it can build is `--exits-once`. It adds no
trading logic, no new lock and no new table; every judgement about the exit
job's health is the one `paper_job_runs` and `judge_exit_liveness` already make.
"""
