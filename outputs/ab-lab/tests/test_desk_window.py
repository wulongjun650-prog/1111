import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from ablab.desk import DeskError, review_tickets
from ablab.desk_window import WorkOrderWindow, readable_list


PHONE = '85200001111'
OTHER = '85200002222'
PASSWORD = 'view-pass'
PAYLOAD = {
    'code': 1,
    'msg': 'success',
    'data': {
        'remark': '鳄鱼-梵高',
        'total': 2,
        'online_num': 0,
        'items': [
            {'acclist_account': PHONE, 'acclist_status': 2, 'account_statistics_today_effective': 2},
            {'acclist_account': OTHER, 'acclist_status': 0, 'account_statistics_today_effective': 1},
        ],
        'shareStatistics': {
            'sharecode_statistics_online_account': 4,
            'sharecode_statistics_total_account': 2,
        },
    },
}
PAGE = """<!doctype html><meta charset="utf-8"><title>工单</title>
<label>查看密码 <input id="pw" type="password" placeholder="密码"></label>
<button id="go" type="button">确定</button>
<script>
document.getElementById('go').onclick = async () => {
  const password = document.getElementById('pw').value;
  await fetch('/webApi/accountshow/list?sharecode=sampleTicket&password=' + encodeURIComponent(password));
};
</script>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/web':
            body = PAGE.encode()
            self._send(200, 'text/html; charset=utf-8', body)
            return
        if path == '/webApi/accountshow/list':
            self.server.seen.append(parse_qs(urlsplit(self.path).query).get('password', [''])[0])
            self._send(200, 'application/json', json.dumps(PAYLOAD).encode())
            return
        self._send(404, 'text/plain', b'missing')

    def _send(self, status, content_type, body):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def _server():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.seen = []
    thread_started = __import__('threading').Thread(target=server.serve_forever, daemon=True)
    thread_started.start()
    return server


def test_hardened_service_turns_off_the_chrome_sandbox():
    from ablab.desk_window import sandbox_off

    assert sandbox_off(0, '') is True
    assert sandbox_off(999, 'Name:\tpython\nNoNewPrivs:\t1\n') is True
    assert sandbox_off(999, 'NoNewPrivs:\t0\n') is False


def test_a_rejected_list_is_not_treated_as_the_account_list():
    assert readable_list({'code': 0, 'msg': 'password', 'data': {}}) is False
    assert readable_list({'code': 1, 'data': {'items': [], 'shareStatistics': {}}}) is True


def test_missing_browser_reports_that_the_window_cannot_open(tmp_path):
    window = WorkOrderWindow(tmp_path / 'profile', executable='/bin/false', headless=True, timeout=5)
    try:
        with pytest.raises(DeskError, match='打不开'):
            window('https://admin.haiwangweb.com/web#/accountshow/sampleTicket', PASSWORD)
    finally:
        window.close()


def test_window_reads_the_list_the_page_requests(tmp_path):
    server = _server()
    host, port = server.server_address
    url = f'http://{host}:{port}/web#/accountshow/sampleTicket'
    window = WorkOrderWindow(tmp_path / 'profile', headless=True, timeout=20)
    try:
        report = review_tickets([{'url': url, 'password': PASSWORD, 'name': ''}], PHONE, window)
        assert server.seen == [PASSWORD]
        assert PASSWORD not in json.dumps(report)
        assert report['switch'] == 'online'
        assert report['total'] == '3'
        assert report['rows'][0]['name'] == '鳄鱼-梵高'
        assert [row['phone'] for row in report['rows']] == [PHONE, OTHER]
        again = window(url, PASSWORD)
        assert again['data']['remark'] == '鳄鱼-梵高'
        assert server.seen == [PASSWORD, PASSWORD]
    finally:
        window.close()
        server.shutdown()
