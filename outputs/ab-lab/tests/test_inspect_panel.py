import json

import httpx

from ablab.provisioning import PanelPreflight
from deploy import inspect_panel


def test_local_command_checks_without_prompt_or_config_change(tmp_path, monkeypatch, capsys):
    config = tmp_path / 'api.json'
    config.write_text(json.dumps({'open': True, 'token': 'a' * 32, 'limit_addr': ['127.0.0.1']}))
    original = PanelPreflight.from_local_config
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={
        'status': 0, 'message': [{'version': '00', 'name': 'Static'}],
    }))
    # Only relocate the installed panel file and external HTTP dependency.
    monkeypatch.setattr(PanelPreflight, 'from_local_config', lambda address: original(address, config, transport))

    def unexpected_prompt(*args):
        raise AssertionError('local command must not prompt for credentials')

    monkeypatch.setattr('builtins.input', unexpected_prompt)
    monkeypatch.setattr(inspect_panel.getpass, 'getpass', unexpected_prompt)
    before = config.read_bytes()
    assert inspect_panel.main(['--local-config', '--address', 'http://127.0.0.1:34734']) == 0
    output = capsys.readouterr().out
    assert '只读' in output and '自动写入保持禁用' in output
    assert 'a' * 32 not in output
    assert config.read_bytes() == before


def test_local_command_reports_safe_error_for_remote_destination(capsys):
    assert inspect_panel.main(['--local-config', '--address', 'https://panel.example.com']) == 1
    output = capsys.readouterr().out
    assert '127.0.0.1' in output
    assert 'Traceback' not in output
