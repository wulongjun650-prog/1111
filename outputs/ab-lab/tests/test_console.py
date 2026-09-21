from fastapi.testclient import TestClient
from ablab.web import create_admin


def test_console_loads_module_and_styles_with_strict_headers(tmp_path):
    client = TestClient(create_admin(tmp_path), base_url='http://127.0.0.1:8765', client=('127.0.0.1', 9999))
    response = client.get('/')
    assert response.status_code == 200
    assert 'AB Lab' in response.text
    assert 'frame-ancestors \'none\'' in response.headers['content-security-policy']
    for path, mime in [('/static/app.js', 'javascript'), ('/static/helpers.mjs', 'javascript'), ('/static/app.css', 'text/css')]:
        response = client.get(path)
        assert response.status_code == 200
        assert mime in response.headers['content-type']
