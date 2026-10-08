"""Navigation scanners must leave inert source and unrelated JavaScript alone."""
import pytest

from ablab import redirects
from ablab.store import Conflict


def bundle(tmp_path, html, javascript=None):
    pages = tmp_path / 'pages'
    root = pages / 'original'
    root.mkdir(parents=True)
    (root / 'index.html').write_text(html, encoding='utf-8')
    if javascript is not None:
        (root / 'app.js').write_text(javascript, encoding='utf-8')
    return root, pages


@pytest.mark.parametrize('element', ['textarea', 'title'])
def test_rcdata_markup_is_not_navigation(tmp_path, element):
    inert = f'<{element}><a href="https://example.com/documentation">sample</a></{element}>'
    root, pages = bundle(tmp_path, inert + '<a href="https://real.example/">go</a>')
    occurrences, _ = redirects.scan_bundle(root, 'original')
    assert [item['url'] for item in occurrences] == ['https://real.example/']
    version, count = redirects.replace_bundle(root, 'original', [occurrences[0]['id']],
                                              'https://new.example/', pages, 'changed.zip')
    assert count == 1
    assert (pages / version['id'] / 'index.html').read_text(encoding='utf-8') == (
        inert + '<a href="https://new.example/">go</a>')


@pytest.mark.parametrize('attribute', [
    'javascript:location.href=\'https://old.example/\'',
    'javascript:window.open(&quot;https://old.example/&quot;)',
])
def test_javascript_anchor_is_scanned_and_replaced(tmp_path, attribute):
    root, pages = bundle(tmp_path, f'<a href="{attribute}">go</a>')
    occurrences, _ = redirects.scan_bundle(root, 'original')
    assert [item['url'] for item in occurrences] == ['https://old.example/']
    version, count = redirects.replace_bundle(root, 'original', [occurrences[0]['id']],
                                              'https://new.example/?x=1&y=2', pages, 'changed.zip')
    assert count == 1
    replaced, _ = redirects.scan_bundle(pages / version['id'], version['id'])
    assert [item['url'] for item in replaced] == ['https://new.example/?x=1&y=2']
    assert (root / 'index.html').read_text(encoding='utf-8') == f'<a href="{attribute}">go</a>'


def test_script_with_valueless_type_is_default_javascript(tmp_path):
    root, _ = bundle(tmp_path, '<script type>location.href="https://old.example/";</script>')
    occurrences, _ = redirects.scan_bundle(root, 'original')
    assert [item['url'] for item in occurrences] == ['https://old.example/']


@pytest.mark.parametrize('control', ['if (condition)', 'while (condition)', 'for (; condition; )'])
def test_regex_after_control_parenthesis_is_inert(tmp_path, control):
    source = control + r' /location.href="https:\/\/api.example\/resource";/.test(input);'
    source += '\nwindow.location.href="https://real.example/";'
    root, _ = bundle(tmp_path, '<script src="app.js"></script>', source)
    occurrences, _ = redirects.scan_bundle(root, 'original')
    assert [item['url'] for item in occurrences] == ['https://real.example/']


def test_function_local_constant_cannot_resolve_global_navigation(tmp_path):
    source = ('function unused(){const target="https://unrelated.example/";} '
              'location.href=target; window.location.href="https://real.example/";')
    root, _ = bundle(tmp_path, '<script src="app.js"></script>', source)
    occurrences, warnings = redirects.scan_bundle(root, 'original')
    assert [item['url'] for item in occurrences] == ['https://real.example/']
    assert warnings


@pytest.mark.parametrize('local_source', [
    'function render(location){location.href="https://api.example/resource";}',
    'function render(window){window.location.href="https://api.example/resource";}',
    'function open(url){return fetch(url)};open("https://api.example/resource");',
])
def test_shadowed_navigation_globals_are_not_navigation(tmp_path, local_source):
    # The first two bind only inside their functions; the last shadows bare open globally.
    source = local_source + '\nwindow.location.href="https://real.example/";'
    root, _ = bundle(tmp_path, '<script src="app.js"></script>', source)
    occurrences, _ = redirects.scan_bundle(root, 'original')
    assert [item['url'] for item in occurrences] == ['https://real.example/']


def test_repetitive_assignment_respects_scan_token_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(redirects, 'MAX_SCAN_TOKENS', 50, raising=False)
    source = 'x=' * 200 + '"https://unused.example/";location.href="https://real.example/";'
    root, _ = bundle(tmp_path, '<script src="app.js"></script>', source)
    with pytest.raises(ValueError):
        redirects.scan_bundle(root, 'original')


