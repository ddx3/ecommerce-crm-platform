"""The Analyst: the shop's daily analyst.

  python manage.py run_analyst --once --dry-run   # show findings, write nothing
  python manage.py run_analyst --once             # one run (writing the findings)
  python manage.py run_analyst --guard            # one watchdog cycle (system state + auto-maintenance)
  python manage.py run_analyst --codefix          # one step of the code-error auto-fix pipeline
  python manage.py run_analyst --codefix-drill    # drill: the real pipeline on a training “canary” (+ --simulate-failure)
  python manage.py run_analyst                    # service: watchdog every 5 min, full run daily at ANALYST_RUN_HOUR

The analyst does not change shop data (orders, stock, prices); it writes to its own analyst_* tables
and, per
ANALYST_AUTOFIX, maintains the server through the “helper” on the VPS.
"""
from __future__ import annotations

import faulthandler
import logging
import os
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from analyst.guard import autofix_mode, run_guard
from analyst.models import AnalystRun, Finding
from analyst.runner import apply, collect, run_daily

log = logging.getLogger('analyst')

POLL_SECONDS = 300          # watchdog and daily run
PIPELINE_SECONDS = 30       # auto-fix pipeline: during rollout the result must be picked up quickly
RETRY_AFTER_FAIL = timedelta(hours=1)
WATCHDOG_SECONDS = 2400     # one turn of the main loop longer than this = hang (a regex holding the GIL, network…): the process exits
PIPELINE_STALL = 2700       # one pipeline step longer than this = hang


