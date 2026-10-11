"""Display-only UA parsing. Claims, not verified hardware identification."""
import re


def _major(value):
    head = str(value).split('.')[0]
    return int(head) if head.isdigit() else None


def ios_major(ua):
    """Major iOS version used by the minimum-version rule.

    A parsed OS token is used as-is. Safari's Version major replaces it only
    when that major is higher, which is how a frozen compatibility token is
    recognized. An iPhone, iPad, or iPod with no readable version counts as 18.
    Other clients return None.
    """
    text = ua.lower()
    if not any(name in text for name in ('iphone', 'ipad', 'ipod')):
        return None
    match = re.search(r'(?:iphone os|cpu os)\s+(\d+)', text)
    major = int(match.group(1)) if match else None
    safari = re.search(r'version/(\d+)', text)
    safari_major = int(safari.group(1)) if safari else None
    if major is None:
        return safari_major if safari_major and safari_major >= 18 else 18
    if safari_major and safari_major > major:
        return safari_major
    return major


def ios_label(ua):
    """Text shown for an Apple phone or tablet.

    A consistent OS token is shown exactly, including versions above 18.
    When Safari reports a newer major than the OS token, or no OS token can
    be read, the label says that version or above instead of an unknown system.
    """
    os_match = re.search(r'(?:CPU (?:iPhone )?OS|iPhone OS)\s+([\d_]+)', ua, re.I)
    safari_match = re.search(r'Version/([\d.]+)', ua, re.I)
    os_value = os_match.group(1).replace('_', '.') if os_match else ''
    safari_value = safari_match.group(1) if safari_match else ''
    os_major = _major(os_value) if os_value else None
    safari_major = _major(safari_value) if safari_value else None
    if os_value and safari_major and os_major is not None and safari_major > os_major:
        return f'iOS {safari_major} 以上'
    if os_value:
        return f'iOS {os_value}'
    if safari_major and safari_major > 18:
        return f'iOS {safari_major} 以上'
    return 'iOS 18 以上'


_IN_APP = (
    (re.compile(r'MicroMessenger|Weixin|\bWeChat/', re.I), '微信'),
    (re.compile(r'HuaweiBrowser/', re.I), '华为浏览器'),
    (re.compile(r'MiuiBrowser/', re.I), '小米浏览器'),
    (re.compile(r'HeyTapBrowser/', re.I), 'OPPO浏览器'),
    (re.compile(r'VivoBrowser/', re.I), 'vivo浏览器'),
    (re.compile(r'UCBrowser/|UCWEB', re.I), 'UC浏览器'),
    (re.compile(r'MQQBrowser/', re.I), 'QQ浏览器'),
    (re.compile(r'(?<![A-Za-z])QQ/\d', re.I), 'QQ'),
    (re.compile(r'Instagram', re.I), 'Instagram'),
    (re.compile(r'FB_IAB|FBAN|FBAV/', re.I), 'Facebook'),
    (re.compile(r'\bAweme/', re.I), '抖音'),
    (re.compile(r'BytedanceWebview|musical_ly|TikTok', re.I), 'TikTok'),
    (re.compile(r'\bLine/\d', re.I), 'LINE'),
    (re.compile(r'KAKAOTALK', re.I), 'KakaoTalk'),
    (re.compile(r'\bWhatsApp/', re.I), 'WhatsApp'),
    (re.compile(r'\bGSA/', re.I), 'Google'),
)


def in_app_name(ua):
    """The app that opened the page, when the UA names one. Ordinary browsers return None."""
    text = ua or ''
    for pattern, name in _IN_APP:
        if pattern.search(text):
            return name
    return None


def open_app(ua):
    """Chinese name of the app or browser that opened the page."""
    named = in_app_name(ua)
    if named:
        return named
    text = ua or ''
    if re.search(r'EdgA/|EdgiOS/|Edg/', text, re.I):
        return 'Edge'
    if re.search(r'OPR/|Opera/', text, re.I):
        return 'Opera'
    if re.search(r'Firefox/|FxiOS/', text, re.I):
        return 'Firefox'
    if re.search(r'SamsungBrowser/', text, re.I):
        return '三星浏览器'
    if re.search(r'CriOS/|Chrome/', text, re.I):
        return 'Chrome'
    if re.search(r'Version/[\d.]+', text) and 'Safari/' in text:
        return 'Safari'
    if re.search(r';\s*wv\)', text, re.I):
        return '应用内打开'
    if text.strip():
        return '浏览器'
    return '未知'


