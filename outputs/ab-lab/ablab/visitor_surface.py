"""Browser-visible names and paths. Legacy aliases stay for migration."""
from __future__ import annotations

import re
from pathlib import Path

CONTACT_BRIDGE_SCRIPT_ID = 'pg-contact-bridge'
NUMBER_POOL_SCRIPT_ID = 'pg-number-pool'

LEGACY_CONTACT_BRIDGE_SCRIPT_ID = 'ab-dest-app'
LEGACY_NUMBER_POOL_SCRIPT_ID = 'ab-number-split'

HUMAN_CLICK_SCRIPT_ID = 'pg-click-guard'
HUMAN_BROWSE_DIV_ID = 'pg-verify-slot'
HUMAN_BROWSE_STYLE_ID = 'pg-verify-style'
HUMAN_BROWSE_LOADER_ID = 'pg-verify-loader'
HUMAN_BROWSE_JS_NAME = 'pg-verify.js'
HUMAN_BROWSE_JS_MARK = b'(function(){'

LEGACY_HUMAN_CLICK_SCRIPT_ID = 'ab-human-click'
LEGACY_HUMAN_BROWSE_STYLE_ID = 'ab-human-browse-style'
LEGACY_HUMAN_BROWSE_DIV_ID = 'ab-human-browse'
LEGACY_HUMAN_BROWSE_LOADER_ID = 'ab-human-browse'
LEGACY_HUMAN_BROWSE_JS_NAME = 'ab-human-browse.js'
LEGACY_HUMAN_BROWSE_JS_MARK = b'/* ab-human-browse */'

HUMAN_BUTTON_QUERY_FLAG = 'pg_human'
LEGACY_HUMAN_BUTTON_QUERY_FLAG = 'ab_human'

EMPTY_SLOT_TITLE = '内容准备中'
EMPTY_SLOT_DETAIL = '请稍后再访问。'

EVENT_INGEST_PATH = '/api/j'
LEGACY_EVENT_INGEST_PATH = '/wa-events'

CLIENT_EVENT_PATH = '/api/collect'

ROUTE_PROOF_PREFIX = '/.well-known/site-verify/'
LEGACY_ROUTE_PROOF_PREFIX = '/.well-known/ab-lab-route/'

SESSION_COOKIE = '__Host-pg_sess'
LEGACY_SESSION_COOKIE = '__Host-ab_session'

SPLIT_COOKIE = 'pg_split'
LEGACY_SPLIT_COOKIE = 'ab_split'

WA_COOKIE = 'pg_wa'
LEGACY_WA_COOKIE = 'ab_wa'

LEGACY_ADMIN_ORIGINS = (
    'https://hhucuq.top',
    'http://hhucuq.top',
)

DEFAULT_ADMIN_ORIGIN = 'https://baozha98.sbs'

_HTML_SUFFIX = frozenset({'.html', '.htm'})
_JS_SUFFIX = frozenset({'.js', '.mjs'})
_FINGERPRINT_MARKERS = (
    'ab-', 'ab_', 'ab-lab', 'ablab', 'ab-human', 'ab_human', 'ab-event', 'ab-event-implant',
    'ab-dest-app', 'ab-number-split', 'abGoWhatsApp', 'ab_split', 'ab_wa', 'implant', '伪装', '规避',
)
_HTML_COMMENT_RE = re.compile(r'<!--([\s\S]*?)-->', re.IGNORECASE)
_JS_BLOCK_RE = re.compile(r'/\*[\s\S]*?\*/')
_JS_LINE_RE = re.compile(r'^[ \t]*//[^\n]*(?:ab-|ab_|ab-lab|implant)[^\n]*\n?', re.IGNORECASE | re.MULTILINE)

_ATTR_REWRITES = (
    (f'id="{LEGACY_CONTACT_BRIDGE_SCRIPT_ID}"', f'id="{CONTACT_BRIDGE_SCRIPT_ID}"'),
    (f"id='{LEGACY_CONTACT_BRIDGE_SCRIPT_ID}'", f"id='{CONTACT_BRIDGE_SCRIPT_ID}'"),
    (f'id="{LEGACY_NUMBER_POOL_SCRIPT_ID}"', f'id="{NUMBER_POOL_SCRIPT_ID}"'),
    (f'id="{LEGACY_HUMAN_CLICK_SCRIPT_ID}"', f'id="{HUMAN_CLICK_SCRIPT_ID}"'),
    (f'id="{LEGACY_HUMAN_BROWSE_STYLE_ID}"', f'id="{HUMAN_BROWSE_STYLE_ID}"'),
    (f'id="{LEGACY_HUMAN_BROWSE_DIV_ID}"', f'id="{HUMAN_BROWSE_DIV_ID}"'),
    (f'id="{LEGACY_HUMAN_BROWSE_LOADER_ID}"', f'id="{HUMAN_BROWSE_LOADER_ID}"'),
    (f'"{LEGACY_HUMAN_BROWSE_JS_NAME}"', f'"{HUMAN_BROWSE_JS_NAME}"'),
    (f"'{LEGACY_HUMAN_BROWSE_JS_NAME}'", f"'{HUMAN_BROWSE_JS_NAME}'"),
    (f'&{LEGACY_HUMAN_BUTTON_QUERY_FLAG}=1', f'&{HUMAN_BUTTON_QUERY_FLAG}=1'),
    (f'?{LEGACY_HUMAN_BUTTON_QUERY_FLAG}=1', f'?{HUMAN_BUTTON_QUERY_FLAG}=1'),
)


def _comment_is_fingerprint(body: str) -> bool:
    lowered = body.lower()
    return any(marker.lower() in lowered or marker in body for marker in _FINGERPRINT_MARKERS)


def _strip_html_comments(text: str) -> str:
    def replace(match):
        return '' if _comment_is_fingerprint(match.group(1)) else match.group(0)
    return _HTML_COMMENT_RE.sub(replace, text)


def _strip_js_noise(text: str) -> str:
    def block_replace(match):
        return '' if _comment_is_fingerprint(match.group(0)) else match.group(0)
    updated = _JS_BLOCK_RE.sub(block_replace, text)
    return _JS_LINE_RE.sub('', updated)


def _rewrite_attrs(text: str) -> str:
    updated = text
    for old, new in _ATTR_REWRITES:
        updated = updated.replace(old, new)
    return updated


def polish_visitor_text(text: str, relative: str) -> str:
    suffix = Path(relative).suffix.lower()
    if suffix in _HTML_SUFFIX:
        updated = _strip_html_comments(text)
        return _rewrite_attrs(updated)
    if suffix in _JS_SUFFIX:
        if text.lstrip().startswith(LEGACY_HUMAN_BROWSE_JS_MARK.decode()):
            text = text.lstrip()[len(LEGACY_HUMAN_BROWSE_JS_MARK):].lstrip()
        return _strip_js_noise(text)
    return text


def polish_visitor_bytes(data: bytes, relative: str) -> bytes:
    suffix = Path(relative).suffix.lower()
    if suffix not in _HTML_SUFFIX and suffix not in _JS_SUFFIX:
        return data
    bom = data.startswith(b'\xef\xbb\xbf')
    raw = data[3:] if bom else data
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        return data
    updated = polish_visitor_text(text, relative)
    if updated == text:
        return data
    payload = updated.encode('utf-8')
    return (b'\xef\xbb\xbf' + payload) if bom else payload