class Command(BaseCommand):
    help = 'The Analyst: daily purchasing advice and constant monitoring of system health.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='A single run, then exit')
        parser.add_argument('--dry-run', action='store_true',
                            help='Only show findings (together with --once); nothing is written to the DB')
        parser.add_argument('--force', action='store_true',
                            help='Run even if there has already been a successful run today')
        parser.add_argument('--only', help='Comma-separated: run only these checks (with --dry-run)')
        parser.add_argument('--agent', action='store_true',
                            help='Enable the LLM purchasing agent (otherwise per ANALYST_AGENT_ENABLED). '
                                 'Spends the Claude subscription.')
        parser.add_argument('--no-agent', action='store_true', help='Do not call the LLM, only the rule-based calculation')
        parser.add_argument('--guard', action='store_true',
                            help='One watchdog cycle: system health checks and, per ANALYST_AUTOFIX, maintenance')
        parser.add_argument('--codefix', action='store_true', help='One step of the auto-fix pipeline (ANALYST_CODEFIX)')
        parser.add_argument('--codefix-drill', action='store_true',
                            help='Auto-fix drill on the training canary core/canary.py (runs it through to the end)')
        parser.add_argument('--simulate-failure', action='store_true',
                            help="With the drill: deliberately fail the health check after rollout to prove the auto-rollback")

    def handle(self, *args, once, dry_run, force, only, agent, no_agent, guard, codefix=False, codefix_drill=False,
               simulate_failure=False, **opts):
        use_agent = True if agent else (False if no_agent else None)
        if codefix_drill:
            from analyst.codefix import drill, pipeline
            if pipeline.mode() == 'off':
                self.stdout.write('ANALYST_CODEFIX=off: auto-fixes are disabled.')
                return
            ready, why = pipeline.ops_ready()
            if not ready:
                self.stdout.write(f'The drill is not possible: {why}')
                return
            fix = drill.run(self.stdout.write, simulate_failure=simulate_failure)
            self.stdout.write(f'Result: {fix.get_status_display()}')
            return
        if codefix:
            from analyst.codefix import pipeline
            lines = pipeline.tick()
            self.stdout.write(f'Mode: {pipeline.mode()}.' + ('' if lines else ' Nothing to do.'))
            for line in lines:
                self.stdout.write(line)
            return
        if guard:
            res = run_guard()
            self.stdout.write(f'Mode: {autofix_mode()}. New findings {res.fresh}, executed {len(res.applied)}, '
                              f'failed to cope {res.stuck}.')
            return
        if dry_run:
            return self._dry_run(only.split(',') if only else None, use_agent)
        if once:
            run = run_daily(force=force, use_agent=use_agent)
            if run is None:
                self.stdout.write('Already done today (--force to repeat).')
            else:
                self.stdout.write(f'{run}: new {run.new_count}, active {run.total_count}'
                                  + (f'\n{run.error}' if run.error else ''))
            return
        self._serve()

    def _dry_run(self, only, use_agent=None):
        now = timezone.now()
        out = collect(now, only=only)
        if not only:
            from analyst import advice
            result = advice.generate(now, use_agent=bool(use_agent), settle=False)   # in dry-run the LLM runs only with an explicit --agent
            out.items.extend(result.items)
            if result.error:
                self.stdout.write(f'The purchasing agent failed: {result.error}')
            elif result.source == 'agent':
                st = result.stats
                self.stdout.write(f'Purchasing agent: {st.get("tokens", 0):,} tokens, {st.get("turns", 0)} steps, '
                                  f'{st.get("seconds", 0)} s')
        apply(out, now, only=only, dry_run=True)
        sev = dict(Finding.SEVERITY_CHOICES)
        if not out.items:
            self.stdout.write('No findings.')
        for item in sorted(out.items, key=lambda i: -i.severity):
            mark = 'NEW' if item.key in out.new_keys else 'known'
            self.stdout.write(f'[{sev[item.severity]:<8}] [{mark}] {item.title}')
            if item.body:
                self.stdout.write(f'           {item.body}')
        if out.crashed:
            self.stdout.write(f'Checks that crashed: {", ".join(out.crashed)}')
        self.stdout.write(f'Total: {len(out.items)}')

    def _serve(self):
        log.info('analyst: service started, watchdog every %s s (auto-maintenance: %s), auto-fix pipeline every %s s, '
                 'full run daily at %s:00 (%s)',
                 POLL_SECONDS, autofix_mode(), PIPELINE_SECONDS, settings.ANALYST_RUN_HOUR, settings.TIME_ZONE)
        beat = [time.monotonic()]
        threading.Thread(target=self._pipeline_loop, args=(beat,), name='analyst-codefix', daemon=True).start()
        next_guard = 0.0
        while True:
            # Watchdog timer: if the loop hangs (even in C code holding the GIL — faulthandler works
            # without it),
            # the process prints the stacks of all threads and exits, and docker (restart:
            # unless-stopped) brings it back up.
            faulthandler.dump_traceback_later(WATCHDOG_SECONDS, exit=True)
            if time.monotonic() - beat[0] > PIPELINE_STALL:
                log.critical('analyst: the auto-fix pipeline has hung for more than %s min — restarting', PIPELINE_STALL // 60)
                faulthandler.dump_traceback()
                os._exit(1)
            try:
                close_old_connections()
                if time.monotonic() >= next_guard:
                    next_guard = time.monotonic() + POLL_SECONDS
                    try:
                        run_guard()
                    except Exception:
                        log.exception('analyst: watchdog failure')
                    local = timezone.localtime()
                    if local.hour >= settings.ANALYST_RUN_HOUR and self._due(local.date()):
                        run = run_daily()
                        if run is not None:
                            log.info('analyst: %s, new %s, active %s', run.get_status_display(),
                                     run.new_count, run.total_count)
            except Exception:
                log.exception('analyst: service loop failure')
            time.sleep(PIPELINE_SECONDS)

    @staticmethod
    def _pipeline_loop(beat):
        """The auto-fix pipeline in its own thread: a long LLM session or waiting on the helper does not hold
up the watchdog."""
        from django.db import connections
        from analyst.codefix import pipeline
        while True:
            beat[0] = time.monotonic()
            try:
                close_old_connections()
                pipeline.tick()
            except Exception:
                log.exception('analyst: auto-fix pipeline failure')
            finally:
                connections.close_all()
                beat[0] = time.monotonic()
            time.sleep(PIPELINE_SECONDS)

    @staticmethod
    def _due(today) -> bool:
        run = AnalystRun.objects.filter(date=today).first()
        if run is None:
            return True
        if run.status == AnalystRun.OK:
            return False
        if run.status == AnalystRun.FAILED and run.finished_at:
            return timezone.now() - run.finished_at >= RETRY_AFTER_FAIL
        # RUNNING: we wait for it to finish, but a run killed by a restart does not hang forever.
        return timezone.now() - run.started_at >= RETRY_AFTER_FAIL
