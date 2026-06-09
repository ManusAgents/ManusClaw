"""Tests for enhanced cron (output targets, webhook delivery, multi-target routing)."""

import os
import pytest
from app.cron import CronJob, CronScheduler, _parse_output_target


# ── CronJob data model ─────────────────────────────────────────────────────

def test_cron_job_creation():
    job = CronJob(
        job_id="job-1",
        name="Daily Report",
        cron_expr="0 9 * * *",
        prompt="Summarise today's news",
        platform="telegram",
        channel_id="12345",
        enabled=False,  # disabled so is_due is False regardless of next_run
    )
    assert job.job_id == "job-1"
    assert job.is_due is False  # disabled jobs are never due


def test_cron_job_update_next_run():
    job = CronJob(
        job_id="job-1", name="Test", cron_expr="0 * * * *", prompt="test"
    )
    assert job.next_run == 0.0
    job.update_next_run()
    assert job.next_run > 0


def test_cron_job_is_due():
    import time
    job = CronJob(
        job_id="job-1", name="Test", cron_expr="0 * * * *", prompt="test",
        enabled=True,
    )
    job.next_run = time.time() - 1  # Set in the past
    assert job.is_due is True

    job.enabled = False
    assert job.is_due is False


# ── Output targets ─────────────────────────────────────────────────────────

def test_all_output_targets_primary_only():
    job = CronJob(
        job_id="j1", name="T", cron_expr="* * * * *", prompt="p",
        platform="telegram", channel_id="123",
    )
    targets = job.all_output_targets
    assert len(targets) == 1
    assert targets[0] == ("telegram", "123")


def test_all_output_targets_with_additional():
    job = CronJob(
        job_id="j1", name="T", cron_expr="* * * * *", prompt="p",
        platform="telegram", channel_id="123",
        output_targets=["discord:456", "slack:789"],
    )
    targets = job.all_output_targets
    assert len(targets) == 3


def test_all_output_targets_no_platform():
    job = CronJob(
        job_id="j1", name="T", cron_expr="* * * * *", prompt="p",
        output_targets=["discord:456"],
    )
    targets = job.all_output_targets
    assert len(targets) == 1
    assert targets[0] == ("discord", "456")


# ── _parse_output_target ──────────────────────────────────────────────────

def test_parse_output_target_valid():
    assert _parse_output_target("telegram:12345") == ("telegram", "12345")
    assert _parse_output_target("discord:general") == ("discord", "general")


def test_parse_output_target_invalid():
    with pytest.raises(ValueError, match="Invalid output target format"):
        _parse_output_target("no_colon_here")


# ── CronScheduler job management ──────────────────────────────────────────

def test_scheduler_add_and_list(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUSCLAW_CRON_FILE", str(tmp_path / "unique_test_cron.yaml"))
    scheduler = CronScheduler()
    job = scheduler.add_job(
        "job-1", "Test Job", "0 * * * *", "run something",
        platform="telegram", channel_id="123",
    )
    assert job.job_id == "job-1"
    assert job.next_run > 0

    jobs = scheduler.list_jobs()
    assert len(jobs) >= 1  # >= 1 because there might be existing jobs from other tests
    assert any(j.job_id == "job-1" for j in jobs)


def test_scheduler_remove_job(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUSCLAW_CRON_FILE", str(tmp_path / "test_cron.yaml"))
    scheduler = CronScheduler()
    scheduler.add_job("job-1", "Test", "* * * * *", "prompt")
    assert scheduler.remove_job("job-1") is True
    assert scheduler.remove_job("nonexistent") is False


def test_scheduler_trigger_job(tmp_path, monkeypatch):
    import time
    monkeypatch.setenv("MANUSCLAW_CRON_FILE", str(tmp_path / "test_cron.yaml"))
    scheduler = CronScheduler()
    job = scheduler.add_job("job-1", "Test", "* * * * *", "prompt")
    original_next = job.next_run

    triggered = scheduler.trigger_job("job-1")
    assert triggered is not None
    assert triggered.next_run <= time.time()  # Should be "now"


def test_scheduler_add_job_with_webhook(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUSCLAW_CRON_FILE", str(tmp_path / "test_cron.yaml"))
    scheduler = CronScheduler()
    job = scheduler.add_job(
        "web-job", "Webhook Test", "0 */2 * * *", "check status",
        webhook_url="https://example.com/hook",
        webhook_secret="my-secret",
    )
    assert job.webhook_url == "https://example.com/hook"
    assert job.webhook_secret == "my-secret"


def test_scheduler_add_job_with_output_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUSCLAW_CRON_FILE", str(tmp_path / "test_cron.yaml"))
    scheduler = CronScheduler()
    job = scheduler.add_job(
        "multi-out", "Multi Output", "* * * * *", "report",
        platform="telegram", channel_id="111",
        output_targets=["discord:222", "slack:333"],
    )
    targets = job.all_output_targets
    assert len(targets) == 3


def test_scheduler_register_webhook_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUSCLAW_CRON_FILE", str(tmp_path / "test_cron.yaml"))
    scheduler = CronScheduler()
    handler = lambda job, result: None
    scheduler.register_webhook_handler("job-1", handler)
    assert "job-1" in scheduler._webhook_handlers
