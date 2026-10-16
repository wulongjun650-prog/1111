"""Write missing CONFIG.contactApp and the matching link. Existing keys stay."""
import hashlib
import json
from pathlib import Path
import re

from .archives import MAX_TOTAL, read_source, source_files, validate_file, write_bundle

_CODE = ('.html', '.htm', '.js', '.mjs')
_HTML = ('.html', '.htm')
_INDEX = ('index.html', 'index.htm')
_DECL = re.compile(r'(?:const|let|var)\s+CONFIG\s*=\s*\{')
_SCRIPT = re.compile(r'<script\b([^>]*)>(.*?)</script>', re.I | re.S)
_IDENT = re.compile(r'[A-Za-z_$][\w$]*')
_URL_KEYS = {'band': ('bandUrl', 'bandLink'), 'kakao': ('kakaoUrl', 'kakaoLink')}


def scan_contact(root):
    """Report whether the published page already has the two CONFIG fields."""
    blank = _blank_contact()
    try:
        files = source_files(root)
    except (OSError, ValueError):
        return blank
    found = []
    for path, (file, _) in sorted(files.items()):
        if Path(path).suffix.lower() not in _CODE:
            continue
        fields = read_contact_fields(_text(read_source(file, path)), Path(path).suffix.lower() in _HTML)
        if fields is None:
            continue
        found.append((path, fields))
    if not found:
        return blank
    def every(name):
        return all(item.get(name) for _, item in found)
    index = next((item for path, item in found if Path(path).name.lower() in _INDEX), found[0][1])
    readable = all(item.get('readable') for _, item in found)
    return {
        'has_config': True,
        'readable': readable,
        'contact_app': {'present': bool(readable and every('contact_app')), 'value': index.get('contact_value') or ''},
        'band_url': {'present': bool(readable and every('band_url')), 'value': index.get('band_value') or ''},
        'kakao_url': {'present': bool(readable and every('kakao_url')), 'value': index.get('kakao_value') or ''},
        'jump_url': {'present': bool(readable and every('jump_url')), 'value': index.get('jump_value') or ''},
    }


def rewrite_contact_bundle(root, filename, pages, app, url):
    """Copy the B page and insert only the CONFIG keys that are missing."""
    files = source_files(root)
    edited, total, digest = [], 0, hashlib.sha256()
    saw = False
    changed_any = False
    summary_app, summary_url = 'present', 'present'
    index_value, fallback = None, None
    for path, (file, _) in sorted(files.items()):
        data = read_source(file, path)
        suffix = Path(path).suffix.lower()
        if suffix in _CODE:
            rewritten, status = fill_contact_bytes(data, app, url, suffix in _HTML)
            if status and status.get('unreadable'):
                raise ValueError('CONFIG 无法识别，没有改代码')
            if status:
                saw = True
                data = rewritten
                if status['contact_app'] == 'added':
                    summary_app = 'added'
                if status['url'] == 'added':
                    summary_url = 'added'
                changed_any = changed_any or status['changed']
                if Path(path).name.lower() in _INDEX and index_value is None:
                    index_value = status.get('contact_value') or ''
                elif fallback is None:
                    fallback = status.get('contact_value') or ''
        validate_file(path, data)
        total += len(data)
        if total > MAX_TOTAL:
            raise ValueError('源码目录超过总大小限制')
        edited.append((path, data))
        digest.update(path.encode() + b'\0' + str(len(data)).encode() + b'\0' + data)
    if not saw:
        raise ValueError('当前 B 页没有 CONFIG，没有改代码')
    summary = {'contact_app': summary_app, 'url': summary_url, 'contact_value': index_value if index_value is not None else (fallback or '')}
    if not changed_any:
        return None, 0, summary
    return write_bundle(edited, filename, pages, digest.hexdigest()), 1, summary


def fill_contact_bytes(data, app, url, html):
    bom = data.startswith(b'\xef\xbb\xbf')
    raw = data[3:] if bom else data
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        return data, None
    rewritten, status = fill_contact_text(text, app, url, html)
    if status is None or status.get('unreadable') or rewritten == text:
        return data, status
    payload = rewritten.encode('utf-8')
    return ((b'\xef\xbb\xbf' + payload) if bom else payload), status


