import hashlib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
import ablab.store as store_module
from ablab.store import Store


def test_counter_boundary_reset_and_ip_canonicalization(tmp_path):
    db = Store(tmp_path)
    assert db.count('2001:db8::1', 24, True, now=100) == 1
    assert db.count('2001:0db8::1', 24, True, now=101) == 2
    assert db.count('2001:db8::1', 24, False, now=102) == 2
    assert db.count('2001:db8::1', 24, True, now=86500) == 1
    db.reset_counts()
    assert db.count('2001:db8::1', 24, False, now=86501) == 0


def test_counter_increment_is_atomic_across_connections(tmp_path):
    db = Store(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(lambda _: db.count('203.0.113.7', 24, True, now=100), range(30)))
    assert sorted(counts) == list(range(1, 31))


def _add_version(store, slot, name):
    version_id = hashlib.sha256(name.encode()).hexdigest()[:32]
    page = store.pages / version_id
    page.mkdir()
    (page / 'index.html').write_text(name, encoding='utf-8')
    store.add_version(slot, {'id': version_id, 'name': name, 'created': 1, 'files': 1, 'bytes': len(name), 'sha256': 'a' * 64})
    return version_id


def test_delete_unpublished_b_version_removes_files_and_stale_active_link(tmp_path):
    store = Store(tmp_path)
    live = _add_version(store, 'B', 'live')
    old = _add_version(store, 'B', 'old')
    other = _add_version(store, 'A', 'alpha')
    store.publish('B', live)
    with store.connect() as db:
        db.execute('INSERT INTO redirect_active VALUES(1,?,?,?,?)', ('https://example.com/', None, old, 1))
    assert store.delete_version(old) == old
    assert store.version(old) is None
    assert not (store.pages / old).exists()
    assert store.redirect_active() is None
    assert store.slots()['B'] == live
    assert (store.pages / live / 'index.html').is_file()
    with pytest.raises(ValueError, match='当前发布'):
        store.delete_version(live)
    with pytest.raises(ValueError, match='只能删除未发布的 B'):
        store.delete_version(other)
    extra = _add_version(store, 'B', 'extra')
    assert store.delete_unpublished_b_versions() == [extra]
    assert store.version(extra) is None
    assert store.version(live)['slot'] == 'B'
    assert store.version(other)['slot'] == 'A'
    assert any(item['action'] == 'b_version_deleted' for item in store.audit())


def _member(preset, weight=1):
    return SimpleNamespace(preset_id=preset['id'], weight=weight)


def test_split_assignment_sticks_to_the_first_destination(tmp_path, monkeypatch):
    store = Store(tmp_path)
    first, second = store.add_redirect_presets(['https://one.example/a', 'https://two.example/b'], '')
    chosen_urls = ['https://one.example/a', 'https://two.example/b']

    def choose(members, mode):
        url = chosen_urls.pop(0)
        return next(item for item in members if item['url'] == url)

    monkeypatch.setattr(store_module, 'choose_split_member', choose)
    store.save_redirect_split(True, 'weighted', [_member(first, 70), _member(second, 30)])
    assert store.split_destination('a' * 64) == 'https://one.example/a'
    assert store.split_destination('a' * 64) == 'https://one.example/a'
    assert store.split_destination('b' * 64) == 'https://two.example/b'
    store.save_redirect_split(True, 'random', [_member(second)])
    assert store.split_destination('a' * 64) == 'https://one.example/a'
    store.save_redirect_split(False, 'random', [_member(second)])
    assert store.split_destination('b' * 64) is None
    with pytest.raises(ValueError, match='至少选择一条'):
        store.save_redirect_split(True, 'random', [])


def _wa_member(number_id, weight=1):
    return SimpleNamespace(number_id=number_id, weight=weight)


def test_retired_number_leaves_the_split_rotation(tmp_path, monkeypatch):
    store = Store(tmp_path)
    keep = store.add_whatsapp_numbers(['85211112222'], '', 'Vivian')[0]
    drop = store.add_whatsapp_numbers(['85233334444'], '', 'Chloe')[0]
    monkeypatch.setattr(store_module, 'choose_split_member', lambda members, mode: next(m for m in members if m['number_id'] == drop['id']))
    store.save_whatsapp_split(True, 'random', [_wa_member(keep['id']), _wa_member(drop['id'])])
    first = store.whatsapp_split_destination('c' * 64)
    assert first['phone'] == '85233334444'
    # Pin the only remaining pick to the kept number, then retire the assigned one.
    monkeypatch.setattr(store_module, 'choose_split_member', lambda members, mode: next(m for m in members if m['number_id'] == keep['id']))
    store.save_whatsapp_split(True, 'random', [_wa_member(keep['id'])])
    store.delete_whatsapp_number(drop['id'])
    again = store.whatsapp_split_destination('c' * 64)
    assert again['phone'] == '85211112222' and again['number_id'] == keep['id']


def test_trust_polling_stays_on_after_both_screens_were_seen(tmp_path):
    store = Store(tmp_path)
    a = store.add_whatsapp_numbers(['85211112222'], '', 'Vivian')[0]
    b = store.add_whatsapp_numbers(['85233334444'], '', 'Chloe')[0]
    assert store.whatsapp_trust_poll_enabled() is False
    store.save_whatsapp_trust(a['id'], 'trust', 'trust')
    assert store.whatsapp_trust_poll_enabled() is False
    store.save_whatsapp_trust(b['id'], 'clear', 'clear')
    assert store.whatsapp_trust_poll_enabled() is True
    # Both now show a trust popup: the old live-status rule would turn polling off,
    # but once both screens were ever seen it must keep watching.
    store.save_whatsapp_trust(a['id'], 'trust', 'trust')
    store.save_whatsapp_trust(b['id'], 'trust', 'trust')
    assert store.whatsapp_trust_poll_enabled() is True


def test_link_round_robin_and_equal_distribution(tmp_path):
    db = Store(tmp_path)
    db.add_links('B', ['https://example.com/1', 'https://example.com/2'])
    assert [db.choose_link('B', 'round_robin')['url'] for _ in range(3)] == ['https://example.com/1', 'https://example.com/2', 'https://example.com/1']
    assert db.choose_link('B', 'equal')['url'] == 'https://example.com/2'
    assert [x['hits'] for x in db.links()] == [2, 2]
