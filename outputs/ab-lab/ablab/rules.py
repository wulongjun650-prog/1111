"""Deterministic lab rules. UA is a claim, never verified identity."""
import ipaddress
import re
from .models import Config, Visitor
from .crawlers import is_crawler_ip
from .devices import describe_device, ios_major


# Names that identify crawlers. A bare "bot" substring is intentionally absent:
# phone brands such as Cubot contain those letters.
_CRAWLER_MARKERS = (
    'googlebot', 'adsbot-google', 'mediapartners-google', 'storebot-google',
    'bingbot', 'bingpreview', 'msnbot', 'adidxbot', 'baiduspider', 'yandexbot',
    'yandex.com/bots', 'duckduckbot', 'applebot', 'amazonbot', 'petalbot',
    'bytespider', 'ahrefsbot', 'ahrefs', 'semrushbot', 'semrush', 'dotbot',
    'mj12bot', 'rogerbot', 'screaming frog', 'gptbot', 'chatgpt-user',
    'oai-searchbot', 'claudebot', 'claude-user', 'anthropic-ai', 'ccbot',
    'perplexitybot', 'facebookexternalhit', 'facebot', 'twitterbot',
    'linkedinbot', 'slackbot', 'telegrambot', 'discordbot', 'embedly',
    'ia_archiver', 'archive.org_bot', 'sogou web spider', 'sogou pic spider',
    'sogou inst spider', '360spider', 'yisouspider',
    'haosouspider', 'tiktokspider', 'dataforseobot', 'blexbot', 'megaindex',
    'googleother', 'google-inspectiontool', 'google-cloudvertexbot', 'google-safety',
    'duckassistbot', 'claude-searchbot', 'meta-externalagent', 'meta-webindexer',
    'amzn-searchbot', 'amzn-user', 'perplexity-user', 'mistralai-user',
    'ttd-content', 'flipboardproxy', 'parsely', 'yeti/', 'google-agent',
    'imagesiftbot', 'omgilibot', 'diffbot',
    'crawler', 'spider', 'headlesschrome', 'phantomjs', 'selenium',
    'puppeteer', 'playwright', 'curl/', 'wget/', 'python-requests',
    'python-urllib', 'scrapy', 'go-http-client', 'httpclient', 'libwww',
    'okhttp', 'axios/', 'node-fetch', 'postmanruntime', 'httpx/', 'aiohttp',
    'java/', 'sqlmap', 'zgrab', 'masscan', 'colly', 'fasthttp', 'libcurl',
)
_BOT_TOKEN = re.compile(r'(?<![a-z0-9])([a-z0-9]*bot)(?:/|\b)')
_BOT_NAMES = {'cubot'}


def device_info(ua):
    text = ua.lower()
    if any(marker in text for marker in ('android', 'iphone', 'ipad', 'ipod', 'mobile')):
        device = 'mobile'
    elif any(marker in text for marker in ('windows nt', 'macintosh', 'x11', 'cros')):
        device = 'desktop'
    else:
        device = 'unknown'
    android = re.search(r'android\s+(\d+)', text)
    return device, int(android[1]) if android else None, ios_major(ua)


_GOOGLE_MARKERS = (
    'googlebot', 'adsbot-google', 'mediapartners-google', 'storebot-google',
    'googleother', 'google-inspectiontool', 'google-cloudvertexbot', 'google-safety',
    'google-agent', 'feedfetcher-google', 'apis-google', 'duplexweb-google',
    'google-read-aloud', 'google-site-verification',
)


def google_crawler(ua):
    """Google's own crawler or ad fetcher. A normal Chrome phone does not match."""
    text = ua.lower()
    return any(marker in text for marker in _GOOGLE_MARKERS)


def named_crawler(ua):
    """A crawler that names itself. Phone brands such as Cubot are not crawlers."""
    text = ua.lower()
    if any(marker in text for marker in _CRAWLER_MARKERS):
        return True
    return any(match.group(1) not in _BOT_NAMES for match in _BOT_TOKEN.finditer(text))


