from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from ablab.desk import DeskError, SheetWriter, buyer_quote, decision_for, lead_report, parse_ticket, review_tickets, should_switch, spreadsheet_id, ticket_code
from ablab.web import create_admin


PHONE = '85200001111'
OTHER = '85200002222'
TICKET = 'https://admin.haiwangweb.com/web#/accountshow/sampleTicket'
SHEET = 'https://docs.google.com/spreadsheets/d/sheet-id-example/edit?gid=0#gid=0'


def test_ticket_and_sheet_links():
    assert ticket_code(TICKET) == 'sampleTicket'
    assert ticket_code('https://admin.haiwangweb.com/web#/accountshow/v+glTuDTY') == 'v+glTuDTY'
    assert spreadsheet_id(SHEET) == 'sheet-id-example'
    with pytest.raises(DeskError):
        ticket_code('https://example.test/nope')


def test_online_over_three_or_offline_switches():
    assert should_switch(3, False) == ''
    assert should_switch(4, False) == 'online'
    assert should_switch(0, True) == 'offline'
    crowded = parse_ticket({'online': 10, 'offline_apps': 2, 'name': '鳄鱼-梵高', 'accounts': [
        {'phone': PHONE, 'online_count': 1, 'leads': 2},
        {'phone': OTHER, 'offline': True, 'leads': 1},
    ]}, TICKET)
    assert decision_for(crowded, PHONE) == 'online'
    assert decision_for(crowded, OTHER) == 'offline'
    assert decision_for(crowded, '85200003333') == ''
    quiet = parse_ticket({'online': 2, 'accounts': [{'phone': PHONE, 'online_count': 1, 'leads': 0}]}, TICKET)
    assert decision_for(quiet, PHONE) == ''
    empty = parse_ticket({'online': 0, 'offline_apps': 4}, TICKET)
    assert empty['offline'] is True
    assert decision_for(empty, PHONE) == 'offline'


def test_haiwang_list_uses_share_statistics_not_online_num():
    live = '85200001111'
    offline_phone = '85200002222'
    quiet_phone = '85200003333'
    payload = {
        'code': 1,
        'msg': 'success',
        'data': {
            'online_num': 0,
            'remark': '鳄鱼-梵高',
            'total': 3,
            'items': [
                {'acclist_account': live, 'acclist_status': 2, 'account_statistics_today_effective': 2, 'account_statistics_today_total': 2},
                {'acclist_account': offline_phone, 'acclist_status': 0, 'account_statistics_today_effective': 3, 'account_statistics_today_total': 3},
                {'acclist_account': quiet_phone, 'acclist_status': 2, 'account_statistics_today_effective': 0, 'account_statistics_today_total': 0},
            ],
            'shareStatistics': {
                'sharecode_statistics_online_account': 4,
                'sharecode_statistics_total_account': 6,
                'sharecode_statistics_applist': [{'account_total': 6, 'account_total_online': 4, 'today_effective': 5}],
            },
        },
    }
    ticket = parse_ticket(payload, TICKET)
    assert ticket['name'] == '鳄鱼-梵高'
    assert ticket['online'] == 4
    assert ticket['offline_apps'] == 2
    assert ticket['accounts'][0]['offline'] is False
    assert ticket['accounts'][1] == {'phone': offline_phone, 'offline': True, 'online': 0, 'leads': Decimal('3')}
    assert decision_for(ticket, live) == 'online'
    assert decision_for(ticket, offline_phone) == 'offline'
    assert decision_for(ticket, '85200004444') == ''
    assert lead_report([ticket])['total'] == '5'
    named = parse_ticket(payload, TICKET, '手填名称')
    assert named['name'] == '手填名称'
    partial = {'code': 1, 'data': {'total': 2, 'items': payload['data']['items'][:1], 'shareStatistics': payload['data']['shareStatistics']}}
    with pytest.raises(DeskError, match='没有读全'):
        parse_ticket(partial, TICKET)
    with pytest.raises(DeskError, match='无法读取'):
        parse_ticket({'code': 0, 'msg': 'no', 'data': {}}, TICKET)


def test_lead_detail_names_the_work_order_and_number():
    first = parse_ticket({'online': 1, 'name': '鳄鱼-梵高', 'accounts': [
        {'phone': PHONE, 'leads': 2}, {'phone': OTHER, 'leads': '0.5'},
    ]}, TICKET)
    second = parse_ticket({'online': 1, 'accounts': [{'phone': PHONE, 'leads': 1}]}, 'https://admin.haiwangweb.com/web#/accountshow/secondTicket', '另一工单')
    report = lead_report([first, second])
    assert report['total'] == '3.5'
    assert report['rows'] == [
        {'name': '鳄鱼-梵高', 'code': 'sampleTicket', 'phone': PHONE, 'leads': '2'},
        {'name': '鳄鱼-梵高', 'code': 'sampleTicket', 'phone': OTHER, 'leads': '0.5'},
        {'name': '另一工单', 'code': 'secondTicket', 'phone': PHONE, 'leads': '1'},
    ]


