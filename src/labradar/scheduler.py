"""Weekly job runner.

This calls exactly the same entrypoints as the CLI, so a scheduled run and a
hand-run are the same code path. Weekly rather than daily because the directory
changes a few times a semester and OpenAlex reindexes slowly -- polling harder
would spend budget to observe noise.
"""

from __future__ import annotations

import logging
import os

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from labradar.cli import main as cli_main

log = logging.getLogger("labradar.scheduler")


def run_chain() -> None:
    """Run the full pipeline, logging rather than raising on failure.

    A failed week must not kill the scheduler: the next run should still fire,
    and the failure is recorded in ``fetch_run`` either way.
    """
    log.info("starting scheduled run")
    try:
        cli_main(["run-all"])
        log.info("scheduled run finished")
    except Exception:
        log.exception("scheduled run failed; will retry on the next trigger")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    scheduler = BlockingScheduler(timezone=os.getenv("TZ", "UTC"))
    trigger = CronTrigger(
        day_of_week=os.getenv("LABRADAR_CRON_DAY_OF_WEEK", "sun"),
        hour=int(os.getenv("LABRADAR_CRON_HOUR", "3")),
        minute=int(os.getenv("LABRADAR_CRON_MINUTE", "0")),
    )
    scheduler.add_job(run_chain, trigger, id="labradar-weekly", replace_existing=True)

    if os.getenv("LABRADAR_RUN_ON_START", "").lower() in ("1", "true", "yes"):
        run_chain()

    log.info("scheduler started with trigger %s", trigger)
    scheduler.start()


if __name__ == "__main__":
    main()