def strict_crawler(ua, device):
    text = ua.lower()
    if not text.strip() or device == 'unknown':
        return True
    return named_crawler(text)


def accepted_languages(header):
    values = []
    for part in header.lower().split(','):
        pieces = part.strip().split(';')
        quality = 1.0
        try:
            for parameter in pieces[1:]:
                if parameter.strip().startswith('q='):
                    quality = float(parameter.strip()[2:])
        except ValueError:
            continue
        if 0 < quality <= 1:
            values.append(pieces[0].strip())
    return values


def decide(config: Config, visitor: Visitor):
    rules, trace = config.rules, []
    device, android, ios = device_info(visitor.ua)
    address = ipaddress.ip_address(visitor.ip)

    def result(slot, reason, detail, status=None):
        trace.append({'rule': reason, 'status': status or ('pass' if slot == 'B' else 'block'), 'detail': detail})
        return {'slot': slot, 'reason': reason, 'trace': trace, 'device': device, 'device_details': describe_device(visitor.ua), 'country': visitor.country, 'count': visitor.visits}

    def matches(networks):
        return any(address in ipaddress.ip_network(network) for network in networks)

    if config.routing != 'RULES':
        return result(config.routing[-1], 'manual', '手动模式优先于所有规则')
    if not config.protection:
        return result(config.allowed_slot, 'protection_off', f'规则保护关闭，全部放行并展示 {config.allowed_slot}', 'pass')
    if matches(rules.blacklist):
        return result('A', 'blacklist', '命中黑名单；优先于白名单')
    trace.append({'rule': 'blacklist', 'status': 'pass', 'detail': '未命中黑名单'})
    if matches(rules.whitelist):
        return result(config.allowed_slot, 'whitelist', f'命中白名单，跳过其余规则；放行后展示 {config.allowed_slot}', 'pass')
    crawler_hit = rules.super_bots and (google_crawler(visitor.ua) or is_crawler_ip(address))
    checks = [
        ('super_bot', rules.super_bots, crawler_hit, '超级防爬虫：谷歌官方爬虫地址，或谷歌爬虫身份'),
        ('strict_bot', rules.strict_bots, strict_crawler(visitor.ua, device), '严格防爬虫：爬虫、脚本或没有正常浏览器标识的访问'),
        ('bot_marker', rules.block_bots, any(marker in visitor.ua.lower() for marker in rules.bot_markers), 'UA 命中爬虫特征；仅为可伪造的声明'),
        ('ipv4', rules.block_ipv4, address.version == 4, 'IPv4 访问被限制'),
        ('device', rules.block_pc, device != 'mobile', '未识别为移动设备（基于 UA）'),
        ('os_version', bool(rules.min_android or rules.min_ios), (android is not None and android < rules.min_android) or (ios is not None and ios < rules.min_ios) or (rules.min_android > 0 and 'android' in visitor.ua.lower() and android is None) or (rules.min_ios > 0 and any(x in visitor.ua.lower() for x in ('iphone', 'ipad', 'ipod')) and ios is None), '移动系统版本低于阈值'),
        ('blocked_cidr', bool(rules.blocked_cidrs), matches(rules.blocked_cidrs), '命中管理员提供的限制网段'),
        ('country_unknown' if visitor.country is None else 'country', bool(rules.countries), visitor.country not in rules.countries, '国家未知或不在允许列表'),
        ('language', bool(rules.languages), not any(lang == allowed or lang.startswith(allowed + '-') for lang in accepted_languages(visitor.language) for allowed in rules.languages), '浏览器语言不在允许列表'),
        ('visit_limit', rules.max_visits > 0, visitor.visits > rules.max_visits, f'当前窗口第 {visitor.visits} 次，最多放行 {rules.max_visits} 次'),
    ]
    for rule, enabled, blocked, detail in checks:
        if enabled and blocked:
            return result('A', rule, detail)
        trace.append({'rule': rule, 'status': 'pass' if enabled else 'skip', 'detail': '通过' if enabled else '未启用'})
    return result(config.allowed_slot, 'allowed', f'已通过所有启用的规则；放行后展示 {config.allowed_slot}', 'pass')
