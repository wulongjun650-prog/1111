"""Add the click and wake beacon when a B page is missing it. Google tags stay untouched."""
import hashlib
from pathlib import Path

from .archives import MAX_TOTAL, read_source, source_files, validate_file, write_bundle
from .contact_config import _boundary, _insert, _locate, _script_regions, _skip_noise, _skip_string

_CODE = ('.html', '.htm', '.js', '.mjs')
_HTML = ('.html', '.htm')
_EVENTS = (
    'whatsapp_click',
    'wa_app_opened',
    'wa_fallback_shown',
    'wa_fallback_click',
    'wa_copy_number',
    'wa_copy_message',
)
_MARKER = '/* ab-event-implant */'
_SEND_EVENT = '''
    /* ab-event-implant */
    function sendEvent(name, extra) {
      if (typeof CONFIG !== "object" || !CONFIG || !CONFIG.eventEndpoint) return;
      try {
        var attempt = (typeof waAttempt === "object" && waAttempt) || null;
        var ids = (typeof CLICK_IDS === "object" && CLICK_IDS) || {};
        var started = (typeof PAGE_START === "number") ? PAGE_START : 0;
        var body = JSON.stringify(Object.assign({
          event: name,
          ts: Date.now(),
          since_load_ms: started ? Date.now() - started : 0,
          wa_env: (typeof WA_ENV === "string") ? WA_ENV : "",
          link_type: (attempt && attempt.linkType) || "",
          cta: (attempt && attempt.placement) || "",
          transaction_id: (attempt && attempt.txId) || "",
          placement_app: ids.placement || "",
          creative: ids.creative || "",
          device: ids.device || "",
          gclid: ids.gclid || "",
          gbraid: ids.gbraid || "",
          wbraid: ids.wbraid || "",
          number: CONFIG.whatsappNumber || "",
          ua: (typeof UA === "string" ? UA : (navigator.userAgent || "")).slice(0, 500)
        }, extra || {}));
        if (navigator.sendBeacon && navigator.sendBeacon(CONFIG.eventEndpoint, new Blob([body], {type: "text/plain"}))) return;
        fetch(CONFIG.eventEndpoint, {method: "POST", body: body, mode: "no-cors", keepalive: true});
      } catch (abSkip) {}
    }
'''
_WRAP = '''
    /* ab-event-implant */
    var abGoWhatsApp = goWhatsApp;
    goWhatsApp = function () {
      var abResult;
      try { abResult = abGoWhatsApp.apply(this, arguments); }
      finally { try { sendEvent("whatsapp_click", {}); } catch (abSkip) {} }
      return abResult;
    };
'''


def scan_implant(root):
    """Status of the B page that is about to be published or already is."""
    try:
        files = source_files(root)
    except (OSError, ValueError):
        return _status('absent')
    found = None
    for path, (file, _) in sorted(files.items()):
        if Path(path).suffix.lower() not in _CODE:
            continue
        text = _text(read_source(file, path))
        if not text:
            continue
        report = _scan_text(text, Path(path).suffix.lower() in _HTML)
        if report['state'] == 'absent' and found is None:
            found = report
            continue
        if report['state'] != 'absent':
            return report
    return found or _status('absent')


def implant_tree(root):
    """Rewrite this version in place. Returns the status after the write."""
    files = source_files(root)
    for path, (file, _) in sorted(files.items()):
        suffix = Path(path).suffix.lower()
        if suffix not in _CODE:
            continue
        data = read_source(file, path)
        updated, _ = implant_bytes(data, suffix in _HTML)
        if updated != data:
            validate_file(path, updated)
            file.write_bytes(updated)
    return scan_implant(root)


def implant_bytes(data, html):
    bom = data.startswith(b'\xef\xbb\xbf')
    raw = data[3:] if bom else data
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        return data, _status('absent')
    updated, status = implant_text(text, html)
    if updated == text:
        return data, status
    payload = updated.encode('utf-8')
    return ((b'\xef\xbb\xbf' + payload) if bom else payload), status


