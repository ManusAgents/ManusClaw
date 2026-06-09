from __future__ import annotations
"""CronScheduler — persists cron jobs as YAML, delivers output to messaging platforms.

Enhanced with:
  - --output and --output-channel/--output-target flags for any messaging channel
  - --trigger-webhook for webhook-triggered cron events
  - Multi-platform output routing
"""
import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable

from app.logger import logger

try:
    from croniter import croniter
    _HAS_CRONITER = True
except ImportError:
    _HAS_CRONITER = False

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

_DEFAULT_CRON_FILE = str(Path.home() / ".manusclaw" / "cron_jobs.yaml")
_JOBS_FILE = Path(os.getenv("MANUSCLAW_CRON_FILE", _DEFAULT_CRON_FILE))


@dataclass
class CronJob:
    """A scheduled job that runs an agent prompt on a cron schedule.

    Attributes:
        job_id: Unique job identifier.
        name: Human-readable job name.
        cron_expr: Cron expression (e.g. ``0 9 * * *`` for daily at 9am).
        prompt: The prompt to send to the agent.
        platform: Messaging platform for output delivery (e.g. ``telegram``, ``whatsapp``).
        channel_id: Channel/chat ID to send output to.
        output_targets: List of additional output targets (platform:channel_id pairs).
        webhook_url: Optional webhook URL to POST results to.
        webhook_secret: Optional secret for webhook HMAC signing.
        enabled: Whether the job is active.
        last_run: Monotonic timestamp of last execution.
        next_run: Monotonic timestamp of next scheduled execution.
        run_count: Total number of times the job has been executed.
    """
    job_id: str
    name: str
    cron_expr: str
    prompt: str
    platform: Optional[str] = None
    channel_id: Optional[str] = None
    output_targets: list[str] = field(default_factory=list)
    webhook_url: Optional[str] = None
    webhook_secret: Optional[str] = None
    enabled: bool = True
    last_run: float = 0.0
    next_run: float = 0.0
    run_count: int = 0

    def update_next_run(self) -> None:
        if not _HAS_CRONITER:
            self.next_run = time.time() + 3600
            return
        try:
            self.next_run = croniter(self.cron_expr, time.time()).get_next(float)
        except Exception:
            self.next_run = time.time() + 3600

    @property
    def is_due(self) -> bool:
        return self.enabled and time.time() >= self.next_run

    @property
    def all_output_targets(self) -> list[tuple[str, str]]:
        """Return all output targets as (platform, channel_id) tuples.

        Includes the primary platform/channel_id and any additional output_targets.
        """
        targets = []
        if self.platform and self.channel_id:
            targets.append((self.platform, self.channel_id))
        for target_str in self.output_targets:
            parts = target_str.split(":", 1)
            if len(parts) == 2:
                targets.append((parts[0], parts[1]))
        return targets


