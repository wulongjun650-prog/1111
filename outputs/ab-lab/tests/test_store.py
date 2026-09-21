from concurrent.futures import ThreadPoolExecutor
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


def test_link_round_robin_and_equal_distribution(tmp_path):
    db = Store(tmp_path)
    db.add_links('B', ['https://example.com/1', 'https://example.com/2'])
    assert [db.choose_link('B', 'round_robin')['url'] for _ in range(3)] == ['https://example.com/1', 'https://example.com/2', 'https://example.com/1']
    assert db.choose_link('B', 'equal')['url'] == 'https://example.com/2'
    assert [x['hits'] for x in db.links()] == [2, 2]
