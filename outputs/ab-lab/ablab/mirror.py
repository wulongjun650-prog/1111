"""Copy the live page and jump settings onto an observer domain.

Visit rows stay on the source. The observer domain only reads them.
HTML copies clear directMode so the observer page stays on the questionnaire.
"""
import hashlib
import json
from pathlib import Path

from .archives import source_files, write_bundle
from .redirects import blank_direct_mode

_TEXT_SUFFIXES = {'.html', '.htm', '.js', '.mjs'}


def sync_mirror_content(source, mirror):
    """Make the mirror serve the source page, numbers, and links. Return whether anything changed."""
    changed = _sync_config(source, mirror)
    changed = _sync_slot(source, mirror, 'A') or changed
    changed = _sync_slot(source, mirror, 'B') or changed
    changed = _sync_pool(source, mirror) or changed
    return changed


def _sync_config(source, mirror):
    src, _revision = source.config()
    dst, revision = mirror.config()
    if src.model_dump() == dst.model_dump():
        return False
    mirror.save_config(src, revision)
    return True


def _blank_bytes(name, data):
    if Path(name).suffix.lower() not in _TEXT_SUFFIXES:
        return data
    bom = data.startswith(b'\xef\xbb\xbf')
    raw = data[3:] if bom else data
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        return data
    updated = blank_direct_mode(text)
    if updated == text:
        return data
    payload = updated.encode('utf-8')
    return (b'\xef\xbb\xbf' + payload) if bom else payload


def _bundle_files(store, version_id):
    files = source_files(store.pages / version_id)
    copied = []
    for name in sorted(files):
        path, _size = files[name]
        copied.append((name, _blank_bytes(name, path.read_bytes())))
    return copied


def _digest(files):
    digest = hashlib.sha256()
    for name, payload in files:
        digest.update(name.encode() + b'\0' + str(len(payload)).encode() + b'\0' + payload)
    return digest.hexdigest()


def _sync_slot(source, mirror, slot):
    published = source.slots().get(slot)
    if not published:
        return _clear_slot(mirror, slot)
    version = source.version(published)
    if not version:
        return _clear_slot(mirror, slot)
    files = _bundle_files(source, published)
    digest = _digest(files)
    current = mirror.slots().get(slot)
    if current:
        existing = mirror.version(current)
        if existing and existing['sha256'] == digest:
            return False
    bundle = write_bundle(files, version['name'], mirror.pages, digest)
    mirror.add_version(slot, bundle)
    mirror.publish(slot, bundle['id'])
    return True


def _clear_slot(store, slot):
    with store.connect() as db:
        current = db.execute('SELECT version_id FROM slots WHERE slot=?', (slot,)).fetchone()[0]
        if not current:
            return False
        db.execute('UPDATE slots SET version_id=NULL WHERE slot=?', (slot,))
    return True


def _pool_snapshot(store):
    published_b = bool(store.slots().get('B'))
    with store.connect() as db:
        numbers = [dict(row) for row in db.execute(
            'SELECT id,phone,note,display_name,trust_result FROM whatsapp_numbers ORDER BY phone,id')]
        presets = [dict(row) for row in db.execute(
            'SELECT id,url,note,check_result FROM redirect_presets ORDER BY url,id')]
        contacts = [dict(row) for row in db.execute(
            'SELECT app,url,note FROM contact_links ORDER BY app,url')]
        links = [dict(row) for row in db.execute(
            'SELECT slot,url,hits FROM links ORDER BY slot,url')]
        number_of = {row['id']: row['phone'] for row in numbers}
        preset_of = {row['id']: row['url'] for row in presets}
        wa = db.execute('SELECT enabled,mode,members FROM whatsapp_split WHERE id=1').fetchone()
        redirect = db.execute('SELECT enabled,mode,members FROM redirect_split WHERE id=1').fetchone()
        active = db.execute('SELECT url FROM redirect_active WHERE id=1').fetchone()
    return {
        'numbers': [{'phone': row['phone'], 'note': row['note'], 'display_name': row['display_name'], 'trust_result': row['trust_result']} for row in numbers],
        'presets': [{'url': row['url'], 'note': row['note'], 'check_result': row['check_result']} for row in presets],
        'contacts': contacts,
        'links': links,
        'whatsapp_split': _members(wa, 'number_id', number_of),
        'redirect_split': _members(redirect, 'preset_id', preset_of),
        'active_url': active['url'] if active and published_b else None,
    }


