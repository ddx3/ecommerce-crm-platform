"""Long-running scheduler of background payment tasks (separate container `scheduler`).

Replaces host cron: the Docker stack has no system cron, so periodic tasks
are run by our own loop. Every --interval seconds it runs in turn:

  - reconcile_pending_payments — reconciles unpaid monobank invoices with the status
    in Mono (success → paid → stock is written off by a signal; expired/failure → cancelled).
    Critical: monobank stock is held only after paid, so without reconcile an abandoned
    invoice would never release the stock level if the webhook did not arrive.
  - match_bank_transactions — matching of the bank statement with orders (bank transfer).
  - process_payment_screenshots — backstop OCR of receipts (the main path is in the background at
    upload).
  - poll_monobank / poll_privat — pull bank statements (only if tokens are set).
  - send_stock_notifications — 'back in stock' emails, a safety net on top of signals
    (bundle availability and stock returns change bypassing post_save).

On a separate (rarer) cadence — once a day and at startup — it runs:
  - recompute_khit — auto-label "Hit" by sales (products + bundles). The remaining
    auto-labels (New / Sale / Made to order) are set by signals on
    save,
    they need no separate recalculation.
  - warmup_np_cache — warms up the Nova Poshta cache of branches/parcel lockers for large cities, so
    that the
    first buyer does not wait for the full pagination (~13s for Kyiv). The run at startup
    refreshes the cache right after deploy.
  - heal_stock_drift — reconciles the invariant 'bag ↔ variant.stock' for own packagings and
    recalculates discrepancies. Guards against an interrupted/forgotten recalculation in any
    bag write path: without it a product with a full bag could hang for weeks as
    "out of stock" until someone noticed by hand. Every finding is an ERROR in
    the log.
  - resolve_pending_instructions — the queue of bundle instructions that deterministic
    substitution could not close on its own (ambiguous replacement / unconfirmed dose):
    LLM resolve without a human. Rarer cadence on purpose (not the main loop): without it
    the endless retry of Rejected rows would eat the shared daily token budget of
    other LLM features within an hour.

Once a day counted from the LAST SUCCESSFUL sync, not from process start (so that a redeploy does
not run
bradas an extra time, but does not push it back either: several redeploys a day would otherwise
never let it run):
  - sync_dropshipping — synchronization of dropshipping catalog prices/stock levels from bradas.ua.
    Updates existing variants, deactivates vanished ones, cleans up long-dead ones. It does not
    create new arrivals itself (that is import_dropshipping's job). If the last sync is older than a
    day —
    run immediately at startup; if fresh — after the rest of the day; if the age is unknown (new
    database) — after a day.

Run: python manage.py run_scheduler [--interval 300] [--khit-interval 86400]
The command is idempotent: all subcommands are safe on repeated runs.
"""
from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import close_old_connections

log = logging.getLogger('orders')

# How many full cycles in a row (no task ran) we tolerate before killing the process. Docker
# `restart: unless-stopped` will bring the container back up with a clean state. This is a backstop
# in case some state got corrupted in a way that close_old_connections() does not rescue: better a
# restart than silently spinning for 4 days.
MAX_DEAD_CYCLES = 3