def test_replacement_uses_the_exact_bytes_that_were_scanned(tmp_path, monkeypatch):
    original = '<a href="https://old.example/">go</a>'
    root, pages = bundle(tmp_path, original)
    occurrences, _ = redirects.scan_bundle(root, 'original')
    before = {path.name for path in pages.iterdir()}
    actual_read, reads = redirects.read_source, 0

    def mutate_before_second_read(file, path):
        nonlocal reads
        reads += 1
        if reads == 2:
            file.write_text('<p>changed after span validation</p>', encoding='utf-8')
        return actual_read(file, path)

    monkeypatch.setattr(redirects, 'read_source', mutate_before_second_read)
    try:
        version, count = redirects.replace_bundle(root, 'original', [occurrences[0]['id']],
                                                  'https://new.example/', pages, 'changed.zip')
    except (Conflict, ValueError):
        assert {path.name for path in pages.iterdir()} == before
    else:
        assert count == 1
        assert (pages / version['id'] / 'index.html').read_text(encoding='utf-8') == (
            '<a href="https://new.example/">go</a>')


def test_relative_destinations_brackets_concat_and_conditional(tmp_path):
    source = '''const base="https://old.example/"; const suffix="join";
window["location"]["href"]=base+suffix;
location.replace(condition ? "https://old.example/a" : "https://old.example/b");
function go(){const local="https://local.example/";window.open(local);}
const run=(window)=>window.location.href="https://unrelated.example/";
location.href="/relative";'''
    root, pages = bundle(tmp_path, '<a href="next.html">go</a><a href="#same">same</a><a href="mailto:x@y.test">email</a>', source)
    occurrences, _ = redirects.scan_bundle(root, 'original')
    assert {item['url'] for item in occurrences} == {'next.html', '/relative', 'https://old.example/join',
                                                    'https://old.example/a','https://old.example/b','https://local.example/'}
    version, changed = redirects.replace_bundle(root, 'original', [item['id'] for item in occurrences],
                                                "https://new.example/?s='quote'&x=2", pages, 'changed.zip')
    assert changed == 6
    after, _ = redirects.scan_bundle(pages/version['id'],version['id'])
    assert {item['url'] for item in after} == {"https://new.example/?s='quote'&x=2"}
    assert 'https://unrelated.example/' in (pages/version['id']/'app.js').read_text(encoding='utf-8')


@pytest.mark.parametrize('source', ['x' + '.a'*70 + '=1;location.href="https://actual.example/";',
                                  'x='*200 + '"https://unused.example/";location.href="https://actual.example/";'])
def test_complex_expressions_stop_with_a_reviewable_error(tmp_path, source):
    root, _ = bundle(tmp_path, '<script src="app.js"></script>', source)
    with pytest.raises(ValueError, match='复杂'):
        redirects.scan_bundle(root,'original')


def test_whatsapp_template_built_in_a_function_is_the_jump(tmp_path):
    source = '''const CONFIG = { whatsappNumber: '85257980601' };
function buildWhatsAppUrl(message){
  const phone = String(CONFIG.whatsappNumber || '').replace(/\\D/g,'');
  const params = new URLSearchParams();
  params.set('phone', phone);
  params.set('text', message);
  return `https://api.whatsapp.com/send?${params.toString()}`;
}
function goWhatsApp(){
  const url = buildWhatsAppUrl('hello');
  setTimeout(() => { window.location.assign(url); }, 180);
}
fetch(buildWhatsAppUrl('nope'));'''
    root, pages = bundle(tmp_path, f'<script>{source}</script><img src="assets/banner.webp">')
    occurrences, warnings = redirects.scan_bundle(root, 'original')
    assert [(item['kind'], item['url']) for item in occurrences] == [('whatsapp_number', '85257980601')]
    assert not warnings
    version, count = redirects.replace_bundle(root, 'original', [occurrences[0]['id']],
                                              '85299990000', pages, 'changed.zip')
    assert count == 1
    rewritten = (pages / version['id'] / 'index.html').read_text(encoding='utf-8')
    assert '85257980601' not in rewritten
    assert '85299990000' in rewritten
    assert 'api.whatsapp.com' in rewritten
    assert "params.set('text', message)" in rewritten
    rescanned, _ = redirects.scan_bundle(pages / version['id'], version['id'])
    assert [(item['kind'], item['url']) for item in rescanned] == [('whatsapp_number', '85299990000')]
    again, copied = redirects.replace_bundle(pages / version['id'], version['id'], [rescanned[0]['id']],
                                             '85299990000', pages, 'same.zip')
    assert again is None and copied == 0
    assert {path.name for path in pages.iterdir()} == {'original', version['id']}


