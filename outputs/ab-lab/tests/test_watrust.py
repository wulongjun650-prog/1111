import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ablab.store import Store
from ablab.watrust import CloudPhone, TrustChecker, VmosError, back_script, classify_screen, device_from_environ, open_script, v2_sign, v4_authorization
from ablab.web import create_admin

PHONE_A = '85200001111'
PHONE_B = '85200002222'
TRUST_SCREEN = '<node text="你信任此用户吗？" /><node text="继续聊天" /><node text="取消聊天" />'
CLEAR_SCREEN = '<node resource-id="com.whatsapp:id/entry" text="输入消息" />'
WEB_SCREEN = 'Chat on WhatsApp Open app Looks like you don\'t have WhatsApp installed.'


def test_v2_sign_matches_the_published_example():
    body = '{"padCode":"AC32010601132"}'
    signed = v2_sign('9cucpjoyn4xxmkhj3q9el3ce', '1747555200', '/vcpcloud/api/padApi/padInfo', body)
    assert signed == '483a4999d303307ef1b8b078b51e03fa0556547729c8a3c1470d2caf63e5f350'


def test_v4_authorization_matches_the_gateway_used_by_the_cloud_phone():
    body = '{"padCodes":["PAD1"],"scriptContent":"echo ok"}'
    header = v4_authorization('access', 'secret', '20260101T000000Z', body)
    assert header == 'HMAC-SHA256 Credential=access, SignedHeaders=content-type;host;x-content-sha256;x-date, Signature=46c470f35306d8b438dabba3353de50df648d98042c5e34fe8d664c814d3f9e0'


def test_screen_phrases_distinguish_trust_clear_and_unconfirmed():
    assert classify_screen(TRUST_SCREEN) == 'trust'
    assert classify_screen('<node text="继续聊天" /><node text="取消聊天" />') == 'trust'
    assert classify_screen('<node text="你信任此用戶嗎" />') == 'trust'
    assert classify_screen(CLEAR_SCREEN) == 'clear'
    assert classify_screen('<node text="輸入訊息" />') == 'clear'
    assert classify_screen(TRUST_SCREEN + CLEAR_SCREEN) == 'trust'
    assert classify_screen('<node text="继续聊天" />') == 'unconfirmed'
    assert classify_screen(WEB_SCREEN) == 'unconfirmed'
    assert classify_screen('') == 'unconfirmed'
    assert classify_screen(None) == 'unconfirmed'


def test_open_script_only_views_the_send_link_and_back_is_the_return_key():
    upload = 'https://hhucuq.top/api/wa-trust/screen/tokenvalue1234567890abcd'
    script = open_script(PHONE_A, upload, '1.2.3.4')
    assert f'phone={PHONE_A}' in script
    assert upload in script
    assert '--resolve' in script
    assert '1.2.3.4' in script
    assert 'am start -a android.intent.action.VIEW' in script
    assert '-p com.whatsapp' in script
    assert 'uiautomator dump /sdcard/ab-wa-trust.xml' in script
    assert ';' not in script
    assert '--resolve' not in open_script(PHONE_A, upload, '10.0.0.8')
    assert back_script() == 'input keyevent 4 && rm -f /sdcard/ab-wa-trust.xml'
    joined = script + back_script()
    for forbidden in ('input tap', 'input text', 'keyevent 66', 'contacts', '继续聊天', 'ACTION_SEND', 'content://'):
        assert forbidden not in joined


def test_back_is_pressed_even_when_the_screen_cannot_be_read():
    class Client:
        def __init__(self):
            self.scripts = []

        def submit(self, script):
            self.scripts.append(script)
            if script.startswith('am start'):
                raise VmosError('cloud phone command failed')
            return ''

    client = Client()
    with pytest.raises(VmosError):
        CloudPhone(client, wait_timeout=0.01).read(PHONE_A)
    assert client.scripts[1] == back_script()


def test_screen_text_is_posted_back_and_back_still_runs():
    calls = []
    holder = {}

    def transport(method, url, headers, body):
        calls.append((url, headers['authorization'], headers['x-date'], body))
        assert headers['authorization'] == v4_authorization('access', 'secret', headers['x-date'], body)
        assert url.endswith('/asyncCmd')
        if 'am start' in body:
            assert PHONE_A in body
            token = body.split('/api/wa-trust/screen/', 1)[1].split("'", 1)[0]
            holder['phone'].deliver(token, CLEAR_SCREEN)
        return {'code': 200, 'data': [{'taskId': 7, 'padCode': 'PAD1', 'vmStatus': 1}]}

    from ablab.watrust import VmosClient
    client = VmosClient('access', 'secret', 'PAD1', transport=transport, clock=lambda: 1767225600)
    phone = CloudPhone(client, origin='https://hhucuq.top', resolve_ip='', wait_timeout=1)
    holder['phone'] = phone
    assert phone.read(PHONE_A) == CLEAR_SCREEN
    assert [url.rsplit('/', 1)[-1] for url, *_ in calls] == ['asyncCmd', 'asyncCmd']
    assert 'input keyevent 4' in calls[1][3]


def test_a_posted_screen_can_arrive_without_a_login(console):
    client, app = console
    phone = CloudPhone(type('Client', (), {'submit': lambda self, script: 1})(), origin='http://127.0.0.1:8765', resolve_ip='', wait_timeout=1)
    token = 'tokenvalue1234567890abcdEF'
    phone._pending[token] = {'event': __import__('threading').Event(), 'text': None}
    app.state.trust_checker = TrustChecker(phone)
    posted = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 5001))
    response = posted.post(f'/api/wa-trust/screen/{token}', content=TRUST_SCREEN.encode())
    assert response.status_code == 200, response.text
    assert phone._pending[token]['text'] == TRUST_SCREEN
    assert posted.post('/api/wa-trust/screen/not-a-real-token-value-123456', content=b'no').status_code == 404


