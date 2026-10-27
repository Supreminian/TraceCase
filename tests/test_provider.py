import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sdlc.providers import CompatibleProvider
from tests.helpers import scoped_service as Service


@contextmanager
def server(status=200, body=None):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.headers.get('Authorization'), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{http.server_port}/v1/chat/completions', requests
    finally:
        http.shutdown()
        http.server_close()
        worker.join()


def configure(monkeypatch, url):
    monkeypatch.setenv('SDLC_MODEL_URL', url)
    monkeypatch.setenv('SDLC_MODEL_NAME', 'test-model')
    monkeypatch.setenv('SDLC_MODEL_API_KEY', 'test-secret')


def test_real_adapter_to_local_mock_and_review_gate(tmp_path, monkeypatch):
    raw = Path('samples/normal.output.json').read_text()
    with server(body={'model': 'test-revision', 'choices': [{'message': {'content': raw}, 'finish_reason': 'stop'}],
                      'usage': {'prompt_tokens': 123, 'completion_tokens': 456}}) as (url, calls):
        configure(monkeypatch, url)
        service = Service(tmp_path / 'run.sqlite3')
        text = Path('samples/normal.md').read_text().replace('完整演示样例', '自定义测试输入')
        run_id = service.generate(text=text, operator='tester', provider=CompatibleProvider())
        run = service.get_run(run_id)
        assert run['status'] == 'valid'
        assert run['raw_output'] == raw
        assert run['usage'] == {'input_tokens': 123, 'output_tokens': 456}
        assert run['config']['endpoint'] == url
        assert run['config']['model_revision'] == 'test-revision'
        assert 'test-secret' not in str(run)
        assert service.gate(run_id)
        assert len(calls) == 1
        assert calls[0][0] == 'Bearer test-secret'
        assert calls[0][1]['max_tokens'] == 4096
        assert '自定义测试输入' in calls[0][1]['messages'][0]['content']


@pytest.mark.parametrize('status,body,expected', [
    (401, {'error': 'test-secret'}, 'HTTP 401'),
    (429, {'error': 'limit'}, 'HTTP 429'),
    (200, {'choices': []}, '不是兼容'),
    (200, {'choices': [{'message': {'content': None}}]}, '未返回文本'),
])
def test_adapter_failure_is_saved_without_secret(tmp_path, monkeypatch, status, body, expected):
    with server(status, body) as (url, calls):
        configure(monkeypatch, url)
        service = Service(tmp_path / 'run.sqlite3')
        run_id = service.generate(text='自定义 PRD', operator='tester', provider=CompatibleProvider())
        run = service.get_run(run_id)
        assert run['status'] == 'failed'
        assert expected in run['error']
        assert 'test-secret' not in str(run)
        assert len(calls) == 1


@pytest.mark.parametrize('url', ['http://example.com/v1/chat/completions',
                                'https://user:secret@example.com/v1', 'https://example.com/v1?key=secret', ''])
def test_unsafe_or_missing_endpoint(monkeypatch, url):
    configure(monkeypatch, url)
    with pytest.raises(ValueError):
        CompatibleProvider()


def test_timeout_configuration_rejected(monkeypatch):
    configure(monkeypatch, 'http://localhost:1234/v1/chat/completions')
    monkeypatch.setenv('SDLC_MODEL_TIMEOUT', '0')
    with pytest.raises(ValueError):
        CompatibleProvider()


def test_timeout_does_not_retry(tmp_path, monkeypatch):
    configure(monkeypatch, 'http://localhost:1234/v1/chat/completions')
    class Opener:
        calls = 0
        def open(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError('test-secret')
    opener = Opener()
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: opener)
    service = Service(tmp_path / 'run.sqlite3')
    run_id = service.generate(text='PRD', operator='tester', provider=CompatibleProvider())
    run = service.get_run(run_id)
    assert run['status'] == 'failed' and '超时' in run['error']
    assert opener.calls == 1 and 'test-secret' not in str(run)


def test_ui_disables_unconfigured_model(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv('SDLC_DB_PATH', str(tmp_path / 'ui.sqlite3'))
    monkeypatch.delenv('SDLC_MODEL_URL', raising=False)
    service = Service(tmp_path / 'ui.sqlite3')
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py')).run()
    app.selectbox(key='current_project').select(service.project_id).run()
    app.radio(key='page').set_value('新建运行').run()
    app.text_input(key='operator').input('tester').run()
    app.radio(key='generation_mode').set_value('模型生成').run()
    assert not app.exception
    assert app.button(key='generate').disabled
    assert app.warning


def test_truncated_response_cannot_enter_review(tmp_path, monkeypatch):
    raw = Path('samples/normal.output.json').read_text()
    with server(body={'choices': [{'message': {'content': raw}, 'finish_reason': 'length'}]}) as (url, _):
        configure(monkeypatch, url)
        service = Service(tmp_path / 'run.sqlite3')
        run_id = service.generate(text=Path('samples/normal.md').read_text(), operator='tester', provider=CompatibleProvider())
        run = service.get_run(run_id)
        assert run['status'] == 'invalid'
        assert run['raw_output'] == raw
        assert run['cases'] == [] and service.gate(run_id)
