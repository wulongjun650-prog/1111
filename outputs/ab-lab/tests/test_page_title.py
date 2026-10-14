import io
import zipfile

from fastapi.testclient import TestClient

from ablab.page_title import read_document_title, rewrite_title, write_title
from ablab.web import create_admin


PAGE = '''<!doctype html><html><head><title>舊標題</title>
<script async src="https://www.googletagmanager.com/gtag/js?id=G-GVPNN75RYK"></script>
<script>
  function gtag(){dataLayer.push(arguments);}
  gtag('config', 'G-GVPNN75RYK');
  const sample = '<title>腳本裡的標題</title>';
  document.title = document.title;
</script>
</head><body><a href="https://wa.me/85290001111">聯絡</a><p>正文保留</p></body></html>'''


def test_only_the_document_title_changes():
    script = "<script>const sample = '<title>腳本裡的標題</title>';</script>"
    page = f'<html><head><title>舊 &amp; 標題</title>{script}</head><body>正文保留 G-GVPNN75RYK</body></html>'
    updated, changed = write_title(page, '講經MAN | 10月14日港股名單')
    assert changed
    assert '<title>講經MAN | 10月14日港股名單</title>' in updated
    assert script in updated
    assert '正文保留 G-GVPNN75RYK' in updated
    assert read_document_title(updated) == '講經MAN | 10月14日港股名單'
    same, again = write_title(updated, '講經MAN | 10月14日港股名單')
    assert same == updated and not again
    other = page.replace('<title>舊 &amp; 標題</title>', '')
    inserted, did = write_title(other, 'A & B')
    assert did
    assert inserted.startswith('<html><head><title>A &amp; B</title>')
    assert script in inserted and '正文保留 G-GVPNN75RYK' in inserted


def test_other_files_stay_byte_for_byte():
    side = b'<p>\xe6\x97\xa7\xe9\xa1\xb5 <title>\xe4\xb8\x8d\xe5\x8a\xa8</title></p>'
    edited, changed, chosen = rewrite_title([('index.html', PAGE.encode()), ('more.htm', side)], '新標題')
    assert changed and chosen == 'index.html'
    files = dict(edited)
    assert files['more.htm'] == side
    assert '正文保留' in files['index.html'].decode()
    assert 'G-GVPNN75RYK' in files['index.html'].decode()
    assert '85290001111' in files['index.html'].decode()
    assert "document.title = document.title" in files['index.html'].decode()
    assert '<title>新標題</title>' in files['index.html'].decode()
    assert "sample = '<title>腳本裡的標題</title>'" in files['index.html'].decode()


def client(tmp_path):
    app = create_admin(tmp_path)
    http = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 5000))
    http.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': app.state.csrf})
    return http, app


def publish(http, slot, html, extra='<p>其他页不动</p>'.encode()):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('index.html', html)
        bundle.writestr('more.htm', extra)
    uploaded = http.post(f'/api/upload/{slot}?name=page.zip', content=archive.getvalue())
    assert uploaded.status_code == 200, uploaded.text
    version = uploaded.json()['version']['id']
    assert http.post(f'/api/publish/{slot}', json={'version_id': version}).status_code == 200
    return version


def test_saving_a_title_publishes_only_that_change(tmp_path):
    http, app = client(tmp_path)
    version = publish(http, 'B', PAGE)
    publish(http, 'A', '<html><head><title>A 頁</title></head><body>A正文</body></html>', '<p>A其他</p>'.encode())
    before = (tmp_path / 'pages' / version / 'more.htm').read_bytes()
    saved = http.post('/api/page-title', json={'slot': 'B', 'title': '講經MAN | 10月14日港股名單', 'expected_published': version})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body['title'] == '講經MAN | 10月14日港股名單'
    assert body['titles']['B']['title'] == '講經MAN | 10月14日港股名單'
    assert body['titles']['A']['title'] == 'A 頁'
    assert body['titles']['B']['version_id'] != version
    page = (tmp_path / 'pages' / body['titles']['B']['version_id'] / 'index.html').read_text()
    side = (tmp_path / 'pages' / body['titles']['B']['version_id'] / 'more.htm').read_bytes()
    assert side == before
    assert '<title>講經MAN | 10月14日港股名單</title>' in page
    assert '正文保留' in page and '85290001111' in page and 'G-GVPNN75RYK' in page
    assert "sample = '<title>腳本裡的標題</title>'" in page
    a_page = (tmp_path / 'pages' / body['titles']['A']['version_id'] / 'index.html').read_text()
    assert '<title>A 頁</title>' in a_page and 'A正文' in a_page
    again = http.post('/api/page-title', json={'slot': 'B', 'title': '講經MAN | 10月14日港股名單', 'expected_published': body['titles']['B']['version_id']})
    assert again.status_code == 400 and '已经是这个' in again.text
    rejected = http.post('/api/page-title', json={'slot': 'B', 'title': '<script>', 'expected_published': body['titles']['B']['version_id']})
    assert rejected.status_code == 400
    stale = http.post('/api/page-title', json={'slot': 'B', 'title': '另一個標題', 'expected_published': version})
    assert stale.status_code == 409
    mirror = http.post('/api/sites', json={'domain': 'watch.example'}).json()['site']
    source = http.post('/api/sites', json={'domain': 'source.example'}).json()['site']
    app.state.registry.bind_mirror(mirror['id'], source['id'], 'c0fbe3810feb49cd97f9ed02b7cb86c0')
    blocked = http.post(f"/api/sites/{mirror['id']}/page-title", json={'slot': 'B', 'title': '不該寫上', 'expected_published': body['titles']['B']['version_id']})
    assert blocked.status_code == 400 and '投放域名' in blocked.text
