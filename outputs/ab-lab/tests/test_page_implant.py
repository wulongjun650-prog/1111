import json
import subprocess

from fastapi.testclient import TestClient

from ablab.page_implant import implant_text, republish_implant, scan_implant
from ablab.web import create_admin


OLD = '''<!doctype html><script>
const CONFIG = {
  whatsappNumber: "85257989850",
  googleSendTo: 'AW-11421360067/KEEP',
  leadEndpoint: ''
};
function track(name, params) {}
function goWhatsApp(e, locationName) {
  let target = urls.universal;
  if (WA_ENV === 'ios_webview') target = urls.scheme;
  waAttempt = { t: Date.now(), placement: locationName || 'unknown', left: false };
  track('whatsapp_click', { event_label: locationName || 'unknown' });
  track('conversion', { send_to: CONFIG.googleSendTo });
}
function markLeft() {
  track('wa_app_opened', { placement: waAttempt.placement });
}
// track('whatsapp_click', { event_label: 'comment' });
const note = "track('whatsapp_click', { event_label: 'string' })";
</script>'''

NEW = '''<!doctype html><script>
const CONFIG = { whatsappNumber: "852", eventEndpoint: '', googleSendTo: 'AW-KEEP' };
function track(name, params) {}
function goWhatsApp() {
  const txId = 't';
  let linkType = 'universal';
  waAttempt = { t: Date.now(), placement: 'hero', left: false, linkType, txId };
  track('whatsapp_click', { event_label: 'hero' });
  track('conversion', { send_to: CONFIG.googleSendTo, transaction_id: txId });
  sendEvent('whatsapp_click', { transaction_id: txId });
}
function sendEvent(name, extra) {}
</script>'''


def test_old_page_gains_the_beacon_and_keeps_google_and_the_jump():
    updated, status = implant_text(OLD, True)
    assert status == {'state': 'implanted', 'label': '植入成功'}
    assert "eventEndpoint: ''" in updated
    assert "googleSendTo: 'AW-11421360067/KEEP'" in updated
    assert "let target = urls.universal;" in updated
    assert "if (WA_ENV === 'ios_webview') target = urls.scheme;" in updated
    assert "track('conversion', { send_to: CONFIG.googleSendTo });" in updated
    assert updated.count("sendEvent('whatsapp_click'") == 1
    assert "sendEvent('wa_app_opened', {});" in updated
    assert "track('whatsapp_click', { event_label: 'comment' })" in updated
    assert "track('whatsapp_click', { event_label: 'string' })" in updated
    assert 'txId: abTx' in updated and 'linkType: abLink' in updated
    again, second = implant_text(updated, True)
    assert again == updated and second['label'] == '植入成功'


def test_a_line_between_track_and_send_is_already_implanted():
    page = '''<!doctype html><script>
const CONFIG = { whatsappNumber: "852", eventEndpoint: '', googleSendTo: 'AW-KEEP' };
function track(name, params) {}
function sendEvent(name, extra) {}
function goWhatsApp() {
  const txId = 't';
  waAttempt = { t: Date.now(), placement: 'hero', left: false, linkType: 'universal', txId };
  track('whatsapp_click', { event_label: 'hero' });
  var keep = txId;
  sendEvent("whatsapp_click", { transaction_id: txId });
}
</script>'''
    updated, status = implant_text(page, True)
    assert updated == page
    assert status == {'state': 'ready', 'label': '已有植入代码'}


def test_page_that_already_reports_is_left_byte_for_byte():
    updated, status = implant_text(NEW, True)
    assert updated == NEW
    assert status == {'state': 'ready', 'label': '已有植入代码'}


def test_plain_page_is_not_rewritten():
    page = '<!doctype html><h1>只是说明</h1>'
    updated, status = implant_text(page, True)
    assert updated == page
    assert status['label'] == '这一页没有跳转代码，没有改'


