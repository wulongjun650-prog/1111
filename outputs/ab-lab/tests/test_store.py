import hashlib
from concurrent.futures import ThreadPoolExecutor
import pytest
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


def test_link_round_robin_and_equal_distribution(tmp_path):
    db = Store(tmp_path)
    db.add_links('B', ['https://example.com/1', 'https://example.com/2'])
    assert [db.choose_link('B', 'round_robin')['url'] for _ in range(3)] == ['https://example.com/1', 'https://example.com/2', 'https://example.com/1']
    assert db.choose_link('B', 'equal')['url'] == 'https://example.com/2'
    assert [x['hits'] for x in db.links()] == [2, 2]