def implant_text(text, html):
    current = _scan_text(text, html)
    if current['state'] in ('ready', 'implanted', 'absent', 'unreadable'):
        return text, current
    updated = text
    located = _locate(updated, html)
    if located and not located.get('unreadable') and 'eventEndpoint' not in located['keys']:
        updated = _insert(updated, located['open'], located['close'], ["eventEndpoint: ''"])
    updated = _insert_track_calls(updated, html)
    if _needs_attempt(updated, html):
        updated = _patch_attempt(updated, html)
    should_wrap = _needs_wrap(updated, html)
    if not _has_function(updated, html, 'sendEvent'):
        updated = _insert_before_function(updated, html, 'goWhatsApp', _SEND_EVENT) or _insert_before_function(updated, html, 'track', _SEND_EVENT) or _append_script(updated, html, _SEND_EVENT)
    if should_wrap:
        updated = _append_script(updated, html, _WRAP)
    status = _scan_text(updated, html)
    if updated == text or status['state'] not in ('ready', 'implanted'):
        return text, _scan_text(text, html)
    return updated, status


def republish_implant(store):
    """Publish a new B only when the live page is missing the beacon."""
    version_id = (store.slots() or {}).get('B')
    if not version_id:
        return _status('absent') | {'published': False}
    root = store.pages / version_id
    before = scan_implant(root)
    if before['state'] != 'missing':
        return before | {'published': False}
    files = source_files(root)
    edited, total, digest, changed = [], 0, hashlib.sha256(), False
    for path, (file, _) in sorted(files.items()):
        data = read_source(file, path)
        suffix = Path(path).suffix.lower()
        if suffix in _CODE:
            data, status = implant_bytes(data, suffix in _HTML)
            changed = changed or status.get('state') == 'implanted'
        validate_file(path, data)
        total += len(data)
        if total > MAX_TOTAL:
            raise ValueError('源码目录超过总大小限制')
        edited.append((path, data))
        digest.update(path.encode() + b'\0' + str(len(data)).encode() + b'\0' + data)
    if not changed:
        return before | {'published': False}
    current = store.version(version_id) or {'name': 'index.html'}
    version = write_bundle(edited, current['name'], store.pages, digest.hexdigest())
    store.add_version('B', version)
    store.publish('B', version['id'])
    report = scan_implant(store.pages / version['id'])
    report['published'] = True
    report['version_id'] = version['id']
    return report


def _status(state):
    labels = {
        'ready': '已有植入代码',
        'implanted': '植入成功',
        'missing': '未植入',
        'absent': '这一页没有跳转代码，没有改',
        'unreadable': 'CONFIG 无法识别，没有改代码',
    }
    return {'state': state, 'label': labels[state]}


def _scan_text(text, html):
    located = _locate(text, html)
    if located and located.get('unreadable'):
        return _status('unreadable')
    if not _is_jump(text, html):
        return _status('absent')
    has_endpoint = bool(located and 'eventEndpoint' in located['keys'])
    has_function = _has_function(text, html, 'sendEvent')
    calls_ready = _calls_paired(text, html)
    attempt_ready = not _needs_attempt(text, html)
    if has_endpoint and has_function and calls_ready and attempt_ready and not _needs_wrap(text, html):
        if _MARKER in text:
            return _status('implanted')
        return _status('ready')
    return _status('missing')


def _text(data):
    raw = data[3:] if data.startswith(b'\xef\xbb\xbf') else data
    try:
        return raw.decode('utf-8')
    except UnicodeError:
        return ''


def _regions(text, html):
    if not html:
        return [(0, len(text))]
    return _script_regions(text)


def _walk(text, html):
    """Yield (index, kind) for code positions. kind is 'code' or the walker stops at strings."""
    for start, end in _regions(text, html):
        i = start
        while i < end:
            step = _skip_noise(text, i, end)
            if step is None:
                return
            if step != i:
                i = step
                continue
            if text[i] in '"\'`':
                nxt = _skip_string(text, i, end)
                if nxt < 0:
                    return
                i = nxt
                continue
            yield i, end
            i += 1


