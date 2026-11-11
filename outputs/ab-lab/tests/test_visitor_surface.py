from pathlib import Path

from ablab import visitor_surface as surface
from ablab.models import Config
from ablab.store import Store


def test_new_site_defaults_to_slot_a(tmp_path):
    config, _ = Store(tmp_path).config()
    assert config.allowed_slot == 'A'


def test_polish_strips_fingerprint_html_comments():
    raw = (
        '<!doctype html><!-- ab-event-implant legacy --><body>'
        f'<script id="{surface.LEGACY_CONTACT_BRIDGE_SCRIPT_ID}"></script>'
        f'<a href="?{surface.LEGACY_HUMAN_BUTTON_QUERY_FLAG}=1">go</a>'
        '</body>'
    )
    cleaned = surface.polish_visitor_text(raw, 'index.html')
    assert 'ab-event-implant' not in cleaned
    assert f'id="{surface.CONTACT_BRIDGE_SCRIPT_ID}"' in cleaned
    assert f'{surface.HUMAN_BUTTON_QUERY_FLAG}=1' in cleaned
    assert '<!--' not in cleaned


def test_polish_strips_legacy_human_js_banner():
    raw = surface.LEGACY_HUMAN_BROWSE_JS_MARK.decode() + '\n(function(){})();'
    cleaned = surface.polish_visitor_text(raw, 'ab-human-browse.js')
    assert surface.LEGACY_HUMAN_BROWSE_JS_MARK.decode() not in cleaned
    assert cleaned.startswith('(function')


def test_polish_visitor_bytes_preserves_bom():
    raw = '\ufeff<html><!-- ab-lab marker --></html>'
    data = surface.polish_visitor_bytes(raw.encode('utf-8'), 'x.htm')
    assert data.startswith(b'\xef\xbb\xbf')
    assert b'ab-lab' not in data
