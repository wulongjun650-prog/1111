import pytest
from ablab.models import Config, Visitor
from ablab.rules import decide


def visit(**changes):
    return Visitor(**({'ip': '203.0.113.7', 'ua': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) Mobile', 'country': 'HK', 'language': 'zh-HK,zh;q=0.9', 'visits': 1} | changes))


@pytest.mark.parametrize('routing,protection,expected', [('FORCE_A', False, 'A'), ('FORCE_B', True, 'B'), ('RULES', False, 'B')])
def test_manual_modes_precede_filters(routing, protection, expected):
    config = Config(routing=routing, protection=protection, rules={'blacklist': ['203.0.113.7']})
    assert decide(config, visit())['slot'] == expected


def test_blacklist_wins_overlap_and_whitelist_bypasses_other_filters():
    config = Config(rules={'blacklist': ['203.0.113.0/24'], 'whitelist': ['203.0.113.7'], 'block_ipv4': True})
    assert decide(config, visit())['reason'] == 'blacklist'
    config.rules.blacklist = []
    assert decide(config, visit())['reason'] == 'whitelist'


@pytest.mark.parametrize('rules,visitor,reason', [
    ({'block_bots': True}, {'ua': 'ExampleCrawler/1'}, 'bot_marker'),
    ({'block_ipv4': True}, {}, 'ipv4'),
    ({'block_pc': True}, {'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}, 'device'),
    ({'block_pc': True}, {'ua': ''}, 'device'),
    ({'min_ios': 18}, {}, 'os_version'),
    ({'min_android': 11}, {'ua': 'Mozilla/5.0 (Linux; Android 9) Mobile'}, 'os_version'),
    ({'blocked_cidrs': ['203.0.113.0/24']}, {}, 'blocked_cidr'),
    ({'countries': ['US']}, {}, 'country'),
    ({'countries': ['HK']}, {'country': None}, 'country_unknown'),
    ({'languages': ['en']}, {}, 'language'),
    ({'languages': ['en']}, {'language': 'en;q=0,zh;q=1'}, 'language'),
    ({'max_visits': 2}, {'visits': 3}, 'visit_limit'),
])
def test_each_filter_returns_explainable_block(rules, visitor, reason):
    result = decide(Config(rules=rules), visit(**visitor))
    assert result['slot'] == 'A'
    assert result['reason'] == reason
    assert result['trace'][-1]['status'] == 'block'


def test_first_n_allowed_and_language_prefix_matches():
    result = decide(Config(rules={'max_visits': 2, 'languages': ['zh'], 'countries': ['HK']}), visit(visits=2))
    assert result['slot'] == 'B'


def test_unknown_country_is_not_a_block_when_filter_disabled():
    assert decide(Config(), visit(country=None))['slot'] == 'B'


@pytest.mark.parametrize('slot', ['A', 'B'])
def test_allowed_slot_changes_only_passed_visitors(slot):
    config = Config(allowed_slot=slot, rules={'block_pc': True})
    passed = decide(config, visit())
    assert passed['slot'] == slot
    assert passed['reason'] == 'allowed'
    assert passed['trace'][-1]['status'] == 'pass'
    blocked = decide(config, visit(ua='Mozilla/5.0 (Windows NT 10.0)'))
    assert blocked['slot'] == 'A'
    assert blocked['reason'] == 'device'
    assert blocked['trace'][-1]['status'] == 'block'


def test_whitelist_follows_allowed_slot_but_blacklist_still_wins():
    config = Config(allowed_slot='A', rules={'whitelist': ['203.0.113.7'], 'block_ipv4': True})
    result = decide(config, visit())
    assert (result['slot'], result['reason'], result['trace'][-1]['status']) == ('A', 'whitelist', 'pass')
    config.allowed_slot = 'B'
    config.rules.blacklist = ['203.0.113.7']
    result = decide(config, visit())
    assert (result['slot'], result['reason']) == ('A', 'blacklist')


def test_protection_off_follows_selected_content_and_global_override_still_wins():
    config = Config(allowed_slot='A', protection=False, rules={'block_ipv4': True})
    result = decide(config, visit())
    assert (result['slot'], result['reason'], result['trace'][-1]['status']) == ('A', 'protection_off', 'pass')
    config.routing = 'FORCE_B'
    assert decide(config, visit())['slot'] == 'B'


def test_invalid_allowed_slot_rejected():
    with pytest.raises(ValueError):
        Config(allowed_slot='C')


@pytest.mark.parametrize('bad', [{'rules': {'blacklist': ['garbage']}}, {'rules': {'countries': ['HKG']}}, {'rules': {'max_visits': -1}}, {'routing': 'OTHER'}, {'unexpected': True}])
def test_invalid_configuration_rejected(bad):
    with pytest.raises(ValueError):
        Config(**bad)