def _has_function(text, html, name):
    needle = 'function'
    for index, end in _walk(text, html):
        if text.startswith(needle, index) and _boundary(text, index) and _after_name(text[index + len(needle):end], name):
            return True
    return False


def _after_name(rest, name):
    body = rest.lstrip()
    if not body.startswith(name):
        return False
    nxt = len(name)
    return nxt >= len(body) or not (body[nxt].isalnum() or body[nxt] in '_$')


def _is_jump(text, html):
    if _has_function(text, html, 'goWhatsApp') or _has_function(text, html, 'buildWhatsAppUrls'):
        return True
    for name in _EVENTS:
        if _track_calls(text, html, name):
            return True
    for index, _end in _walk(text, html):
        if text.startswith(('wa.me', 'whatsapp://', 'api.whatsapp.com'), index):
            return True
    return False


def _track_calls(text, html, name):
    found = []
    for index, end in _walk(text, html):
        if not (text.startswith('track(', index) and _boundary(text, index)):
            continue
        call_end = _call_end(text, index + 5, end)
        if call_end is None:
            continue
        argument = _first_string(text, index + 6, call_end)
        if argument == name:
            found.append(call_end)
    return found


def _calls_paired(text, html):
    for name in _EVENTS:
        for call_end in _track_calls(text, html, name):
            if not _followed_by_send(text, html, call_end, name):
                return False
    return True


def _followed_by_send(text, html, call_end, name):
    """sendEvent(name) anywhere in the same function counts, even with a line between it and track()."""
    span = _enclosing_span(text, html, call_end)
    start, end = span if span is not None else (call_end, min(len(text), call_end + 1600))
    return _contains_send(text, start, end, name)


def _starts_send(text, index, name):
    if not text.startswith('sendEvent(', index):
        return False
    argument = _first_string(text, index + len('sendEvent('), len(text))
    return argument == name


def _needs_wrap(text, html):
    # A page that already defines sendEvent calls it from its own jump. Do not wrap that.
    if _has_function(text, html, 'sendEvent') or not _has_function(text, html, 'goWhatsApp'):
        return False
    if _track_calls(text, html, 'whatsapp_click'):
        return False
    return 'sendEvent("whatsapp_click"' not in text and "sendEvent('whatsapp_click'" not in text


def _needs_attempt(text, html):
    for start, end in _function_spans(text, html, 'goWhatsApp'):
        body = text[start:end]
        marker = body.find('waAttempt')
        if marker < 0:
            continue
        brace = body.find('{', marker)
        if brace < 0:
            continue
        close = _brace_end(body, brace)
        if close is None:
            continue
        if 'txId' not in body[brace:close + 1]:
            return True
    return False


def _insert_track_calls(text, html):
    points = []
    for name in _EVENTS:
        for call_end in _track_calls(text, html, name):
            if not _followed_by_send(text, html, call_end, name):
                points.append((call_end, name))
    for call_end, name in sorted(points, reverse=True):
        line = text.rfind('\n', 0, call_end) + 1
        indent = text[line:call_end]
        indent = indent if indent.strip() == '' else '      '
        text = text[:call_end] + f"\n{indent}sendEvent('{name}', {{}});" + text[call_end:]
    return text