def test_missing_cloud_phone_credentials_do_not_invent_a_device():
    assert device_from_environ({}) is None
    assert device_from_environ({'AB_VMOS_ACCESS_KEY': 'access', 'AB_VMOS_SECRET_KEY': 'secret'}) is None
    device = device_from_environ({'AB_VMOS_ACCESS_KEY': 'access', 'AB_VMOS_SECRET_KEY': 'secret', 'AB_VMOS_PAD_CODE': 'PAD1'})
    assert callable(device)


def test_old_number_table_keeps_rows_and_starts_unchecked(tmp_path):
    database = sqlite3.connect(tmp_path / 'app.db')
    database.execute('CREATE TABLE whatsapp_numbers(id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL UNIQUE, note TEXT NOT NULL, created REAL NOT NULL)')
    database.execute('INSERT INTO whatsapp_numbers(phone, note, created) VALUES(?,?,?)', (PHONE_A, '', 1))
    database.commit()
    database.close()
    row = Store(tmp_path).whatsapp_numbers()[0]
    assert row['phone'] == PHONE_A
    assert row['trust'] == {'status': 'unchecked', 'detail': '', 'checked_at': None}
    assert 'trust_result' not in row


@pytest.fixture
def console(tmp_path):
    app = create_admin(tmp_path)
    client = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 5000))
    client.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': app.state.csrf})
    return client, app


def add_numbers(client, phones):
    added = client.post('/api/b-redirects/numbers', json={'phones': phones, 'note': ''})
    assert added.status_code == 200, added.text
    return added.json()['numbers']


def test_checks_are_stored_apart_from_visits_and_poll_waits_for_both_screens(console):
    client, app = console
    screens = {PHONE_A: TRUST_SCREEN, PHONE_B: CLEAR_SCREEN}
    app.state.trust_checker = TrustChecker(lambda phone: screens[phone])
    first, second = add_numbers(client, [PHONE_A, PHONE_B])
    assert first['trust']['status'] == 'unchecked'
    assert client.get('/api/b-redirects').json()['trust_poll'] is False
    trust = client.post(f"/api/b-redirects/numbers/{first['id']}/trust")
    assert trust.status_code == 200, trust.text
    assert trust.json()['busy'] is False
    assert trust.json()['poll_enabled'] is False
    assert trust.json()['number']['trust']['status'] == 'trust'
    assert trust.json()['number']['trust']['detail'] == '出现信任弹窗'
    clear = client.post(f"/api/b-redirects/numbers/{second['id']}/trust")
    assert clear.json()['number']['trust'] == {'status': 'clear', 'detail': '正常', 'checked_at': clear.json()['number']['trust']['checked_at']}
    assert clear.json()['poll_enabled'] is True
    assert client.get('/api/b-redirects').json()['trust_poll'] is True
    with app.state.store.connect() as db:
        assert db.execute('SELECT COUNT(*) AS n FROM events').fetchone()['n'] == 0
    audited = sorted((item for item in client.get('/api/audit').json()['items'] if item['action'] == 'whatsapp_trust_checked'), key=lambda item: item['id'])
    assert [json.loads(item['detail'])['status'] for item in audited] == ['trust', 'clear']
    assert PHONE_A not in json.dumps(audited, ensure_ascii=False)
    assert PHONE_B not in json.dumps(audited, ensure_ascii=False)


def test_two_matching_screens_do_not_enable_polling(console):
    client, app = console
    app.state.trust_checker = TrustChecker(lambda phone: TRUST_SCREEN)
    numbers = add_numbers(client, [PHONE_A, PHONE_B])
    for number in numbers:
        result = client.post(f"/api/b-redirects/numbers/{number['id']}/trust")
        assert result.json()['number']['trust']['status'] == 'trust'
        assert result.json()['poll_enabled'] is False


def test_unread_screen_is_unconfirmed_and_does_not_alarm_the_gate(console):
    client, app = console
    app.state.trust_checker = TrustChecker(lambda phone: WEB_SCREEN)
    number = add_numbers(client, [PHONE_A])[0]
    result = client.post(f"/api/b-redirects/numbers/{number['id']}/trust")
    assert result.json()['number']['trust']['status'] == 'unconfirmed'
    assert result.json()['number']['trust']['detail'] == '这次没看清'
    assert result.json()['poll_enabled'] is False
    app.state.trust_checker = TrustChecker(None)
    again = client.post(f"/api/b-redirects/numbers/{number['id']}/trust")
    assert again.json()['number']['trust']['detail'] == '云手机还没接上'


def test_a_busy_device_returns_the_saved_result_without_opening_another_link(console):
    client, app = console
    calls = []
    app.state.trust_checker = TrustChecker(lambda phone: calls.append(phone) or CLEAR_SCREEN)
    number = add_numbers(client, [PHONE_A])[0]
    assert app.state.trust_checker._lock.acquire(blocking=False)
    busy = client.post(f"/api/b-redirects/numbers/{number['id']}/trust")
    assert busy.json()['busy'] is True
    assert busy.json()['number']['trust']['status'] == 'unchecked'
    assert calls == []
    app.state.trust_checker._lock.release()
    done = client.post(f"/api/b-redirects/numbers/{number['id']}/trust")
    assert done.json()['busy'] is False
    assert calls == [PHONE_A]
