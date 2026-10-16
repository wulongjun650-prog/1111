"""Copy visit rows onto an observer domain before its source domain is deleted.

Pages, numbers, and settings stay where they are. The copies live in kept_*
tables, which the landing process does not trim.
"""
import sqlite3
from contextlib import closing
from pathlib import Path


_PAIRS = (
    ('events', 'kept_events'),
    ('access_log', 'kept_access_log'),
    ('client_events', 'kept_client_events'),
    ('intake_log', 'kept_intake_log'),
)


def keep_visit_records(source, mirror, source_domain='', mirror_domain=''):
    """Append this domain's visit rows onto the observer domain. Return the counts copied."""
    return _copy_visits(source, mirror, source_domain, mirror_domain, include_kept=False)


def import_visit_records(source, mirror, source_domain='', mirror_domain=''):
    """Copy one domain's visit rows onto an observer domain. The source rows stay."""
    return _copy_visits(source, mirror, source_domain, mirror_domain, include_kept=True)


def _copy_visits(source, mirror, source_domain, mirror_domain, include_kept):
    counts = {}
    with source.connect() as src, mirror.connect() as dst:
        for table, kept in _PAIRS:
            counts[table] = _copy(src, dst, table, kept, source_domain, mirror_domain)
            if include_kept:
                counts[table] += _copy(src, dst, kept, kept, source_domain, mirror_domain)
    return counts


def has_kept(store):
    with store.connect() as db:
        for _table, kept in _PAIRS:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (kept,)).fetchone():
                continue
            if db.execute(f'SELECT 1 FROM {kept} LIMIT 1').fetchone():
                return True
    return False


def kept_event_rows(db_path, start, end, comparison):
    """Visit rows saved on this domain from a source that was deleted."""
    path = Path(db_path)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kept_events'").fetchone():
            return []
        names = {row['name'] for row in db.execute('PRAGMA table_info(kept_events)')}
        details = 'device_details' if 'device_details' in names else 'NULL AS device_details'
        rows = db.execute(
            f'SELECT id,created,ip,country,device,slot,reason,path,mode,{details} '
            f'FROM kept_events WHERE created>=? AND created{comparison}? ORDER BY created DESC, id DESC',
            (start, end),
        ).fetchall()
    return [dict(row) for row in rows]


class PreservedView:
    """Source rows, plus visit rows kept on the observer domain."""

    def __init__(self, source, mirror):
        self.source = source
        self.mirror = mirror

    def stats(self):
        base = dict(self.source.stats())
        extra = self.mirror.stats(only_kept=True)
        for key, value in extra.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                base[key] = (base.get(key) or 0) + value
        return base

    def recent_count(self, seconds=60):
        return self.source.recent_count(seconds) + self.mirror.recent_count(seconds, only_kept=True)

    def logs(self, days=7, slot='', page=1, page_size=25):
        left = self.source.logs(days, slot, 1, 10000)
        right = self.mirror.logs(days, slot, 1, 10000, only_kept=True)
        items = left['items'] + right['items']
        items.sort(key=lambda row: (row.get('created') or 0, row.get('id') or 0), reverse=True)
        total = len(items)
        start = (page - 1) * page_size
        return {
            'items': items[start:start + page_size],
            'total': total,
            'page': page,
            'pages': max(1, (total + page_size - 1) // page_size),
        }

    def recent_access(self, since, limit):
        total_a, rows_a = self.source.recent_access(since, limit)
        total_b, rows_b = self.mirror.recent_access(since, limit, only_kept=True)
        rows = sorted(rows_a + rows_b, key=lambda row: row.get('created') or 0, reverse=True)
        return total_a + total_b, rows[:limit]

    def recent_client_events(self, since, limit):
        total_a, rows_a = self.source.recent_client_events(since, limit)
        total_b, rows_b = self.mirror.recent_client_events(since, limit, only_kept=True)
        rows = sorted(rows_a + rows_b, key=lambda row: row.get('created') or 0, reverse=True)
        return total_a + total_b, rows[:limit]

    def campaign_access(self, since, until):
        rows = self.source.campaign_access(since, until) + self.mirror.campaign_access(since, until, only_kept=True)
        rows.sort(key=lambda row: row.get('created') or 0)
        return rows

    def campaign_events(self, since, until):
        rows = self.source.campaign_events(since, until) + self.mirror.campaign_events(since, until, only_kept=True)
        rows.sort(key=lambda row: (row.get('created') or 0, row.get('id') or 0))
        return rows

    def intake_between(self, since, until):
        rows = self.source.intake_between(since, until) + self.mirror.intake_between(since, until, only_kept=True)
        rows.sort(key=lambda row: (row.get('created') or 0, row.get('id') or 0))
        return rows

    def access_report(self, days=7, country=None, domain='', rename_from=''):
        text = self.source.access_report(days, country, domain, rename_from)
        kept_access, _rows = self.mirror.recent_access(0, 1, only_kept=True)
        kept_events, _events = self.mirror.recent_client_events(0, 1, only_kept=True)
        if not kept_access and not kept_events:
            return text
        extra = self.mirror.access_report(days, country, domain, '', only_kept=True)
        return text + '\n# 保留在观察域名上的记录\n' + extra


def _columns(db, table):
    return [row['name'] for row in db.execute(f'PRAGMA table_info({table})')]


def _copy(src, dst, table, kept, source_domain, mirror_domain):
    if not src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return 0
    if not dst.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (kept,)).fetchone():
        return 0
    shared = [name for name in _columns(src, table) if name != 'id' and name in set(_columns(dst, kept))]
    if not shared:
        return 0
    listed = ','.join(shared)
    rows = src.execute(f'SELECT {listed} FROM {table}').fetchall()
    if not rows:
        return 0
    replace = bool(source_domain) and source_domain != mirror_domain
    placeholders = ','.join('?' * len(shared))
    copied = 0
    for row in rows:
        values = []
        for name, value in zip(shared, row):
            if replace and isinstance(value, str):
                value = value.replace(source_domain, mirror_domain)
            values.append(value)
        dst.execute(f'INSERT INTO {kept}({listed}) VALUES({placeholders})', values)
        copied += 1
    return copied