class CronScheduler:
    """
    Simple cron scheduler that runs agent prompts on a schedule.
    Jobs are persisted to YAML and survive restarts.
    Requires: pip install croniter pyyaml

    Enhanced features:
    - Multi-platform output routing via --output-channel/--output-target
    - Webhook trigger support via --trigger-webhook
    - Backward compatible with existing job format
    """

    def __init__(self) -> None:
        self._jobs: dict[str, CronJob] = {}
        self._running = False
        self._tasks: set[asyncio.Task] = set()
        self._webhook_handlers: dict[str, Callable] = {}
        self._load_jobs()

    def _load_jobs(self) -> None:
        if not _JOBS_FILE.exists():
            return
        if not _HAS_YAML:
            logger.warning("[Cron] PyYAML not installed — cannot load persisted jobs. pip install pyyaml")
            return
        try:
            data = _yaml.safe_load(_JOBS_FILE.read_text()) or {}
            for job_id, d in data.items():
                kwargs = {k: v for k, v in d.items() if k != "job_id"}
                job = CronJob(job_id=job_id, **kwargs)
                self._jobs[job_id] = job
            logger.info(f"[Cron] Loaded {len(self._jobs)} jobs")
        except Exception as e:
            logger.warning(f"[Cron] Could not load jobs: {e}")

    def _save_jobs(self) -> None:
        if not _HAS_YAML:
            logger.warning("[Cron] PyYAML not installed — job persistence disabled.")
            return
        try:
            _JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                jid: {k: v for k, v in vars(j).items() if k != "job_id"}
                for jid, j in self._jobs.items()
            }
            _JOBS_FILE.write_text(_yaml.dump(data, default_flow_style=False))
        except Exception as e:
            logger.warning(f"[Cron] Could not save jobs: {e}")

    def add_job(self, job_id: str, name: str, cron_expr: str, prompt: str,
                platform: str = "", channel_id: str = "",
                output_targets: Optional[list[str]] = None,
                webhook_url: Optional[str] = None,
                webhook_secret: Optional[str] = None) -> CronJob:
        """Add a new cron job with optional output routing.

        Args:
            job_id: Unique job identifier.
            name: Human-readable name.
            cron_expr: Cron expression.
            prompt: Agent prompt.
            platform: Primary output platform.
            channel_id: Primary output channel ID.
            output_targets: Additional output targets as ``platform:channel_id`` strings.
            webhook_url: Optional webhook URL for result delivery.
            webhook_secret: Optional HMAC secret for webhook signing.

        Returns:
            The created CronJob instance.
        """
        job = CronJob(
            job_id=job_id, name=name, cron_expr=cron_expr, prompt=prompt,
            platform=platform or None, channel_id=channel_id or None,
            output_targets=output_targets or [],
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
        )
        job.update_next_run()
        self._jobs[job_id] = job
        self._save_jobs()

        output_info = ""
        if platform and channel_id:
            output_info += f" → {platform}:{channel_id}"
        if output_targets:
            for t in output_targets:
                output_info += f" → {t}"
        if webhook_url:
            output_info += f" → webhook:{webhook_url[:50]}"

        logger.info(f"[Cron] Added job: {name!r} ({cron_expr}){output_info}")
        return job

    def remove_job(self, job_id: str) -> bool:
        if job_id in self._jobs:
            del self._jobs[job_id]
            self._save_jobs()
            return True
        return False

    def list_jobs(self) -> list[CronJob]:
        return list(self._jobs.values())

    def trigger_job(self, job_id: str) -> Optional[CronJob]:
        """Force a job to run on the next scheduler tick."""
        job = self._jobs.get(job_id)
        if job:
            job.next_run = time.time()
        return job

    def register_webhook_handler(self, job_id: str, handler: Callable) -> None:
        """Register a custom webhook handler for a cron job.

        The handler is called with the job and result as arguments:
            ``handler(job: CronJob, result: str) -> None``
        """
        self._webhook_handlers[job_id] = handler
        logger.debug(f"[Cron] Registered webhook handler for job {job_id}")

    async def run_forever(self, output_callback: Optional[Callable] = None) -> None:
        self._running = True
        logger.info("[Cron] Scheduler started")
        while self._running:
            due = [j for j in self._jobs.values() if j.is_due]
            for job in due:
                # FIX: store task reference to prevent GC mid-execution
                task = asyncio.create_task(self._run_job(job, output_callback))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            await asyncio.sleep(30)

    async def _run_job(self, job: CronJob, callback: Optional[Callable]) -> None:
        logger.info(f"[Cron] Running job: {job.name!r}")
        job.last_run = time.time()
        job.run_count += 1
        job.update_next_run()
        self._save_jobs()
        try:
            from app.agent.manus import Manus
            agent = Manus()
            result = await agent.run(job.prompt)

            # 1. Custom callback
            if callback:
                await callback(job, result)

            # 2. Primary output target
            if job.platform and job.channel_id:
                from app.messaging.gateway import MessagingGateway
                gw = MessagingGateway(use_router=True)
                await gw.send(job.platform, job.channel_id,
                              f"[Cron: {job.name}]\n{result[:3000]}")

            # 3. Additional output targets
            for platform, channel_id in job.all_output_targets:
                if platform == job.platform and channel_id == job.channel_id:
                    continue  # Already sent to primary
                try:
                    from app.messaging.gateway import MessagingGateway
                    gw = MessagingGateway(use_router=True)
                    await gw.send(platform, channel_id,
                                  f"[Cron: {job.name}]\n{result[:3000]}")
                except Exception as e:
                    logger.warning(
                        f"[Cron] Failed to send to {platform}:{channel_id}: {e}"
                    )

            # 4. Webhook delivery
            if job.webhook_url:
                await self._deliver_webhook(job, result)

            # 5. Custom webhook handler
            if job.job_id in self._webhook_handlers:
                handler = self._webhook_handlers[job.job_id]
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(job, result)
                    else:
                        handler(job, result)
                except Exception as e:
                    logger.error(
                        f"[Cron] Webhook handler error for {job.job_id}: {e}"
                    )

        except Exception as e:
            logger.error(f"[Cron] Job {job.name!r} failed: {e}")

    async def _deliver_webhook(self, job: CronJob, result: str) -> None:
        """Deliver cron job results to a webhook URL.

        Sends a JSON POST with the job result. If webhook_secret is set,
        includes an HMAC-SHA256 signature in the ``X-ManusClaw-Signature`` header.
        """
        try:
            import aiohttp
            import json
            import hashlib
            import hmac as hmac_mod

            payload = {
                "job_id": job.job_id,
                "name": job.name,
                "cron_expr": job.cron_expr,
                "result": result[:3000],
                "run_count": job.run_count,
                "timestamp": time.time(),
            }

            headers = {"Content-Type": "application/json"}

            # HMAC signature if secret is configured
            if job.webhook_secret:
                body = json.dumps(payload)
                sig = hmac_mod.new(
                    job.webhook_secret.encode(),
                    body.encode(),
                    hashlib.sha256,
                ).hexdigest()
                headers["X-ManusClaw-Signature"] = f"sha256={sig}"
            else:
                body = json.dumps(payload)

            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    job.webhook_url, data=body, headers=headers
                ) as resp:
                    if resp.status >= 400:
                        error_text = await resp.text()
                        logger.warning(
                            f"[Cron] Webhook delivery failed: HTTP {resp.status} "
                            f"{error_text[:200]}"
                        )
                    else:
                        logger.info(
                            f"[Cron] Webhook delivered to {job.webhook_url[:50]} "
                            f"(HTTP {resp.status})"
                        )
        except ImportError:
            logger.warning("[Cron] aiohttp not installed — webhook delivery skipped")
        except Exception as e:
            logger.warning(f"[Cron] Webhook delivery error: {e}")

    def stop(self) -> None:
        self._running = False
        logger.info("[Cron] Scheduler stopped")


