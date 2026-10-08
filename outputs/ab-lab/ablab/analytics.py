"""Read-only summaries of retained visit events for the admin dashboard."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3


ALLOWED = {'allowed', 'pass', 'whitelist', 'protection_off'}
BLOCKED = {'blacklist', 'super_bot', 'strict_bot', 'bot_marker', 'ipv4', 'device', 'os_version', 'blocked_cidr',
           'country_unknown', 'country', 'language', 'visit_limit'}
FIELDS = ('total', 'allowed', 'blocked', 'other')


def counts():
    return dict.fromkeys(FIELDS, 0)


def add(bucket, outcome):
    bucket['total'] += 1
    bucket[outcome] += 1


def summarize(registry, site_id, period, scope, tz_offset, now, owner_id=None):
    zone = timezone(timedelta(minutes=tz_offset))
    local_now = datetime.fromtimestamp(now, zone)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    hourly = period in ('today', 'yesterday')
    if period == 'yesterday':
        start_local, end_local = midnight - timedelta(days=1), midnight
    elif period == 'today':
        start_local, end_local = midnight, local_now
    else:
        start_local, end_local = midnight - timedelta(days=int(period[:-1]) - 1), local_now
    start = start_local.astimezone(timezone.utc).timestamp()
    end = end_local.astimezone(timezone.utc).timestamp()
    labels = ([f'{hour:02d}:00' for hour in range(24)] if hourly else
              [(start_local + timedelta(days=day)).date().isoformat() for day in range(int(period[:-1]))])
    trend = {label: {'label': label, **counts()} for label in labels}
    sites = registry.list(owner_id) if scope == 'all' else [registry.get(site_id)]
    summary = counts() | {'rate': 0.0, 'countries': 0, 'a': 0, 'b': 0}
    domains, countries, reasons, recent = [], {}, Counter(), []

    for site in sites:
        domain = {'id': site['id'], 'domain': site['domain'], **counts()}
        if site['id'] != 'default' and not re.fullmatch('[a-f0-9]{32}', site['id']):
            raise ValueError('站点ID格式错误')
        db_path = (registry.root if site['id'] == 'default' else registry.root / 'sites' / site['id']) / 'app.db'
        if not db_path.exists():
            domains.append(domain)
            continue
        with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            comparison = '<' if period == 'yesterday' else '<='
            details_field = ('device_details' if any(row['name'] == 'device_details' for row in db.execute('PRAGMA table_info(events)'))
                             else 'NULL AS device_details')
            rows = db.execute(
                f'SELECT id,created,ip,country,device,slot,reason,path,mode,{details_field} '
                f'FROM events WHERE created>=? AND created{comparison}? ORDER BY created DESC,id DESC',
                (start, end),
            )
            from .web import shown_country
            for row in rows:
                item = dict(row)
                item['country'] = shown_country(item.get('ip'), item.get('country'))
                reason = item['reason']
                outcome = 'allowed' if reason in ALLOWED else 'blocked' if reason in BLOCKED else 'other'
                add(summary, outcome)
                add(domain, outcome)
                code = item['country']
                if not isinstance(code, str) or not re.fullmatch('[A-Z]{2}', code) or code in ('XX', 'ZZ'):
                    code = None
                add(countries.setdefault(code, {'code': code, **counts()}), outcome)
                if outcome == 'blocked':
                    reasons[reason] += 1
                if item['slot'] == 'A':
                    summary['a'] += 1
                elif item['slot'] == 'B':
                    summary['b'] += 1
                local = datetime.fromtimestamp(item['created'], zone)
                label = f'{local.hour:02d}:00' if hourly else local.date().isoformat()
                add(trend[label], outcome)
                if len(recent) < 8 or item['created'] > recent[-1]['created']:
                    item['device_details'] = json.loads(item['device_details']) if item['device_details'] else None
                    item.update(site_id=site['id'], domain=site['domain'], outcome=outcome)
                    recent.append(item)
                    recent.sort(key=lambda entry: (entry['created'], entry['id']), reverse=True)
                    del recent[8:]
        domains.append(domain)

    summary['rate'] = round(100 * summary['allowed'] / summary['total'], 1) if summary['total'] else 0.0
    summary['countries'] = sum(code is not None for code in countries)
    return {
        'period': period, 'scope': scope, 'tz_offset': tz_offset,
        'start': datetime.fromtimestamp(start, timezone.utc).isoformat(),
        'end': datetime.fromtimestamp(end, timezone.utc).isoformat(),
        'summary': summary, 'trend': list(trend.values()), 'domains': domains,
        'countries': sorted(countries.values(), key=lambda row: (-row['total'], row['code'] is None, row['code'] or '')),
        'reasons': [{'reason': reason, 'count': count} for reason, count in sorted(reasons.items(), key=lambda pair: (-pair[1], pair[0]))],
        'recent': recent, 'retention': {'days': 30, 'max_events_per_site': 10000},
    }
