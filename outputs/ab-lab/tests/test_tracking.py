import io
import zipfile

from fastapi.testclient import TestClient

from ablab.tracking import normalize_conversion, normalize_ga4, rewrite_pages
from ablab.web import create_admin


GA4 = '''<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-GVPNN75RYK"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'G-GVPNN75RYK');
</script>'''

CONVERSION = '''<!-- Event snippet for 联系人 conversion page
In your html page, add the snippet and call gtag_report_conversion when someone clicks on the chosen link or button. -->
<script>
function gtag_report_conversion(url) {
  var callback = function () {
    if (typeof(url) != 'undefined') {
      window.location = url;
    }
  };
  gtag('event', 'conversion', {
      'send_to': 'AW-11421360067/Z7iXCK6y948dEMO_kMYq',
      'event_callback': callback
  });
  return false;
}
</script>'''

OLD_PAGE = '''<!doctype html><html><head><title>旧页</title>
<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-OLDOLDOLD1"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'G-OLDOLDOLD1');
</script>
<!-- Event snippet for 联系人 conversion page -->
<script>
function gtag_report_conversion(url) {
  var callback = function () {
    if (typeof(url) != 'undefined') { window.location = url; }
  };
  gtag('event', 'conversion', {'send_to': 'AW-10000000000/oldlabelxx', 'event_callback': callback});
  return false;
}
</script>
</head><body><p>正文保留</p></body></html>'''


def test_snippets_keep_one_id_and_reject_fragments():
    assert normalize_ga4(GA4)[0] == 'G-GVPNN75RYK'
    assert normalize_conversion(CONVERSION)[0] == 'AW-11421360067/Z7iXCK6y948dEMO_kMYq'
    for body in ('<script>gtag("config","G-GVPNN75RYK")</script>', 'hello', GA4.replace('G-GVPNN75RYK', 'AW-1')):
        try:
            normalize_ga4(body)
        except ValueError:
            continue
        raise AssertionError(body)
    try:
        normalize_conversion('<script>function gtag_report_conversion(url){return false;}</script>')
    except ValueError:
        return
    raise AssertionError('conversion without send_to')


def test_existing_blocks_are_replaced_and_missing_blocks_are_inserted():
    replaced, changed = rewrite_pages([('index.html', OLD_PAGE.encode()), ('more.htm', '<p>不动</p>'.encode())], GA4, CONVERSION)
    page = dict(replaced)['index.html'].decode()
    assert changed
    assert page.count('G-GVPNN75RYK') == 2
    assert 'G-OLDOLDOLD1' not in page
    assert 'AW-11421360067/Z7iXCK6y948dEMO_kMYq' in page
    assert 'AW-10000000000/oldlabelxx' not in page
    assert '正文保留' in page
    assert dict(replaced)['more.htm'] == '<p>不动</p>'.encode()
    fresh = '<html><head><title>新</title></head><body>空</body></html>'
    inserted, changed = rewrite_pages([('index.html', fresh.encode())], GA4, None)
    text = dict(inserted)['index.html'].decode()
    assert changed and 'G-GVPNN75RYK' in text and 'gtag_report_conversion' not in text and '空' in text
    ga4_only, _ = rewrite_pages([('index.html', OLD_PAGE.encode())], GA4, None)
    kept = dict(ga4_only)['index.html'].decode()
    assert 'G-GVPNN75RYK' in kept and 'AW-10000000000/oldlabelxx' in kept and 'G-OLDOLDOLD1' not in kept


def client(tmp_path):
    app = create_admin(tmp_path)
    http = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 5000))
    http.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': app.state.csrf})
    return http


def publish(http, prefix, slot, html, name='page.zip'):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('index.html', html)
        bundle.writestr('more.htm', '<p>其他页 G-OLDOLDOLD1</p>')
    uploaded = http.post(prefix + f'/upload/{slot}?name={name}', content=archive.getvalue())
    assert uploaded.status_code == 200, uploaded.text
    version = uploaded.json()['version']['id']
    assert http.post(prefix + f'/publish/{slot}', json={'version_id': version}).status_code == 200
    return version


def test_saved_codes_are_shared_and_written_into_the_published_page(tmp_path):
    http = client(tmp_path)
    other = http.post('/api/sites', json={'domain': 'other.example.com'}).json()['site']['id']
    saved = http.post('/api/tracking', json={'kind': 'ga4', 'body': GA4, 'note': '主号'})
    assert saved.status_code == 200, saved.text
    ga4_id = saved.json()['snippet']['id']
    conversion = http.post('/api/tracking', json={'kind': 'conversion', 'body': CONVERSION, 'note': '联系人'})
    assert conversion.status_code == 200, conversion.text
    conversion_id = conversion.json()['snippet']['id']
    shared = http.get(f'/api/sites/{other}/tracking').json()['snippets']
    assert [item['label'] for item in shared] == ['G-GVPNN75RYK', 'AW-11421360067/Z7iXCK6y948dEMO_kMYq']
    version = publish(http, '/api', 'A', OLD_PAGE)
    publish(http, '/api', 'B', '<html><head></head><body>B</body></html>')
    applied = http.post('/api/tracking/apply', json={'slot': 'A', 'ga4_id': ga4_id, 'expected_published': version})
    assert applied.status_code == 200, applied.text
    current = applied.json()['published']
    assert current['A']['ga4'] == 'G-GVPNN75RYK'
    assert current['A']['conversion'] == 'AW-10000000000/oldlabelxx'
    assert current['B']['ga4'] == ''
    both = http.post('/api/tracking/apply', json={'slot': 'A', 'conversion_id': conversion_id, 'expected_published': current['A']['version_id']})
    assert both.status_code == 200, both.text
    assert both.json()['published']['A']['conversion'] == 'AW-11421360067/Z7iXCK6y948dEMO_kMYq'
    page = (tmp_path / 'pages' / both.json()['published']['A']['version_id'] / 'index.html').read_text()
    assert page.count('function gtag_report_conversion') == 1
    assert '正文保留' in page
    again = http.post('/api/tracking/apply', json={'slot': 'A', 'ga4_id': ga4_id, 'expected_published': both.json()['published']['A']['version_id']})
    assert again.status_code == 400
    assert '已经是这一份' in again.text
    assert http.delete(f'/api/tracking/{ga4_id}').status_code == 200
    assert http.get('/api/tracking').json()['snippets'][0]['kind'] == 'conversion'