class Command(BaseCommand):
    help = 'Long-running background scheduler for payment reconciliation, matching and screenshot OCR.'

    def add_arguments(self, parser):
        parser.add_argument('--interval', type=int, default=300,
                            help='Pause between cycles, sec (default: 300)')
        parser.add_argument('--khit-interval', type=int, default=86400,
                            help='How often to recompute "Hit", sec (default: 86400 = a day)')
        parser.add_argument('--np-interval', type=int, default=86400,
                            help='How often to warm the NP branch cache, sec (default: 86400 = a day)')
        parser.add_argument('--dropsync-interval', type=int, default=86400,
                            help='How often to sync dropshipping with bradas, sec (default: 86400 = a day)')
        parser.add_argument('--drift-interval', type=int, default=86400,
                            help='How often to check/heal bag↔stock drift, sec (default: 86400 = a day)')
        parser.add_argument('--seo-interval', type=int, default=86400,
                            help='How often to generate SEO meta for new products/bundles/brands/blog, sec (default: 86400 = a day)')
        parser.add_argument('--instructions-interval', type=int, default=600,
                            help='How often to resolve the bundle instruction queue via LLM, sec (default: 600)')
        parser.add_argument('--once', action='store_true',
                            help='Run one cycle and exit (for manual checking)')

    def _jobs(self):
        jobs = []
        if settings.MONOBANK_PERSONAL_TOKEN:
            jobs.append('poll_monobank')
        if settings.PRIVAT_BUSINESS_TOKEN and settings.PRIVAT_BUSINESS_CLIENT_ID:
            jobs.append('poll_privat')
        jobs += ['reconcile_pending_payments', 'match_bank_transactions', 'process_payment_screenshots',
                 'send_stock_notifications']
        return jobs

    def _run_job(self, cmd: str) -> bool:
        """Run a sub-command once. Returns True if it finished without an exception.

        close_old_connections() is mandatory, not cosmetic. Django applies
        CONN_MAX_AGE and CONN_HEALTH_CHECKS exactly here, and calls it itself only on the
        request_started/request_finished signals. A long-running management command has
        no HTTP requests, so without an explicit call the connection is opened once for
        the whole lifetime of the process: as soon as Postgres drops it (DB restart, idle
        timeout, network) it stays dead forever, and EVERY task fails with
        InterfaceError('connection already closed'). This is exactly how the scheduler silently
        stood idle for 4 days (29.07–02.08.2026), until the manager noticed that bank transfers
        stopped being auto-confirmed.
        """
        close_old_connections()
        try:
            call_command(cmd)
            self._beat(cmd, ok=True)
            return True
        except Exception as exc:
            log.exception('scheduler: job %s failed', cmd)
            self._beat(cmd, ok=False, error=exc)
            return False

    @staticmethod
    def _beat(name: str, *, ok: bool, error: Exception | None = None) -> None:
        """Task heartbeat for the analyst (analyst.JobHeartbeat): the scheduler will not report its own
        death, so the "Analyst" section reads when it last ran successfully.
        A failure to write the heartbeat must never affect the scheduler itself."""
        try:
            from django.utils import timezone
            from analyst.models import JobHeartbeat
            if ok:
                defaults = {'last_ok_at': timezone.now()}
            else:
                defaults = {'last_fail_at': timezone.now(),
                            'last_error': f'{type(error).__name__}: {error}'[:300]}
            JobHeartbeat.objects.update_or_create(name=name, defaults=defaults)
        except Exception:
            log.warning('scheduler: failed to write the heartbeat %s', name, exc_info=True)

    @staticmethod
    def _dropsync_age() -> float | None:
        """How many seconds have passed since the last successful sync with bradas: by the task
        heartbeat and by the freshest
        last_synced_at of dropshipping variants (whichever is later). None — unknown or a read
        failure."""
        try:
            from django.utils import timezone
            from analyst.models import JobHeartbeat
            from catalog.models import ProductVariant
            beat = (JobHeartbeat.objects.filter(name='sync_dropshipping', last_ok_at__isnull=False)
                    .values_list('last_ok_at', flat=True).first())
            synced = (ProductVariant.objects.filter(is_dropshipping=True, last_synced_at__isnull=False)
                      .order_by('-last_synced_at').values_list('last_synced_at', flat=True).first())
            last = max((x for x in (beat, synced) if x), default=None)
            return None if last is None else max(0.0, (timezone.now() - last).total_seconds())
        except Exception:
            log.warning('scheduler: failed to determine the age of the last sync with bradas', exc_info=True)
            return None

    def _run_cycle(self) -> bool:
        """Main loop. True if at least one task ran."""
        results = [self._run_job(cmd) for cmd in self._jobs()]
        return any(results)

    def _run_khit(self):
        self._run_job('recompute_khit')

    def _run_np_warmup(self):
        self._run_job('warmup_np_cache')

    def _run_dropsync(self):
        self._run_job('sync_dropshipping')

    def _run_drift(self):
        # A run at startup is also needed: if drift was left over from the previous release, a
        # redeploy should clear it right away, not a day later.
        self._run_job('heal_stock_drift')

    def _run_seo(self):
        # Fills meta_title/description for new products/bundles/brands/blog
        # from their descriptions (idempotent — empty fields only). SEO 'under the hood':
        # the manager adds a product, and the next day it automatically has meta.
        self._run_job('seo_apply_content_meta')

    def _run_instructions(self):
        self._run_job('resolve_pending_instructions')

    def handle(self, *args, interval, khit_interval, np_interval, dropsync_interval,
               drift_interval, seo_interval, instructions_interval,
               once, **opts):
        if once:
            self._run_cycle()
            self._run_khit()
            self._run_np_warmup()
            self._run_dropsync()
            self._run_drift()
            self._run_seo()
            self._run_instructions()
            return
        log.info('scheduler: started, interval=%ss, khit_interval=%ss, np_interval=%ss, '
                 'dropsync_interval=%ss, jobs=%s',
                 interval, khit_interval, np_interval, dropsync_interval, self._jobs())
        last_khit = None
        last_np = None
        last_seo = None
        last_drift = None
        last_instructions = None
        # Dropsync is not run on every start (otherwise every redeploy = an extra pass over bradas),
        # but we also do not postpone it by a day
        # FROM PROCESS START: there are several redeploys a day, and then the sync would never
        # run (that is how it was on 18–21.10.2026: dropshipping prices and stock levels were stale
        # for 77 h).
        # The countdown is from the last successful sync: not yet overdue — wait the rest of the
        # day, overdue — run immediately.
        # Age unknown (new database, read failure) — as before: the first run after a day.
        age = self._dropsync_age()
        last_dropsync = time.monotonic() - (0 if age is None else min(age, dropsync_interval))
        dead_cycles = 0
        while True:
            if self._run_cycle():
                dead_cycles = 0
            else:
                dead_cycles += 1
                log.error('scheduler: cycle #%d in a row without a single successful task '
                          '(of %d). %d more and the process restarts.',
                          dead_cycles, MAX_DEAD_CYCLES, MAX_DEAD_CYCLES - dead_cycles)
                if dead_cycles >= MAX_DEAD_CYCLES:
                    log.error('scheduler: %d dead cycles in a row — exiting, '
                              'let Docker bring the container back up.', dead_cycles)
                    raise SystemExit(1)
            # Less frequent cadences: at startup (last_* is None) and then once a day.
            now = time.monotonic()
            if last_khit is None or (now - last_khit) >= khit_interval:
                self._run_khit()
                last_khit = time.monotonic()
            if last_np is None or (now - last_np) >= np_interval:
                self._run_np_warmup()
                last_np = time.monotonic()
            if (now - last_dropsync) >= dropsync_interval:
                self._run_dropsync()
                last_dropsync = time.monotonic()
            if last_drift is None or (now - last_drift) >= drift_interval:
                self._run_drift()
                last_drift = time.monotonic()
            if last_seo is None or (now - last_seo) >= seo_interval:
                self._run_seo()
                last_seo = time.monotonic()
            if last_instructions is None or (now - last_instructions) >= instructions_interval:
                self._run_instructions()
                last_instructions = time.monotonic()
            time.sleep(interval)