def test_upload_reports_the_status_and_a_second_drop_says_already_present(tmp_path):
    admin = TestClient(create_admin(tmp_path), base_url='http://127.0.0.1:8765', client=('127.0.0.1', 9))
    admin.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': admin.app.state.csrf})
    uploaded = admin.post('/api/upload/B?name=old.html', content=OLD.encode())
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()['implant']['label'] == '植入成功'
    version = uploaded.json()['version']['id']
    assert admin.post('/api/publish/B', json={'version_id': version}).status_code == 200
    scan = admin.get('/api/b-redirects').json()['implant']
    assert scan['label'] == '植入成功'
    stored = (admin.app.state.store.pages / version / 'index.html').read_text(encoding='utf-8')
    again = admin.post('/api/upload/B?name=old.html', content=stored.encode())
    assert again.json()['implant']['label'] == '植入成功'
    fresh = admin.post('/api/upload/B?name=new.html', content=NEW.encode())
    assert fresh.json()['implant']['label'] == '已有植入代码'
    assert (admin.app.state.store.pages / fresh.json()['version']['id'] / 'index.html').read_text(encoding='utf-8') == NEW
    plain = admin.post('/api/upload/A?name=a.html', content=OLD.encode())
    assert plain.json()['implant'] is None
    assert 'sendEvent' not in (admin.app.state.store.pages / plain.json()['version']['id'] / 'index.html').read_text(encoding='utf-8')


def test_republish_writes_a_new_b_and_leaves_a_finished_page(tmp_path):
    from ablab.archives import write_bundle
    store_app = create_admin(tmp_path)
    store = store_app.state.store
    version = write_bundle([('index.html', OLD.encode())], 'old.html', store.pages, 'abc')
    store.add_version('B', version)
    store.publish('B', version['id'])
    report = republish_implant(store)
    assert report['published'] is True and report['label'] == '植入成功'
    assert report['version_id'] != version['id']
    live = (store.pages / store.slots()['B'] / 'index.html').read_text(encoding='utf-8')
    assert "sendEvent('whatsapp_click'" in live
    assert "googleSendTo: 'AW-11421360067/KEEP'" in live
    assert (store.pages / version['id'] / 'index.html').read_text(encoding='utf-8') == OLD
    second = republish_implant(store)
    assert second['published'] is False and second['label'] == '植入成功'
    ready = write_bundle([('index.html', NEW.encode())], 'new.html', store.pages, 'def')
    store.add_version('B', ready)
    store.publish('B', ready['id'])
    assert republish_implant(store)['published'] is False
    assert scan_implant(store.pages / ready['id'])['label'] == '已有植入代码'


def test_implanted_beacon_posts_the_click_name():
    updated, _ = implant_text(OLD, True)
    start = updated.index('function sendEvent')
    end = updated.index('function goWhatsApp')
    function = updated[start:end]
    program = function + '''
const CONFIG = { whatsappNumber: "85257989850", eventEndpoint: "https://hhucuq.top/wa-events" };
const WA_ENV = "android_webview";
const waAttempt = { linkType: "iframe", placement: "hero", txId: "tx-1" };
const CLICK_IDS = { placement: "https://www.youtube.com/@FinanceDaily", device: "mobile", gclid: "g1" };
const UA = "Mozilla/5.0 (Linux; Android 13; Pixel 7)";
const sent = [];
const navigator = { sendBeacon(url, blob) { sent.push({url, body: blob.text}); return true; } };
const Blob = function (parts) { this.text = parts[0]; };
sendEvent("whatsapp_click", {});
process.stdout.write(JSON.stringify(sent));
'''
    completed = subprocess.run(['node', '-e', program], check=True, capture_output=True, text=True)
    sent = json.loads(completed.stdout)
    body = json.loads(sent[0]['body'])
    assert sent[0]['url'] == 'https://hhucuq.top/wa-events'
    assert body['event'] == 'whatsapp_click'
    assert body['transaction_id'] == 'tx-1'
    assert body['wa_env'] == 'android_webview'
    assert body['placement_app'] == 'https://www.youtube.com/@FinanceDaily'
    assert body['number'] == '85257989850'
