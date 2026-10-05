"""Save Google snippets and write them into a published page."""
import re
from pathlib import Path


GA4_SRC = re.compile(r'googletagmanager\.com/gtag/js\?id=(G-[A-Z0-9]{4,24})', re.I)
GA4_CONFIG = re.compile(r'''gtag\(\s*['"]config['"]\s*,\s*['"](G-[A-Z0-9]{4,24})['"]''', re.I)
GA4_LOADER = re.compile(
    r'<script\b[^>]*\bsrc\s*=\s*[\'"]https://www\.googletagmanager\.com/gtag/js\?id=G-[A-Z0-9]{4,24}[\'"][^>]*>\s*</script>',
    re.I,
)
SEND_TO = re.compile(r'''['"]send_to['"]\s*:\s*['"](AW-\d{5,15}/[A-Za-z0-9_-]{4,80})['"]''')
TEXT_SUFFIXES = {'.html', '.htm', '.js', '.mjs'}


def normalize_ga4(body):
    text = _snippet(body)
    sources = [item.upper() for item in GA4_SRC.findall(text)]
    configs = [item.upper() for item in GA4_CONFIG.findall(text)]
    if not sources or not configs:
        raise ValueError('GA4 代码需要包含 gtag.js 和 gtag config')
    if len(set(sources + configs)) != 1:
        raise ValueError('GA4 编号不一致，请只保留一个 G- 编号')
    return sources[0], text


def normalize_conversion(body):
    text = _snippet(body)
    if not re.search(r'function\s+gtag_report_conversion\s*\(', text):
        raise ValueError('转化代码需要包含 gtag_report_conversion')
    targets = SEND_TO.findall(text)
    if len(set(targets)) != 1:
        raise ValueError('转化代码需要一个 send_to 目标')
    return targets[0], text


def read_pages(pairs):
    ga4 = conversion = ''
    for name, payload in _ordered(pairs):
        text = _text(name, payload)
        if text is None:
            continue
        if not ga4:
            found = GA4_CONFIG.findall(text) or GA4_SRC.findall(text)
            if found:
                ga4 = found[0].upper()
        if not conversion:
            found = SEND_TO.findall(text)
            if found:
                conversion = found[0]
        if ga4 and conversion:
            break
    return {'ga4': ga4, 'conversion': conversion}


def rewrite_pages(pairs, ga4_body=None, conversion_body=None):
    ga4_id = normalize_ga4(ga4_body)[0] if ga4_body else ''
    send_to = normalize_conversion(conversion_body)[0] if conversion_body else ''
    decoded = []
    had_ga4 = had_conversion = False
    for name, payload in pairs:
        text = _text(name, payload)
        if text is None:
            decoded.append((name, payload, None, False))
            continue
        bom = payload.startswith(b'\xef\xbb\xbf')
        had_ga4 = had_ga4 or bool(GA4_SRC.search(text) or GA4_CONFIG.search(text))
        had_conversion = had_conversion or bool(re.search(r'function\s+gtag_report_conversion\s*\(', text) or SEND_TO.search(text))
        decoded.append((name, payload, text, bom))
    edited = []
    changed = False
    for name, payload, text, bom in decoded:
        if text is None:
            edited.append((name, payload))
            continue
        updated = text
        if ga4_body:
            updated, did = _replace_ga4(updated, ga4_body.strip())
            changed = changed or did
        if conversion_body:
            updated, did = _replace_conversion(updated, conversion_body.strip())
            changed = changed or did
        edited.append((name, updated, bom, payload))
    if ga4_body and not had_ga4:
        edited, did = _insert(edited, 'index.html', ga4_body.strip())
        changed = changed or did
    if conversion_body and not had_conversion:
        edited, did = _insert(edited, 'index.html', conversion_body.strip())
        changed = changed or did
    result = []
    for item in edited:
        if len(item) == 2:
            result.append(item)
            continue
        name, updated, bom, payload = item
        if ga4_body and had_ga4:
            updated = _swap_ga4(updated, ga4_id)
        if conversion_body and had_conversion:
            updated = _swap_send_to(updated, send_to)
        data = updated.encode('utf-8')
        if bom:
            data = b'\xef\xbb\xbf' + data
        if data != payload:
            changed = True
        result.append((name, data))
    return result, changed


