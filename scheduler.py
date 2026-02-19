"""
APScheduler wrapper — runs the full pipeline daily at the configured hour.
"""

import logging
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


def start_scheduler(run_pipeline_fn, schedule_hour: int = 8) -> None:
    """
    Start a blocking scheduler that calls `run_pipeline_fn` every day
    at `schedule_hour` (UTC).
    """
    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(
        run_pipeline_fn,
        trigger=CronTrigger(hour=schedule_hour, minute=0),
        id="daily_pipeline",
        name="Daily competitor monitor pipeline",
        misfire_grace_time=3600,   # allow up to 1h late start
        replace_existing=True,
    )

    logger.info(
        "Scheduler started — pipeline will run daily at %02d:00 UTC",
        schedule_hour,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")
        scheduler.shutdown(wait=False)
        sys.exit(0)
