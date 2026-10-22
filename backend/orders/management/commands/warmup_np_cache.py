"""Warm-up of the Nova Poshta branch/parcel locker cache for big cities.

Without warm-up the first buyer from a big city waits for the full NP pagination (Kyiv ~7600
points = 16 pages with pauses of ~13s). The command fills the cache in advance, so the buyer
hits a warm cache instantly. Run by the scheduler (run_scheduler) at startup
and once a day; the 7-day cache TTL gives slack if a run is missed.

The --flush flag first wipes old np:wh:* keys (needed once after deploying the pagination
fix, to remove cached truncations at 500 points).
"""
from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand

from orders.views import _np_call, _np_get_all_warehouses, NP_WH_CACHE_TTL

log = logging.getLogger('orders')

# Big cities of Ukraine (excluding the occupied territories, where NP does not operate). The
# remaining cities
# are small, pagination takes one or two requests, no noticeable wait.
# Language data: the city names stay in Ukrainian, as the Nova Poshta API knows them.
TOP_CITIES = [
    'Київ', 'Харків', 'Одеса', 'Дніпро', 'Запоріжжя', 'Львів', 'Кривий Ріг',
    'Миколаїв', 'Вінниця', 'Полтава', 'Чернігів', 'Черкаси', 'Хмельницький',
    'Чернівці', 'Житомир', 'Суми', 'Рівне', 'Івано-Франківськ', 'Тернопіль',
    'Кропивницький', 'Луцьк', 'Кременчук', 'Біла Церква', 'Ужгород',
    "Кам'янець-Подільський", 'Бровари', 'Бориспіль', 'Ірпінь',
]


class Command(BaseCommand):
    help = 'Warm the NP branch/parcel locker cache for big cities (removes the first buyer\'s wait).'

    def add_arguments(self, parser):
        parser.add_argument('--flush', action='store_true',
                            help='First delete all np:wh:* keys (after deploying the pagination fix).')

    def _flush_wh_cache(self):
        try:
            client = cache._cache.get_client()  # RedisCache → redis-py client
        except AttributeError:
            cache.clear()  # LocMemCache (dev) — there is no separate scan
            self.stdout.write('  flush: local cache fully cleared (LocMem)')
            return
        n = 0
        for k in client.scan_iter(match='*np:wh:*'):
            client.delete(k)
            n += 1
        self.stdout.write(f'  flush: deleted np:wh:* keys — {n}')

    def handle(self, *args, flush=False, **opts):
        key = settings.NOVA_POSHTA_API_KEY
        if not key:
            self.stderr.write('NOVA_POSHTA_API_KEY is not set — warm-up skipped')
            return

        if flush:
            self._flush_wh_cache()

        ok = 0
        for name in TOP_CITIES:
            res = _np_call('searchSettlements', {'CityName': name, 'Limit': 1, 'Page': 1})
            addrs = ((res.get('data') or [{}])[0]).get('Addresses') or []
            if not addrs:
                self.stderr.write(f'  {name}: locality not found')
                time.sleep(0.6)
                continue
            ref = addrs[0].get('Ref')
            data = _np_get_all_warehouses(key, ref)
            if data:
                cache.set(f'np:wh:{ref}', data, NP_WH_CACHE_TTL)
                ok += 1
                self.stdout.write(f'  {name}: {len(data)} points cached')
            else:
                self.stderr.write(f'  {name}: Nova Poshta load failed')
            time.sleep(0.6)

        msg = f'Cities warmed up: {ok}/{len(TOP_CITIES)}'
        log.info('warmup_np_cache: %s', msg)
        self.stdout.write(self.style.SUCCESS(msg))