def _snippet(body):
    if not isinstance(body, str):
        raise ValueError('请粘贴完整代码')
    text = body.strip()
    if not text or len(text) > 12000 or '\x00' in text:
        raise ValueError('代码为空、过长或含无效字符')
    return text


def _text(name, payload):
    if Path(name).suffix.lower() not in TEXT_SUFFIXES:
        return None
    try:
        return payload.decode('utf-8-sig')
    except UnicodeDecodeError:
        return None


def _ordered(pairs):
    return sorted(pairs, key=lambda item: (item[0] != 'index.html', item[0]))


def _replace_ga4(text, snippet):
    spans = []
    for match in GA4_LOADER.finditer(text):
        start, end = match.start(), match.end()
        comment = _comment_before(text, start, 'google tag')
        if comment is not None:
            start = comment
        rest = text[end:]
        gap = re.match(r'(?:\s|<!--[\s\S]*?-->)*', rest).end()
        following = re.match(r'(<script\b[^>]*>)([\s\S]*?)</script>', rest[gap:], re.I)
        if following and re.search(r'''gtag\(\s*['"]config['"]''', following.group(2), re.I) and 'gtag_report_conversion' not in following.group(2):
            end += gap + following.end()
        spans.append((start, _one_newline(text, end)))
    return _splice(text, spans, snippet)


def _replace_conversion(text, snippet):
    spans = []
    for match in re.finditer(r'function\s+gtag_report_conversion\s*\(', text):
        end = _closing_brace(text, match.end())
        if end is None:
            continue
        start = match.start()
        script_open = text.rfind('<script', 0, start)
        script_close = text.find('</script>', end)
        if script_open != -1 and script_close != -1:
            between = text[script_open:start]
            if between.count('<script') == 1 and '</script>' not in between:
                start = script_open
                end = script_close + len('</script>')
        for needle in ('conversion', 'gtag_report_conversion'):
            comment = _comment_before(text, start, needle)
            if comment is not None:
                start = comment
                break
        spans.append((start, _one_newline(text, end)))
    return _splice(text, spans, snippet)


def _one_newline(text, end):
    if end < len(text) and text[end] == '\r':
        end += 1
    if end < len(text) and text[end] == '\n':
        end += 1
    return end


def _closing_brace(text, after_name):
    brace = text.find('{', after_name)
    if brace < 0:
        return None
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == '{':
            depth += 1
        elif text[index] == '}':
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _comment_before(text, start, needle):
    prefix = text[:start].rstrip()
    if not prefix.endswith('-->'):
        return None
    opened = prefix.rfind('<!--')
    if opened < 0 or needle not in prefix[opened:].lower():
        return None
    return opened


def _splice(text, spans, snippet):
    if not spans:
        return text, False
    spans = sorted(spans)
    pieces, cursor = [], 0
    for index, (start, end) in enumerate(spans):
        if start < cursor:
            continue
        pieces.append(text[cursor:start])
        if index == 0:
            pieces.append(snippet.strip() + '\n')
        cursor = end
    pieces.append(text[cursor:])
    result = ''.join(pieces)
    return result, result != text


def _insert(items, filename, snippet):
    updated = []
    done = False
    for item in items:
        if len(item) == 2 or item[0] != filename:
            updated.append(item)
            continue
        name, text, bom, payload = item
        text = _insert_text(text, snippet)
        updated.append((name, text, bom, payload))
        done = True
    if not done:
        raise ValueError('发布页面没有 index.html，无法加入代码')
    return updated, True


def _insert_text(text, snippet):
    block = snippet.strip() + '\n'
    head = text.lower().rfind('</head>')
    if head != -1:
        return text[:head] + block + text[head:]
    body = re.search(r'<body\b[^>]*>', text, re.I)
    if body:
        return text[:body.end()] + '\n' + block + text[body.end():]
    return block + text


def _swap_ga4(text, new_id):
    def source(match):
        return match.group(0) if match.group(1).upper() == new_id else match.group(0).replace(match.group(1), new_id, 1)

    def config(match):
        return match.group(0) if match.group(1).upper() == new_id else match.group(0).replace(match.group(1), new_id, 1)

    text = GA4_SRC.sub(source, text)
    return GA4_CONFIG.sub(config, text)


def _swap_send_to(text, send_to):
    def replace(match):
        return match.group(0) if match.group(1) == send_to else match.group(0).replace(match.group(1), send_to, 1)

    return SEND_TO.sub(replace, text)