def _parse_output_target(output_str: str) -> tuple[str, str]:
    """Parse an output target string like ``telegram:channel_id`` or ``whatsapp:12345``.

    Returns:
        Tuple of (platform, channel_id).
    """
    parts = output_str.split(":", 1)
    if len(parts) != 2:
        raise ValueError(
            f"Invalid output target format: '{output_str}'. "
            f"Expected 'platform:channel_id' (e.g. telegram:12345)"
        )
    return parts[0].strip(), parts[1].strip()


def main() -> None:
    """
    Entry point for manusclaw-cron command.

    Usage:
      manusclaw-cron --run                          # Start scheduler loop
      manusclaw-cron --list                         # List all jobs
      manusclaw-cron --add ID NAME EXPR PROMPT      # Add a new job
      manusclaw-cron --add ID NAME EXPR PROMPT --output telegram:12345
      manusclaw-cron --add ID NAME EXPR PROMPT --output-channel telegram --output-target 12345
      manusclaw-cron --add ID NAME EXPR PROMPT --trigger-webhook https://example.com/hook
      manusclaw-cron --remove JOB_ID                # Remove a job
      manusclaw-cron --trigger JOB_ID               # Force-trigger a job now
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="ManusClaw Cron Scheduler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  manusclaw-cron --run
  manusclaw-cron --list
  manusclaw-cron --add daily-report "Daily Report" "0 9 * * *" "Summarise today's news"
  manusclaw-cron --add daily-report "Daily Report" "0 9 * * *" "Summarise news" --output telegram:12345
  manusclaw-cron --add daily-report "Daily Report" "0 9 * * *" "Summarise news" --output-channel telegram --output-target 12345
  manusclaw-cron --add web-hook "Webhook" "0 */2 * * *" "Check status" --trigger-webhook https://example.com/hook
  manusclaw-cron --remove daily-report
  manusclaw-cron --trigger daily-report
        """,
    )
    parser.add_argument("--run",     action="store_true", help="Start the scheduler loop")
    parser.add_argument("--list",    action="store_true", help="List all scheduled jobs")
    parser.add_argument("--add",     nargs=4, metavar=("ID", "NAME", "EXPR", "PROMPT"),
                        help="Add a new job")
    parser.add_argument("--output", metavar="PLATFORM:CHANNEL",
                        help="Output target for job results (e.g. telegram:12345)")
    parser.add_argument("--output-channel", metavar="PLATFORM",
                        help="Output platform (e.g. telegram, discord, whatsapp)")
    parser.add_argument("--output-target", metavar="CHANNEL_ID",
                        help="Output channel ID for the output platform")
    parser.add_argument("--trigger-webhook", metavar="URL",
                        help="Webhook URL to POST job results to")
    parser.add_argument("--remove",  metavar="JOB_ID", help="Remove a job by ID")
    parser.add_argument("--trigger", metavar="JOB_ID", help="Force-trigger a job immediately")

    args = parser.parse_args()

    scheduler = CronScheduler()

    if args.list:
        jobs = scheduler.list_jobs()
        if not jobs:
            print("No scheduled jobs.")
            return
        print(f"{'ID':<20} {'NAME':<24} {'CRON':<16} {'RUNS':>5}  {'OUTPUT':<30} {'ENABLED'}")
        print("-" * 110)
        for j in jobs:
            status = "yes" if j.enabled else "no"
            output = ""
            if j.platform and j.channel_id:
                output = f"{j.platform}:{j.channel_id}"
            if j.webhook_url:
                output = f"webhook:{j.webhook_url[:25]}..."
            for t in j.output_targets:
                output = f"{t}"
            print(f"{j.job_id:<20} {j.name:<24} {j.cron_expr:<16} {j.run_count:>5}  {output:<30} {status}")
        return

    if args.add:
        job_id, name, expr, prompt = args.add

        # Parse output flags
        platform = ""
        channel_id = ""
        output_targets = []
        webhook_url = None

        if args.output:
            p, c = _parse_output_target(args.output)
            platform, channel_id = p, c

        if args.output_channel:
            platform = args.output_channel
        if args.output_target:
            channel_id = args.output_target

        if args.trigger_webhook:
            webhook_url = args.trigger_webhook

        job = scheduler.add_job(
            job_id, name, expr, prompt,
            platform=platform, channel_id=channel_id,
            output_targets=output_targets,
            webhook_url=webhook_url,
        )
        print(f"Added job '{job_id}'. Next run at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(job.next_run))}")
        if platform and channel_id:
            print(f"  Output: {platform}:{channel_id}")
        if webhook_url:
            print(f"  Webhook: {webhook_url}")
        return

    if args.remove:
        removed = scheduler.remove_job(args.remove)
        print(f"Removed job '{args.remove}'." if removed else f"Job '{args.remove}' not found.")
        return

    if args.trigger:
        job = scheduler.trigger_job(args.trigger)
        print(f"Job '{args.trigger}' scheduled to run on next tick." if job else f"Job '{args.trigger}' not found.")

    # Default: run the scheduler (also triggered by --run)
    try:
        asyncio.run(scheduler.run_forever())
    except KeyboardInterrupt:
        scheduler.stop()
        print("\n[Cron] Stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
