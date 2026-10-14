"""Change only the HTML title that GA4 reports as the page title."""
import html
import re
from pathlib import Path


MAX_TITLE = 120
_REGION = re.compile(r'(?is)<(script|style|textarea|xmp|noscript)\b[^>]*>.*?</\1>')
_TITLE = re.compile(r'(?is)(<title\b[^>]*>)(.*?)(</title>)')
_HEAD = re.compile(r'(?i)<head\b[^>]*>')
_HTML = re.compile(r'(?i)<html\b[^>]*>')


def normalize_title(value):
    if not isinstance(value, str):
        raise ValueError('请填写网站标题')
    text = re.sub(r'\s+', ' ', value).strip()
    if not text or len(text) > MAX_TITLE:
        raise ValueError('网站标题需要 1 到 120 个字')
    if any(ord(char) < 32 or char in '<>' for char in text):
        raise ValueError('网站标题不能包含换行或尖括号')
    return text


def choose_html(names):
    folded = {name.casefold(): name for name in names}
    for candidate in ('index.html', 'index.htm'):
        found = folded.get(candidate)
        if found:
            return found
    html_names = [name for name in names if Path(name).suffix.lower() in {'.html', '.htm'}]
    return sorted(html_names)[0] if html_names else None


def read_document_title(text):
    match = _title_span(text)
    if not match:
        return ''
    return html.unescape(re.sub(r'\s+', ' ', match.group(2)).strip())


def write_title(text, title):
    """Replace the first real title, or add one. Everything else stays put."""
    title = normalize_title(title)
    match = _title_span(text)
    if match:
        current = html.unescape(re.sub(r'\s+', ' ', match.group(2)).strip())
        if current == title:
            return text, False
        escaped = html.escape(title, quote=False)
        return text[:match.start(2)] + escaped + text[match.end(2):], True
    escaped = html.escape(title, quote=False)
    snippet = f'<title>{escaped}</title>'
    masked = _masked(text)
    head = _HEAD.search(masked)
    if head:
        return text[:head.end()] + snippet + text[head.end():], True
    opened = _HTML.search(masked)
    if opened:
        return text[:opened.end()] + '<head>' + snippet + '</head>' + text[opened.end():], True
    return snippet + text, True


def rewrite_title(pairs, title):
    chosen = choose_html(name for name, _payload in pairs)
    if not chosen:
        raise ValueError('这个页面没有 HTML，改不了网站标题')
    edited = []
    changed = False
    for name, payload in pairs:
        if name != chosen:
            edited.append((name, payload))
            continue
        bom = payload.startswith(b'\xef\xbb\xbf')
        try:
            text = payload.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ValueError('页面不是 UTF-8，改不了网站标题') from exc
        updated, did = write_title(text, title)
        changed = did
        data = updated.encode('utf-8')
        if bom:
            data = b'\xef\xbb\xbf' + data
        edited.append((name, data))
    return edited, changed, chosen


def _masked(text):
    return _REGION.sub(lambda match: ' ' * len(match.group(0)), text)


def _title_span(text):
    return _TITLE.search(_masked(text))