def _members(row, key, mapping):
    if row is None:
        return None
    items = []
    for item in json.loads(row['members']):
        items.append({'value': mapping.get(item[key], ''), 'weight': item['weight']})
    items.sort(key=lambda item: (item['value'], item['weight']))
    return {'enabled': row['enabled'], 'mode': row['mode'], 'members': items}


def _sync_pool(source, mirror):
    if _pool_snapshot(source) == _pool_snapshot(mirror):
        return False
    with source.connect() as src:
        numbers = [dict(row) for row in src.execute(
            'SELECT id,phone,note,created,trust_result,display_name FROM whatsapp_numbers ORDER BY id')]
        presets = [dict(row) for row in src.execute(
            'SELECT id,url,note,created,check_result FROM redirect_presets ORDER BY id')]
        contacts = [dict(row) for row in src.execute(
            'SELECT app,url,note,created FROM contact_links ORDER BY id')]
        links = [dict(row) for row in src.execute('SELECT slot,url,hits FROM links ORDER BY id')]
        wa = src.execute('SELECT enabled,mode,members FROM whatsapp_split WHERE id=1').fetchone()
        redirect = src.execute('SELECT enabled,mode,members FROM redirect_split WHERE id=1').fetchone()
        active = src.execute('SELECT url,preset_id FROM redirect_active WHERE id=1').fetchone()
        wa = dict(wa) if wa else None
        redirect = dict(redirect) if redirect else None
        active = dict(active) if active else None
    published_b = mirror.slots().get('B')
    with mirror.connect() as dst:
        dst.execute('BEGIN IMMEDIATE')
        dst.execute('DELETE FROM redirect_active')
        dst.execute('DELETE FROM redirect_split')
        dst.execute('DELETE FROM whatsapp_split')
        dst.execute('DELETE FROM redirect_split_assign')
        dst.execute('DELETE FROM whatsapp_split_assign')
        dst.execute('DELETE FROM rotation')
        dst.execute('DELETE FROM whatsapp_numbers')
        dst.execute('DELETE FROM redirect_presets')
        dst.execute('DELETE FROM contact_links')
        dst.execute('DELETE FROM links')
        number_map = {}
        for row in numbers:
            cursor = dst.execute(
                'INSERT INTO whatsapp_numbers(phone,note,created,trust_result,display_name) VALUES(?,?,?,?,?)',
                (row['phone'], row['note'], row['created'], row['trust_result'], row['display_name']),
            )
            number_map[row['id']] = cursor.lastrowid
        preset_map = {}
        for row in presets:
            cursor = dst.execute(
                'INSERT INTO redirect_presets(url,note,created,check_result) VALUES(?,?,?,?)',
                (row['url'], row['note'], row['created'], row['check_result']),
            )
            preset_map[row['id']] = cursor.lastrowid
        dst.executemany(
            'INSERT INTO contact_links(app,url,note,created) VALUES(?,?,?,?)',
            [(row['app'], row['url'], row['note'], row['created']) for row in contacts],
        )
        dst.executemany(
            'INSERT INTO links(slot,url,hits) VALUES(?,?,?)',
            [(row['slot'], row['url'], row['hits']) for row in links],
        )
        if wa:
            dst.execute(
                'INSERT INTO whatsapp_split(id,enabled,mode,members,updated) VALUES(1,?,?,?,?)',
                (wa['enabled'], wa['mode'], _remap_members(wa['members'], 'number_id', number_map), 0),
            )
        if redirect:
            dst.execute(
                'INSERT INTO redirect_split(id,enabled,mode,members,updated) VALUES(1,?,?,?,?)',
                (redirect['enabled'], redirect['mode'], _remap_members(redirect['members'], 'preset_id', preset_map), 0),
            )
        if active and published_b:
            preset_id = preset_map.get(active['preset_id'])
            dst.execute(
                'INSERT INTO redirect_active(id,url,preset_id,version_id,updated) VALUES(1,?,?,?,?)',
                (active['url'], preset_id, published_b, 0),
            )
    return True


def _remap_members(payload, key, mapping):
    members = []
    for item in json.loads(payload):
        mapped = mapping.get(item[key])
        if mapped:
            members.append({key: mapped, 'weight': item['weight']})
    return json.dumps(members, ensure_ascii=False)