def fill_contact_text(text, app, url, html):
    found = _locate(text, html)
    if found is None:
        return text, None
    if found.get('unreadable'):
        return text, {'unreadable': True}
    keys = found['keys']
    pieces = []
    contact_state = 'present' if 'contactApp' in keys else 'added'
    url_names = _URL_KEYS[app]
    url_state = 'present' if any(name in keys for name in url_names) else 'added'
    if contact_state == 'added':
        pieces.append('contactApp: ' + _js_string(app))
    if url_state == 'added':
        pieces.append(url_names[0] + ': ' + _js_string(url))
    contact_value = app if contact_state == 'added' else _string_value(keys.get('contactApp'))
    status = {'unreadable': False, 'changed': False, 'contact_app': contact_state, 'url': url_state, 'contact_value': contact_value}
    if not pieces:
        return text, status
    status['changed'] = True
    return _insert(text, found['open'], found['close'], pieces), status


def read_contact_fields(text, html):
    found = _locate(text, html)
    if found is None:
        return None
    if found.get('unreadable'):
        return {'readable': False, 'contact_app': False, 'band_url': False, 'kakao_url': False, 'jump_url': False, 'contact_value': '', 'band_value': '', 'kakao_value': '', 'jump_value': ''}
    keys = found['keys']
    return {
        'readable': True,
        'contact_app': 'contactApp' in keys,
        'band_url': 'bandUrl' in keys or 'bandLink' in keys,
        'kakao_url': 'kakaoUrl' in keys or 'kakaoLink' in keys,
        'jump_url': 'jumpUrl' in keys,
        'contact_value': _string_value(keys.get('contactApp')),
        'band_value': _string_value(keys.get('bandUrl', keys.get('bandLink'))),
        'kakao_value': _string_value(keys.get('kakaoUrl', keys.get('kakaoLink'))),
        'jump_value': _string_value(keys.get('jumpUrl')),
    }


def set_jump_text(text, url, html):
    """Write CONFIG.jumpUrl. An existing value is replaced. Other keys stay.

    An empty url clears the field. Pages without CONFIG are left untouched.
    """
    found = _locate(text, html)
    if found is None:
        return text, None
    if found.get('unreadable'):
        return text, {'unreadable': True}
    keys = found['keys']
    current = _string_value(keys.get('jumpUrl')) if 'jumpUrl' in keys else None
    if url:
        if current == url:
            return text, {'unreadable': False, 'changed': False}
        if 'jumpUrl' in keys:
            rewritten = _replace_named_string(text, found['open'], found['close'] + 1, 'jumpUrl', url)
            if rewritten is None:
                return text, {'unreadable': True}
            return rewritten, {'unreadable': False, 'changed': True}
        return _insert(text, found['open'], found['close'], ['jumpUrl: ' + _js_string(url)]), {'unreadable': False, 'changed': True}
    if current in (None, ''):
        return text, {'unreadable': False, 'changed': False}
    rewritten = _replace_named_string(text, found['open'], found['close'] + 1, 'jumpUrl', '')
    if rewritten is None:
        return text, {'unreadable': True}
    return rewritten, {'unreadable': False, 'changed': True}


def _blank_contact():
    return {
        'has_config': False, 'readable': True,
        'contact_app': {'present': False, 'value': ''},
        'band_url': {'present': False, 'value': ''},
        'kakao_url': {'present': False, 'value': ''},
        'jump_url': {'present': False, 'value': ''},
    }


def _text(data):
    raw = data[3:] if data.startswith(b'\xef\xbb\xbf') else data
    try:
        return raw.decode('utf-8')
    except UnicodeError:
        return ''


