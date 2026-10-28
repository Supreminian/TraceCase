"""Real product flow with an isolated network boundary, including bounded repair."""
import json
from pathlib import Path

import pytest

from sdlc.domain import prepare_input, validate_output
from sdlc.providers import ModelResponse
from tests.helpers import scoped_service


class Model:
    config = {'provider': 'test-model', 'model': 'network-mock', 'real_model_call': True,
              'endpoint': 'https://unit-test.invalid/'}

    def __init__(self, responses, finish='stop'):
        self.responses, self.finish, self.calls = responses, finish, []

    def generate(self, request):
        self.calls.append(request)
        return ModelResponse(self.responses[min(len(self.calls) - 1, len(self.responses) - 1)],
                             {**self.config, 'finish_reason': self.finish})


def data():
    root = Path('tests/fixtures/generation')
    return (root / 'normal.md').read_text(), (root / 'normal.output.json').read_text()


@pytest.mark.parametrize('broken', ['{"unfinished":', '{"schema_version":"wrong"}'])
def test_one_click_automatically_repairs_once_and_retains_both_attempts(tmp_path, broken):
    service = scoped_service(tmp_path / 'live.sqlite3')
    text, raw = data()
    provider = Model([broken, raw])
    child_id = service.generate_from_prd(text=text, operator='tester', provider=provider, request_id='one-click')
    child = service.get_run(child_id)
    parent = service.get_run(child['parent_run_id'])
    assert child['status'] == 'valid' and parent['status'] == 'invalid'
    assert parent['raw_output'] == broken and child['raw_output'] == raw
    assert len(provider.calls) == 2
    assert provider.calls[1]['prompt']['repair_context']['validation_errors'] == parent['validation']['errors']
    assert all(c['status'] == 'pending_review' for c in child['cases'])
    assert service.gate(child_id)
    assert service.generate_from_prd(text=text, operator='tester', provider=provider, request_id='one-click') == child_id
    assert len(provider.calls) == 2


def test_repair_is_bounded_and_truncation_does_not_trigger_same_budget_retry(tmp_path):
    service = scoped_service(tmp_path / 'live.sqlite3')
    text, _ = data()
    provider = Model(['{"unfinished":'])
    run_id = service.generate_from_prd(text=text, operator='tester', provider=provider)
    assert service.get_run(run_id)['status'] == 'invalid' and len(provider.calls) == 2
    limited = Model(['{"unfinished":'], finish='length')
    service.generate_from_prd(text=text, operator='tester', provider=limited)
    assert len(limited.calls) == 1


def test_real_categories_and_local_uncertainty_do_not_invalidate_entire_output():
    text, raw = data()
    output = json.loads(raw)
    case = output['test_cases'][0]
    case['category'] = 'security'
    rule = output['business_rules'][0]
    rule['status'] = 'ambiguous'
    output['clarifications'] = [{
        'id': 'Q-LOCAL', 'question': '另一种设备的表现是否相同？',
        'paragraph_ids': rule['paragraph_ids'], 'requirement_ids': [rule['id']],
        'affected_case_ids': [case['case_id']], 'scope': 'case', 'blocking': False,
        'rationale': '不影响当前已明确环境的执行与判断。'}]
    checked = validate_output(json.dumps(output), prepare_input(text=text)['paragraphs'])
    assert checked['valid'], checked['errors']
    assert checked['output']['test_cases'][0]['category'] == 'security'
    assert checked['output']['test_cases'][0]['readiness'] == 'ready'
    output['clarifications'] = []
    checked = validate_output(json.dumps(output), prepare_input(text=text)['paragraphs'])
    assert checked['valid'], checked['errors']
    assert checked['output']['test_cases'][0]['readiness'] == 'needs_clarification'


def test_long_arbitrary_prd_is_sent_in_full_without_fixture_matching(tmp_path):
    text = '# 客户端需求\n\n' + '用户可以查看虚构任务。' * 1500
    document = prepare_input(text=text)
    pid = document['paragraphs'][-1]['id']
    output = {'schema_version': '1.0', 'features': [], 'roles': [], 'clarifications': [],
              'business_rules': [{'id': 'R-VIEW', 'statement': '用户可以查看任务。',
                                  'status': 'clear', 'paragraph_ids': [pid]}],
              'test_cases': [{'case_id': 'TC-VIEW', 'title': '查看任务', 'requirement_ids': ['R-VIEW'],
                              'paragraph_ids': [pid], 'preconditions': ['存在虚构任务'],
                              'steps': [{'number': 1, 'action': '查看任务'}],
                              'expected_results': ['显示任务'], 'priority': 'P1', 'category': 'functional',
                              'readiness': 'ready', 'clarification_ids': []}]}
    provider = Model([json.dumps(output)])
    service = scoped_service(tmp_path / 'long.sqlite3')
    run = service.get_run(service.generate_from_prd(text=text, operator='tester', provider=provider))
    assert run['status'] == 'valid'
    assert len(text) > 15000
    assert provider.calls[0]['normalized_text'] == text
    assert provider.calls[0]['prompt']['version'] == 'GENERAL-R1'


def test_ui_only_exposes_prd_input_and_hides_diagnostic_json(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    service = scoped_service(tmp_path / 'ui.sqlite3')
    text, raw = data()
    output = json.loads(raw)
    output['test_cases'][0]['category'] = 'accessibility'
    provider = Model(['{"unfinished":', json.dumps(output)])
    monkeypatch.setattr('sdlc.providers.GeminiProvider', lambda: provider)
    monkeypatch.setenv('SDLC_DB_PATH', service.db_path)
    app = AppTest.from_file(str(Path('app.py').resolve()), default_timeout=20).run()
    app.selectbox(key='current_project').select(service.project_id).run()
    app.radio(key='page').set_value('新建运行').run()
    assert not any(x.key in {'generation_mode', 'prompt_version', 'sample'} for x in [*app.radio, *app.selectbox])
    assert app.radio(key='input_source').options == ['粘贴 Markdown', '上传 Markdown']
    app.text_area(key='prd_text').input(text).run()
    app.button(key='generate').click().run()
    assert not app.exception
    run = service.get_run(app.session_state['selected_run'])
    assert run['status'] == 'valid' and len(provider.calls) == 2
    assert not any(e.proto.expanded for e in app.expander if 'JSON' in e.label or '原始输出' in e.label)
    assert any(e.label == '查看业务角色 JSON' for e in app.expander)
