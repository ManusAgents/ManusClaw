"""Tests for cron scheduler."""
import pytest
import os
os.environ["APP_ENV"] = "test"

from app.config import Config
Config.reset()


def test_cron_add_and_list():
    """Should add a job and return it in list."""
    from app.cron import CronScheduler
    sched = CronScheduler()
    job = sched.add_job("j1", "Test Job", "0 * * * *", "Say hello")
    assert job.job_id == "j1"
    assert any(j.job_id == "j1" for j in sched.list_jobs())


def test_cron_remove_job():
    from app.cron import CronScheduler
    sched = CronScheduler()
    sched.add_job("j2", "Remove Me", "0 0 * * *", "Test")
    removed = sched.remove_job("j2")
    assert removed
    assert not any(j.job_id == "j2" for j in sched.list_jobs())


def test_cron_trigger_sets_due():
    """trigger_job should make a job immediately due."""
    from app.cron import CronScheduler, CronJob
    import time
    sched = CronScheduler()
    sched.add_job("j3", "Trigger Test", "0 0 1 1 *", "Test")  # once a year
    # Trigger it immediately
    sched.trigger_job("j3")
    job = next(j for j in sched.list_jobs() if j.job_id == "j3")
    assert job.is_due


def test_cron_graceful_without_croniter():
    """CronJob.update_next_run should not crash without croniter."""
    from app.cron import CronJob, _HAS_CRONITER
    job = CronJob(job_id="t", name="t", cron_expr="invalid", prompt="test")
    job.update_next_run()  # should not raise
    assert job.next_run > 0