def _js_string(value):
    return json.dumps(value, ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def _string_value(value):
    return value if isinstance(value, str) else ''


def _insert(text, open_index, close_index, pieces):
    body = text[open_index + 1:close_index]
    joined = ', '.join(pieces)
    if not body.strip():
        return text[:open_index + 1] + joined + text[close_index:]
    if '\n' in body or '\r' in body:
        newline = '\r\n' if '\r\n' in body else '\n'
        indent = _indent_of(body)
        block = newline + (',' + newline).join(indent + piece for piece in pieces) + ','
        return text[:open_index + 1] + block + body + text[close_index:]
    inserted = joined + ','
    if not body[:1].isspace():
        inserted += ' '
    return text[:open_index + 1] + inserted + body + text[close_index:]


def _replace_named_string(text, open_index, limit, name, new_value):
    """Replace the first plain string value of one top-level key. None if it is not a string."""
    i = open_index + 1
    expect_key = True
    while i < limit:
        i = _skip_space(text, i, limit)
        if i >= limit:
            return None
        if text[i] == '}':
            return None
        if text[i] == ',':
            i += 1
            expect_key = True
            continue
        if not expect_key:
            return None
        key, nxt = _read_key(text, i, limit)
        if key is None:
            nxt = _skip_element(text, i, limit)
            if nxt is None:
                return None
            i = nxt
            expect_key = True
            continue
        nxt = _skip_space(text, nxt, limit)
        if nxt >= limit:
            return None
        if text[nxt] == ':':
            value_at = _skip_space(text, nxt + 1, limit)
            _value, after = _read_value(text, nxt + 1, limit)
            if after is None:
                return None
            if key == name:
                if value_at >= limit or text[value_at] not in '"\'':
                    return None
                end = _skip_string(text, value_at, limit)
                if end < 0 or end != after or (text[value_at] == '`' and '${' in text[value_at:end]):
                    return None
                return text[:value_at] + _js_string(new_value) + text[end:]
            i = _skip_space(text, after, limit)
            expect_key = False
            continue
        if text[nxt] in ',}':
            if key == name:
                return None
            i = nxt
            expect_key = False
            continue
        return None
    return None


def _indent_of(body):
    for line in body.splitlines():
        if not line.strip():
            continue
        return line[:len(line) - len(line.lstrip(' \t'))] or '  '
    return '  '


def _locate(text, html):
    regions = _script_regions(text) if html else [(0, len(text))]
    if html and not regions:
        return None
    for start, end in regions:
        kind, open_index = _find_decl(text, start, end)
        if kind == 'missing':
            continue
        if kind != 'found':
            return {'unreadable': True}
        parsed = _parse_object(text, open_index, end)
        if parsed is None:
            return {'unreadable': True}
        close_index, keys = parsed
        return {'open': open_index, 'close': close_index, 'keys': keys}
    return None


def _script_regions(text):
    regions = []
    for match in _SCRIPT.finditer(text):
        attrs = match.group(1) or ''
        if re.search(r'\bsrc\s*=', attrs, re.I):
            continue
        kind = re.search(r'''\btype\s*=\s*(['"]?)([^'">\s]*)''', attrs, re.I)
        if kind and kind.group(2).lower() not in ('', 'text/javascript', 'application/javascript', 'module'):
            continue
        regions.append((match.start(2), match.end(2)))
    return regions


def _find_decl(text, start, end):
    i = start
    while i < end:
        step = _skip_noise(text, i, end)
        if step is None:
            return 'unclosed', None
        if step != i:
            i = step
            continue
        if text[i] in '"\'`':
            nxt = _skip_string(text, i, end)
            if nxt < 0:
                return 'unclosed', None
            i = nxt
            continue
        if _boundary(text, i) and _DECL.match(text, i) and _DECL.match(text, i).end() <= end:
            return 'found', _DECL.match(text, i).end() - 1
        if _ident_at(text, i):
            i += 1
            while i < end and _ident_at(text, i):
                i += 1
            continue
        i += 1
    return 'missing', None


def _parse_object(text, open_index, limit):
    i = open_index + 1
    expect_key = True
    keys = {}
    while i < limit:
        i = _skip_space(text, i, limit)
        if i >= limit:
            return None
        if text[i] == '}':
            return i, keys
        if text[i] == ',':
            i += 1
            expect_key = True
            continue
        if not expect_key:
            return None
        name, nxt = _read_key(text, i, limit)
        if name is None:
            nxt = _skip_element(text, i, limit)
            if nxt is None:
                return None
            i = nxt
            expect_key = True
            continue
        nxt = _skip_space(text, nxt, limit)
        if nxt >= limit:
            return None
        if text[nxt] == ':':
            value, after = _read_value(text, nxt + 1, limit)
            if after is None:
                return None
            keys.setdefault(name, value)
            i = _skip_space(text, after, limit)
            expect_key = False
            continue
        if text[nxt] in ',}':
            keys.setdefault(name, None)
            i = nxt
            expect_key = False
            continue
        return None
    return None


def _read_key(text, i, limit):
    if i >= limit:
        return None, i
    if text[i] in '"\'':
        end = _skip_string(text, i, limit)
        if end < 0:
            return None, i
        return _decode_js_string(text[i:end]), end
    match = _IDENT.match(text, i)
    if not match or match.end() > limit:
        return None, i
    return match.group(0), match.end()


def _read_value(text, i, limit):
    i = _skip_space(text, i, limit)
    if i >= limit:
        return None, None
    if text[i] in '"\'`':
        end = _skip_string(text, i, limit)
        if end < 0:
            return None, None
        return _decode_js_string(text[i:end]), end
    brace = bracket = paren = 0
    k = i
    while k < limit:
        jumped = _skip_noise(text, k, limit)
        if jumped is None:
            return None, None
        if jumped != k:
            k = jumped
            continue
        if text[k] in '"\'`':
            nxt = _skip_string(text, k, limit)
            if nxt < 0:
                return None, None
            k = nxt
            continue
        ch = text[k]
        if ch == '{':
            brace += 1
        elif ch == '}':
            if brace == bracket == paren == 0:
                return None, k
            brace -= 1
        elif ch == '[':
            bracket += 1
        elif ch == ']':
            if bracket == 0:
                return None, None
            bracket -= 1
        elif ch == '(':
            paren += 1
        elif ch == ')':
            if paren == 0:
                return None, None
            paren -= 1
        elif ch == ',' and brace == bracket == paren == 0:
            return None, k
        k += 1
    return None, None


def _skip_element(text, i, limit):
    brace = bracket = paren = 0
    k = i
    while k < limit:
        jumped = _skip_noise(text, k, limit)
        if jumped is None:
            return None
        if jumped != k:
            k = jumped
            continue
        if text[k] in '"\'`':
            nxt = _skip_string(text, k, limit)
            if nxt < 0:
                return None
            k = nxt
            continue
        ch = text[k]
        if ch == ',' and brace == bracket == paren == 0:
            return k
        if ch == '}' and brace == bracket == paren == 0:
            return k
        if ch == '{':
            brace += 1
        elif ch == '}':
            if brace == 0:
                return None
            brace -= 1
        elif ch == '[':
            bracket += 1
        elif ch == ']':
            if bracket == 0:
                return None
            bracket -= 1
        elif ch == '(':
            paren += 1
        elif ch == ')':
            if paren == 0:
                return None
            paren -= 1
        k += 1
    return None


def _skip_space(text, i, limit):
    while i < limit:
        jumped = _skip_noise(text, i, limit)
        if jumped is None or jumped == i:
            break
        i = jumped
    return i


def _skip_noise(text, i, limit):
    if i >= limit:
        return i
    if text[i] in ' \t\r\n\f':
        while i < limit and text[i] in ' \t\r\n\f':
            i += 1
        return i
    if text.startswith('//', i) or text.startswith('<!--', i):
        newline = text.find('\n', i)
        return limit if newline < 0 or newline >= limit else newline + 1
    if text.startswith('/*', i):
        close = text.find('*/', i + 2)
        if close < 0 or close >= limit:
            return None
        return close + 2
    return i


def _skip_string(text, i, limit):
    quote = text[i]
    k = i + 1
    while k < limit:
        if text[k] == '\\':
            k += 2
            continue
        if quote == '`' and text.startswith('${', k):
            k += 2
            depth = 1
            while k < limit and depth:
                if text[k] == '\\':
                    k += 2
                    continue
                if text[k] in '"\'`':
                    nested = _skip_string(text, k, limit)
                    if nested < 0:
                        return -1
                    k = nested
                    continue
                if text[k] == '{':
                    depth += 1
                elif text[k] == '}':
                    depth -= 1
                k += 1
            continue
        if text[k] == quote:
            return k + 1
        k += 1
    return -1


def _decode_js_string(literal):
    if len(literal) < 2 or literal[0] not in '"\'`' or literal[-1] != literal[0]:
        return None
    if literal[0] == '`' and '${' in literal:
        return None
    if literal[0] == '"':
        try:
            return json.loads(literal)
        except json.JSONDecodeError:
            return None
    body, out, i = literal[1:-1], [], 0
    mapping = {'n': '\n', 'r': '\r', 't': '\t', '\\': '\\', "'": "'", '"': '"', '`': '`', '/': '/'}
    while i < len(body):
        if body[i] != '\\':
            out.append(body[i])
            i += 1
            continue
        if i + 1 >= len(body) or body[i + 1] not in mapping:
            return None
        out.append(mapping[body[i + 1]])
        i += 2
    return ''.join(out)


def _boundary(text, i):
    return i == 0 or not _ident_at(text, i - 1)


def _ident_at(text, i):
    char = text[i]
    return char == '$' or char == '_' or char.isascii() and char.isalnum()
