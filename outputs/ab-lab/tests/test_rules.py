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


IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS {os} like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{safari} Mobile/15E148 Safari/604.1'
ANDROID = 'Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'
CUBOT = 'Mozilla/5.0 (Linux; Android 11; CUBOT KINGKONG) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'


def test_ios_versions_above_18_stay_exact_and_hidden_versions_say_above():
    exact = decide(Config(), visit(ua=IPHONE.format(os='26_0', safari='26.0')))
    assert exact['device_details']['os'] == 'iOS 26.0'
    assert exact['device_details']['browser'] == 'Safari 26.0'
    frozen = decide(Config(), visit(ua=IPHONE.format(os='18_7', safari='26.1')))
    assert frozen['device_details']['os'] == 'iOS 26 以上'
    assert frozen['device_details']['browser'] == 'Safari 26.1'
    current = decide(Config(), visit(ua=IPHONE.format(os='18_7', safari='18.7.5')))
    assert current['device_details']['os'] == 'iOS 18.7'
    assert current['device_details']['browser'] == 'Safari 18.7.5'
    hidden = decide(Config(), visit(ua='Mozilla/5.0 (iPhone; CPU iPhone OS like Mac OS X) Mobile'))
    assert hidden['device_details']['os'] == 'iOS 18 以上'
    assert '未知' not in ''.join(hidden['device_details'].values())


def test_unparsed_ios_counts_as_18_and_a_higher_safari_version_counts_as_itself():
    hidden = 'Mozilla/5.0 (iPhone; CPU iPhone OS like Mac OS X) Mobile'
    assert decide(Config(rules={'min_ios': 18}), visit(ua=hidden))['reason'] == 'allowed'
    assert decide(Config(rules={'min_ios': 19}), visit(ua=hidden))['reason'] == 'os_version'
    frozen = IPHONE.format(os='18_7', safari='26.0')
    assert decide(Config(rules={'min_ios': 26}), visit(ua=frozen))['reason'] == 'allowed'
    assert decide(Config(rules={'min_ios': 27}), visit(ua=frozen))['reason'] == 'os_version'
    assert decide(Config(rules={'min_ios': 18}), visit(ua=IPHONE.format(os='26_0', safari='26.0')))['reason'] == 'allowed'


@pytest.mark.parametrize('ua', [
    'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
    'Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36 (compatible; Googlebot/2.1)',
    'curl/8.6.0',
    '',
    'HelloScanner/1.0',
    'python-requests/2.32',
])
def test_strict_crawler_blocks_bots_empty_and_unrecognized_clients(ua):
    result = decide(Config(rules={'strict_bots': True}), visit(ua=ua))
    assert result['slot'] == 'A'
    assert result['reason'] == 'strict_bot'


def test_super_crawler_blocks_official_ranges_and_named_bots_only_when_enabled():
    iphone = IPHONE.format(os='18_7', safari='18.7.5')
    googlebot = 'Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36 (compatible; Googlebot/2.1)'
    assert decide(Config(), visit(ip='66.249.66.1', ua=iphone))['reason'] == 'allowed'
    disguised = decide(Config(rules={'super_bots': True}), visit(ip='66.249.66.1', ua=iphone))
    assert disguised['slot'] == 'A'
    assert disguised['reason'] == 'super_bot'
    named = decide(Config(rules={'super_bots': True}), visit(ua=googlebot))
    assert named['reason'] == 'super_bot'
    assert decide(Config(rules={'super_bots': True}), visit(ip='157.55.39.5', ua=iphone))['reason'] == 'allowed'
    assert decide(Config(rules={'super_bots': True}), visit(ua=iphone))['reason'] == 'allowed'
    assert decide(Config(rules={'super_bots': True}), visit(ua=CUBOT))['reason'] == 'allowed'
    assert decide(Config(rules={'super_bots': True}), visit(ip='8.8.8.8', ua=iphone))['reason'] == 'allowed'
    assert decide(Config(rules={'super_bots': True}), visit(ip='3.81.245.78', ua=iphone))['reason'] == 'allowed'
    assert decide(Config(rules={'super_bots': True}), visit(ua='Mozilla/5.0 (compatible; Perplexity-User/1.0)'))['reason'] == 'allowed'
    assert decide(Config(rules={'super_bots': True}), visit(ua='Mozilla/5.0 (compatible; AdsBot-Google; +http://www.google.com/adsbot.html)'))['reason'] == 'super_bot'
    assert decide(Config(rules={'super_bots': True, 'whitelist': ['66.249.66.1']}), visit(ip='66.249.66.1', ua=iphone))['reason'] == 'whitelist'


def test_strict_crawler_leaves_normal_phones_and_stays_off_by_default():
    iphone = IPHONE.format(os='18_7', safari='18.7.5')
    assert decide(Config(rules={'strict_bots': True}), visit(ua=iphone))['reason'] == 'allowed'
    assert decide(Config(rules={'strict_bots': True}), visit(ua=ANDROID))['reason'] == 'allowed'
    assert decide(Config(rules={'strict_bots': True}), visit(ua=CUBOT))['reason'] == 'allowed'
    assert decide(Config(), visit(ua='Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'))['reason'] == 'allowed'


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