def test_landing_page_lists_visitor_jumps_and_skips_google_tags(tmp_path):
    page = '''<script async src="https://www.googletagmanager.com/gtag/js?id=AW-1"></script>
<script>
  gtag('config', 'AW-1');
  gtag('config', 'G-1');
  const CONFIG = { whatsappNumber: '85257980601', googleSendTo: 'AW-1/abc' };
  function buildWhatsAppUrl(message){
    const phone = String(CONFIG.whatsappNumber || '').replace(/\\D/g,'');
    const params = new URLSearchParams();
    params.set('phone', phone);
    params.set('text', message);
    params.set('type', 'phone_number');
    params.set('app_absent', '0');
    return `https://api.whatsapp.com/send?${params.toString()}`;
  }
  function go(){ const url = buildWhatsAppUrl('hi'); window.location.assign(url); }
</script>
<script src="https://connect.facebook.net/en_US/fbevents.js"></script>
<a href="https://www.youtube.com/@EconManBlog/shorts">YouTube</a>'''
    root, _ = bundle(tmp_path, page)
    occurrences, warnings = redirects.scan_bundle(root, 'original')
    assert [(item['kind'], item['url']) for item in occurrences] == [
        ('whatsapp_number', '85257980601'),
        ('anchor', 'https://www.youtube.com/@EconManBlog/shorts')]
    assert not warnings
    assert all('google' not in item['url'] and 'facebook' not in item['url'] for item in occurrences)


def test_whatsapp_prefill_and_wa_me_number_are_listed(tmp_path):
    source = r'''const CONFIG = { whatsappNumber: '852-5798-0601' };
const VARIANTS = {
  blackhorse: { message: '你好，黑马' },
  picks: { message: '心水' }
};
function buildWhatsAppUrl(message){
  const phone = String(CONFIG.whatsappNumber || '').replace(/\D/g,'');
  const params = new URLSearchParams();
  params.set('phone', phone);
  params.set('text', encodeURIComponent(message));
  return `https://api.whatsapp.com/send?${params.toString()}`;
}
function go(){
  const data = VARIANTS[currentKey] || VARIANTS.blackhorse;
  const extra = `\n${gateAnswers.interest}`;
  window.location.assign(buildWhatsAppUrl(data.message + extra));
}
const direct = '85211112222';
location.href = `https://wa.me/${direct}?text=${missing}`;
location.href = 'https://wa.me/' + CONFIG.whatsappNumber;'''
    root, _ = bundle(tmp_path, f'<script>{source}</script>')
    occurrences, warnings = redirects.scan_bundle(root, 'original')
    assert [(item['kind'], item['url']) for item in occurrences] == [
        ('whatsapp_number', '85257980601'),
        ('whatsapp_number', '85211112222')]
    assert not warnings
    assert all('心水' not in item['url'] and 'gateAnswers' not in item['url'] for item in occurrences)


def test_encoded_whatsapp_text_is_encoded_once(tmp_path):
    source = '''function build(message){
  const params = new URLSearchParams();
  params.set('phone', '85257980601');
  params.set('text', encodeURIComponent(message));
  return `https://api.whatsapp.com/send?${params.toString()}`;
}
location.href = build('hello world');'''
    root, pages = bundle(tmp_path, '<script src="app.js"></script>', source)
    occurrences, warnings = redirects.scan_bundle(root, 'original')
    assert [(item['kind'], item['url']) for item in occurrences] == [('whatsapp_number', '85257980601')]
    assert not warnings
    version, _ = redirects.replace_bundle(root, 'original', [occurrences[0]['id']], '85288887777', pages, 'changed.zip')
    rewritten = (pages / version['id'] / 'app.js').read_text(encoding='utf-8')
    assert 'api.whatsapp.com' in rewritten and 'hello world' in rewritten
    assert '85257980601' not in rewritten and '85288887777' in rewritten


