"""Display-only UA parsing. Claims, not verified hardware identification."""
import re


def describe_device(ua):
    ua = ''.join(c for c in ua[:1024] if c.isprintable())
    device, brand, model, system, browser = '未知设备', '', '', '系统未知', '浏览器未知'

    def version(pattern):
        match = re.search(pattern, ua, re.I)
        return match.group(1).replace('_', '.') if match else ''

    if any(name.lower() in ua.lower() for name in ('iPhone', 'iPad', 'iPod')):
        device = next(name for name in ('iPhone', 'iPad', 'iPod') if name.lower() in ua.lower())
        brand = 'Apple'
        system = 'iOS' + (' ' + value if (value := version(r'(?:CPU (?:iPhone )?OS|iPhone OS)\s+([\d_]+)')) else '')
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
    for pattern, name in [(r'(?:EdgA|EdgiOS|Edg|Edge)/([\d.]+)', 'Edge'), (r'(?:OPR|Opera)/([\d.]+)', 'Opera'), (r'SamsungBrowser/([\d.]+)', 'Samsung Internet'), (r'(?:Firefox|FxiOS)/([\d.]+)', 'Firefox'), (r'(?:Chrome|CriOS)/([\d.]+)', 'Chrome')]:
        if value := version(pattern):
            browser = name + ' ' + value
            break
    else:
        if 'Safari/' in ua and (value := version(r'Version/([\d.]+)')):
            browser = 'Safari ' + value
    return {'device': device, 'brand': brand, 'model': model, 'os': system, 'browser': browser, 'source': 'user-agent'}