def _patch_attempt(text, html):
    for start, end in _function_spans(text, html, 'goWhatsApp'):
        body = text[start:end]
        marker = body.find('waAttempt')
        brace = body.find('{', marker)
        close = _brace_end(body, brace)
        if close is None:
            continue
        absolute = start + close
        line = text.rfind('\n', 0, start + marker) + 1
        indent = text[line:start + marker]
        indent = indent if indent.strip() == '' else '      '
        prelude = (
            f"{indent}var abLink = 'universal';\n"
            f"{indent}var abTx = Date.now().toString(36);\n"
            f"{indent}try {{ abTx += '-' + makeLeadCode(); }} catch (abSkip) {{}}\n"
            f"{indent}try {{\n"
            f"{indent}  if (WA_ENV === 'ios_webview') abLink = 'scheme';\n"
            f"{indent}  else if (WA_ENV === 'android_browser' && ANDROID_INTENT_OK) abLink = 'intent';\n"
            f"{indent}  if (WA_ENV === 'android_webview') abLink = 'iframe';\n"
            f"{indent}}} catch (abSkip) {{}}\n"
        )
        text = text[:line] + prelude + text[line:absolute] + ', linkType: abLink, txId: abTx' + text[absolute:]
        return text
    return text


def _insert_before_function(text, html, name, block):
    for index, end in _walk(text, html):
        if text.startswith('function', index) and _boundary(text, index) and _after_name(text[index + len('function'):end], name):
            line = text.rfind('\n', 0, index) + 1
            return text[:line] + block.strip('\n') + '\n' + text[line:]
    return None


def _append_script(text, html, block):
    regions = _regions(text, html)
    if not regions:
        return text + '\n<script>' + block + '</script>\n'
    _start, end = regions[-1]
    return text[:end] + '\n' + block.strip('\n') + '\n' + text[end:]


def _enclosing_span(text, html, pos):
    closest = None
    for index, region_end in _walk(text, html):
        if not (text.startswith('function', index) and _boundary(text, index)):
            continue
        brace = text.find('{', index)
        if brace < 0 or brace >= region_end or pos < brace:
            continue
        close = _brace_end(text, brace, region_end)
        if close is None or pos > close:
            continue
        if closest is None or brace >= closest[0]:
            closest = (brace, close + 1)
    return closest


def _contains_send(text, start, end, name):
    index = start
    while index < end:
        step = _skip_noise(text, index, end)
        if step is None:
            return False
        if step != index:
            index = step
            continue
        if text[index] in '"\'`':
            nxt = _skip_string(text, index, end)
            if nxt < 0:
                return False
            index = nxt
            continue
        if text.startswith('sendEvent(', index) and _boundary(text, index) and _starts_send(text, index, name):
            return True
        index += 1
    return False


def _function_spans(text, html, name):
    for index, end in _walk(text, html):
        if not (text.startswith('function', index) and _boundary(text, index) and _after_name(text[index + len('function'):end], name)):
            continue
        brace = text.find('{', index)
        if brace < 0 or brace >= end:
            continue
        close = _brace_end(text, brace, end)
        if close is not None:
            yield index, close + 1


def _call_end(text, open_paren, limit):
    depth = 0
    i = open_paren
    while i < limit:
        step = _skip_noise(text, i, limit)
        if step is None:
            return None
        if step != i:
            i = step
            continue
        if text[i] in '"\'`':
            nxt = _skip_string(text, i, limit)
            if nxt < 0:
                return None
            i = nxt
            continue
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                nxt = i + 1
                while nxt < limit and text[nxt] in ' \t':
                    nxt += 1
                if nxt < limit and text[nxt] == ';':
                    return nxt + 1
                return i + 1
        i += 1
    return None


def _first_string(text, start, limit):
    i = _skip_space(text, start, limit)
    if i >= limit or text[i] not in '"\'':
        return None
    end = _skip_string(text, i, limit)
    if end < 0:
        return None
    from .contact_config import _decode_js_string
    return _decode_js_string(text[i:end])


def _skip_space(text, i, limit):
    while i < limit:
        step = _skip_noise(text, i, limit)
        if step is None or step == i:
            return i
        i = step
    return i


def _brace_end(text, open_index, limit=None):
    limit = len(text) if limit is None else limit
    depth = 0
    i = open_index
    while i < limit:
        step = _skip_noise(text, i, limit)
        if step is None:
            return None
        if step != i:
            i = step
            continue
        if text[i] in '"\'`':
            nxt = _skip_string(text, i, limit)
            if nxt < 0:
                return None
            i = nxt
            continue
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None