def describe_device(ua):
    ua = ''.join(c for c in ua[:1024] if c.isprintable())
    device, brand, model, system, browser = '', '', '', '', ''

    def version(pattern):
        match = re.search(pattern, ua, re.I)
        return match.group(1).replace('_', '.') if match else ''

    if any(name.lower() in ua.lower() for name in ('iPhone', 'iPad', 'iPod')):
        device = next(name for name in ('iPhone', 'iPad', 'iPod') if name.lower() in ua.lower())
        brand = 'Apple'
        system = ios_label(ua)
    elif re.search('Android', ua, re.I):
        device = 'Android 移动设备'
        system = 'Android' + (' ' + value if (value := version(r'Android\s+([\d.]+)')) else '')
        match = re.search(r'Android[^;)]*;([^)]*)', ua, re.I)
        if match:
            candidates = [part.strip() for part in match[1].split(';')]
            for candidate in reversed(candidates):
                candidate = re.split(r'\s+Build/', candidate, flags=re.I)[0].strip()
                if candidate.lower() in ('k', 'wv', 'u', 'linux') or re.fullmatch(r'[a-z]{2}(?:[-_][a-z]{2})?', candidate, re.I):
                    continue
                if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._+-]{1,79}', candidate):
                    model = candidate
                    break
        for pattern, name in [(r'^(SM-|GT-|SCH-|SAMSUNG)', 'Samsung'), (r'^Pixel\b', 'Google'), (r'^HUAWEI\b', 'Huawei'), (r'^HONOR\b', 'Honor'), (r'^(Redmi|Mi |Xiaomi|POCO)', 'Xiaomi'), (r'^ONEPLUS\b', 'OnePlus'), (r'^OPPO\b', 'OPPO'), (r'^vivo\b', 'vivo')]:
            if re.search(pattern, model, re.I):
                brand = name
                break
        if model:
            device = f'{brand} {model}' if brand and not model.lower().startswith(brand.lower()) else model
    elif 'Windows NT' in ua:
        device = 'Windows 电脑'
        nt = version(r'Windows NT\s+([\d.]+)')
        system = {'10.0': 'Windows 10 / 11', '6.3': 'Windows 8.1', '6.2': 'Windows 8', '6.1': 'Windows 7'}.get(nt, 'Windows')
    elif 'CrOS' in ua:
        device, system = 'Chromebook', 'ChromeOS'
    elif 'Macintosh' in ua:
        device, brand = 'Mac（UA 声明）', 'Apple'
        system = 'macOS' + (' ' + value if (value := version(r'Mac OS X\s+([\d_.]+)')) else '')
    elif 'Linux' in ua or 'X11' in ua:
        device, system = 'Linux 设备', 'Linux'
    named = in_app_name(ua)
    if named:
        browser = named
    else:
        for pattern, name in [(r'(?:EdgA|EdgiOS|Edg|Edge)/([\d.]+)', 'Edge'), (r'(?:OPR|Opera)/([\d.]+)', 'Opera'), (r'SamsungBrowser/([\d.]+)', 'Samsung Internet'), (r'(?:Firefox|FxiOS)/([\d.]+)', 'Firefox'), (r'(?:Chrome|CriOS)/([\d.]+)', 'Chrome')]:
            if value := version(pattern):
                browser = name + ' ' + value
                break
        else:
            if 'Safari/' in ua and (value := version(r'Version/([\d.]+)')):
                browser = 'Safari ' + value
    if not brand and re.search(r'Android', ua, re.I):
        for pattern, name in (
            (r'SamsungBrowser/', 'Samsung'),
            (r'MiuiBrowser/', 'Xiaomi'),
            (r'HuaweiBrowser/', 'Huawei'),
            (r'HeyTapBrowser/', 'OPPO'),
            (r'VivoBrowser/', 'vivo'),
        ):
            if re.search(pattern, ua, re.I):
                brand = name
                if device == 'Android 移动设备':
                    device = f'{name} 手机'
                break
    if not device:
        device = '手机'
    if not system:
        system = 'iOS 18 以上'
    if not browser:
        browser = 'Safari' if system.startswith(('iOS', 'macOS')) else '内置浏览器'
    return {'device': device, 'brand': brand, 'model': model, 'os': system, 'browser': browser, 'source': 'user-agent'}