def test_static_whatsapp_url_stays_a_full_link(tmp_path):
    source = '''location.href = "https://api.whatsapp.com/send?phone=85257980601&text=hello";
location.href = "https://www.youtube.com/@EconManBlog/shorts";'''
    root, _ = bundle(tmp_path, '<script src="app.js"></script>', source)
    occurrences, warnings = redirects.scan_bundle(root, 'original')
    assert occurrences[0]['kind'] == 'js_location'
    assert occurrences[0]['url'].startswith('https://api.whatsapp.com/send?phone=85257980601')
    assert occurrences[1]['url'] == 'https://www.youtube.com/@EconManBlog/shorts'
    assert not warnings


def test_dynamic_host_template_and_nested_function_stay_precise(tmp_path):
    source = '''function build(){ return `https://api.whatsapp.com/send?${q}`; }
function wrap(){
  function build(){ return `https://${host}/hidden`; }
  location.href = build();
}
function direct(){ location.href = `https://wa.me/${phone}`; }
fetch(build());
location.href = "https://real.example/";'''
    root, _ = bundle(tmp_path, '<script src="app.js"></script>', source)
    occurrences, warnings = redirects.scan_bundle(root, 'original')
    assert {item['url'] for item in occurrences} == {'https://wa.me/', 'https://real.example/'}
    assert warnings


def test_whatsapp_object_fields_are_the_jump(tmp_path):
    source = r'''const CONFIG = { whatsappNumber: '85257980601', googleSendTo: 'AW-1/abc' };
function gtag_report_conversion(url) {
  var callback = function () {
    if (typeof(url) != 'undefined') { window.location = url; }
  };
}
function getWhatsAppUrls(message) {
  const phone = String(CONFIG.whatsappNumber || '').replace(/\D/g, '');
  const encodedMsg = encodeURIComponent(message);
  return {
    scheme: `whatsapp://send?phone=${phone}&text=${encodedMsg}`,
    universal: `https://wa.me/${phone}?text=${encodedMsg}`
  };
}
function goWhatsApp() {
  const fullMessage = '你好';
  const urls = getWhatsAppUrls(fullMessage);
  if (true) { window.location.href = urls.scheme; }
  setTimeout(() => { window.location.href = urls.universal; }, 1500);
  window.open(urls.universal, '_blank');
}'''
    root, pages = bundle(tmp_path, f'<script>{source}</script><a href="https://www.youtube.com/@EconManBlog/shorts">YouTube</a>')
    occurrences, warnings = redirects.scan_bundle(root, 'original')
    assert [(item['kind'], item['url']) for item in occurrences] == [
        ('whatsapp_number', '85257980601'),
        ('anchor', 'https://www.youtube.com/@EconManBlog/shorts')]
    assert len(warnings) == 1 and '无法静态解析' in warnings[0]
    version, count = redirects.replace_bundle(root, 'original', [occurrences[0]['id']],
                                              '85211112222', pages, 'changed.zip')
    assert count == 1
    rewritten = (pages / version['id'] / 'index.html').read_text(encoding='utf-8')
    assert '85257980601' not in rewritten and '85211112222' in rewritten
    assert 'whatsapp://send?phone=' in rewritten and 'https://wa.me/' in rewritten
    assert 'encodeURIComponent(message)' in rewritten
    rescanned, _ = redirects.scan_bundle(pages / version['id'], version['id'])
    assert ('whatsapp_number', '85211112222') in [(item['kind'], item['url']) for item in rescanned]
    again, copied = redirects.replace_bundle(pages / version['id'], version['id'], [rescanned[0]['id']],
                                             '85211112222', pages, 'same.zip')
    assert again is None and copied == 0


def test_long_assignment_still_reaches_the_jump(tmp_path):
    pieces = ' + '.join(f'"{index}"' for index in range(160))
    source = f'const noise = ({pieces});\nlocation.href = "https://actual.example/";'
    root, _ = bundle(tmp_path, '<script src="app.js"></script>', source)
    occurrences, warnings = redirects.scan_bundle(root, 'original')
    assert [item['url'] for item in occurrences] == ['https://actual.example/']
    assert not warnings