def test_buyer_quote_applies_fee_then_divides_by_leads():
    quote = buyer_quote({
        'date': '10/06', 'project': 'HK项目', 'buyer': 'AJ', 'phone': '187-027-3339',
        'recharge': 0, 'balance': '1447.93', 'spend': '28.95', 'leads': '0.8',
    })
    assert quote['adjusted'] == '33.29'
    assert quote['cost'] == '41.61'
    assert quote['text'] == '\n'.join([
        '10/06',
        'HK项目',
        'AJ',
        '187-027-3339 充值0     余额：1,447.93',
        '消耗：33.29',
        '进线：0.8',
        '成本：41.61',
    ])
    assert quote['row'] == ['10/06', 'AJ', '28.95', '33.29', '0.8', '41.61']
    blank = buyer_quote({
        'date': '10/06', 'project': 'HK项目', 'buyer': 'AJ', 'phone': PHONE,
        'recharge': 0, 'balance': 0, 'spend': 10, 'leads': 0,
    })
    assert blank['cost'] == ''
    assert blank['text'].endswith('成本：—')


def test_review_hides_the_password_and_sheet_writer_sends_one_row():
    seen = {}

    def reader(url, password, name=''):
        seen['password'] = password
        return {'online': 4, 'name': name, 'accounts': [{'phone': PHONE, 'leads': 2}]}

    report = review_tickets([{'url': TICKET, 'password': 'secret', 'name': '鳄鱼-梵高'}], PHONE, reader)
    assert seen['password'] == 'secret'
    assert 'secret' not in str(report)
    assert report['switch'] == 'online'
    assert report['total'] == '2'
    calls = []

    def transport(url, headers, body):
        calls.append((url, headers, body))
        return 200, {'updates': {'updatedRows': 1}}

    written = SheetWriter(token='test-token', transport=transport).append(SHEET, report and buyer_quote({
        'date': '10/06', 'project': 'HK项目', 'buyer': 'AJ', 'phone': PHONE,
        'recharge': 0, 'balance': 1, 'spend': 10, 'leads': 2,
    })['row'])
    assert written['ok'] is True
    assert calls[0][0].startswith('https://sheets.googleapis.com/v4/spreadsheets/sheet-id-example/values/A:F:append')
    assert calls[0][1]['Authorization'] == 'Bearer test-token'
    assert calls[0][2]['values'] == [['10/06', 'AJ', '10.00', '11.50', '2', '5.75']]
    with pytest.raises(DeskError, match='还没接上'):
        SheetWriter().append(SHEET, calls[0][2]['values'][0])


def test_desk_routes_review_quote_and_sheet(tmp_path):
    app = create_admin(tmp_path)
    client = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 5000))
    client.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': app.state.csrf})

    def reader(url, password, name=''):
        assert password == 'secret'
        return {'online': 10, 'offline_apps': 1, 'accounts': [
            {'phone': PHONE, 'online_count': 1, 'leads': 3},
            {'phone': OTHER, 'leads': 1},
        ]}

    app.state.desk_reader = reader
    review = client.post('/api/desk/review', json={'phone': PHONE, 'tickets': [{'url': TICKET, 'password': 'secret', 'name': '鳄鱼-梵高'}]})
    assert review.status_code == 200, review.text
    body = review.json()
    assert body['switch'] == 'online'
    assert body['total'] == '4'
    assert 'secret' not in review.text
    assert [item['phone'] for item in body['rows']] == [PHONE, OTHER]
    quote = client.post('/api/desk/quote', json={
        'date': '10/06', 'project': 'HK项目', 'buyer': 'AJ', 'phone': PHONE,
        'recharge': 0, 'balance': '1447.93', 'spend': '28.95', 'leads': '0.8',
    })
    assert quote.status_code == 200
    assert quote.json()['adjusted'] == '33.29'
    missing = client.post('/api/desk/sheet', json={
        'spreadsheet': SHEET, 'date': '10/06', 'project': 'HK项目', 'buyer': 'AJ', 'phone': PHONE,
        'recharge': 0, 'balance': '1447.93', 'spend': '28.95', 'leads': '0.8',
    })
    assert missing.status_code == 400 and '还没接上' in missing.text
    saved = []
    app.state.sheet_writer = SheetWriter(token='test-token', transport=lambda url, headers, body: saved.append(body) or (200, {'updates': {'updatedRows': 1}}))
    written = client.post('/api/desk/sheet', json={
        'spreadsheet': SHEET, 'date': '10/06', 'project': 'HK项目', 'buyer': 'AJ', 'phone': PHONE,
        'recharge': 0, 'balance': '1447.93', 'spend': '28.95', 'leads': '0.8',
    })
    assert written.status_code == 200, written.text
    assert saved[0]['values'][0][3:6] == ['33.29', '0.8', '41.61']