def test_shorthand_targets_and_wa_links_are_one_number(tmp_path):
    page = r'''<a class="cta" href="https://wa.me/85257980601?text=hello%20list">领取</a>
<a href="https://api.whatsapp.com/send?phone=85257980601&amp;text=hello">备用</a>
<a href="https://chat.whatsapp.com/InviteCode">群</a>
<a href="https://www.youtube.com/@EconManBlog/shorts">YouTube</a>
<script>
const CONFIG = { whatsappNumber: '85257980601', receptionist: 'Chloe' };
function buildWhatsAppUrls(message) {
  const phone = String(CONFIG.whatsappNumber || '').replace(/\D/g, '');
  const text = encodeURIComponent(message);
  const universal = `https://wa.me/${phone}?text=${text}`;
  return {
    phone,
    universal,
    scheme: `whatsapp://send?phone=${phone}&text=${text}`,
    intent: `intent://send?phone=${phone}&text=${text}#Intent;scheme=whatsapp;package=com.whatsapp;end`
  };
}
function goWhatsApp(e, locationName) {
  const anchor = e && e.currentTarget;
  const urls = buildWhatsAppUrls('hi');
  let target = urls.universal;
  if (WA_ENV === 'ios_webview') target = urls.scheme;
  else if (WA_ENV === 'android_browser' && ANDROID_INTENT_OK) target = urls.intent;
  if (anchor) anchor.href = target;
  else if (WA_ENV === 'desktop') window.open(target, '_blank');
  else window.location.href = target;
}
</script>'''
    root, pages = bundle(tmp_path, page)
    occurrences, warnings = redirects.scan_bundle(root, 'original')
    numbers = [item for item in occurrences if item['kind'] == 'whatsapp_number']
    others = [(item['kind'], item['url']) for item in occurrences if item['kind'] != 'whatsapp_number']
    assert [item['url'] for item in numbers] == ['85257980601', '85257980601', '85257980601']
    assert others == [
        ('anchor', 'https://chat.whatsapp.com/InviteCode'),
        ('anchor', 'https://www.youtube.com/@EconManBlog/shorts')]
    assert not warnings
    version, count = redirects.replace_bundle(root, 'original', [item['id'] for item in numbers],
                                              '85211112222', pages, 'changed.zip')
    assert count == 3
    rewritten = (pages / version['id'] / 'index.html').read_text(encoding='utf-8')
    assert '85257980601' not in rewritten and rewritten.count('85211112222') == 3
    assert 'href="https://wa.me/85211112222?text=hello%20list"' in rewritten
    assert 'href="https://api.whatsapp.com/send?phone=85211112222&amp;text=hello"' in rewritten
    assert 'https://wa.me/${phone}?text=${text}' in rewritten
    assert 'whatsapp://send?phone=${phone}' in rewritten
    assert 'intent://send?phone=${phone}' in rewritten
    assert 'function goWhatsApp(e, locationName)' in rewritten
    assert 'youtube.com/@EconManBlog/shorts' in rewritten
    assert 'chat.whatsapp.com/InviteCode' in rewritten


def test_repeated_targets_that_disagree_stay_a_warning(tmp_path):
    source = ('let target = "https://one.example/";\n'
              'target = "https://two.example/";\n'
              'location.href = target;\n'
              'window.location.href = "https://actual.example/";')
    root, _ = bundle(tmp_path, '<script src="app.js"></script>', source)
    occurrences, warnings = redirects.scan_bundle(root, 'original')
    assert [item['url'] for item in occurrences] == ['https://actual.example/']
    assert warnings and '无法静态解析' in warnings[0]


def test_dom_anchor_href_and_setattribute_are_navigation(tmp_path):
    source = '''const destination="https://old.example/";
document.querySelector("#destination").href=destination;
const button=document.getElementById("go");button.href="https://old.example/button";
document.createElement("a").setAttribute("href","https://old.example/attribute");
document.querySelector("link.stylesheet").href="https://api.example/style.css";
const stylesheet=document.createElement("link");stylesheet.href="https://api.example/other.css";'''
    root,pages=bundle(tmp_path,'<a id="destination" href="https://old.example/index">go</a>',source)
    occurrences,_=redirects.scan_bundle(root,'original')
    assert len(occurrences)==4 and all('api.example' not in item['url'] for item in occurrences)
    version,count=redirects.replace_bundle(root,'original',[item['id'] for item in occurrences],
                                          'https://new.example/',pages,'changed.zip')
    assert count==4
    rescanned,_=redirects.scan_bundle(pages/version['id'],version['id'])
    assert {item['url'] for item in rescanned}=={'https://new.example/'}
    assert 'https://api.example/style.css' in (pages/version['id']/'app.js').read_text(encoding='utf-8')
