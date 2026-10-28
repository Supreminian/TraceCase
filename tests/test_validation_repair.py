import json
from pathlib import Path
from uuid import uuid4

import pytest

from sdlc.providers import ModelResponse
from sdlc.service import ServiceError
from tests.helpers import scoped_service


class Provider:
    config = {'provider': 'test', 'model': 'repair-test'}

    def __init__(self, raw, finish='stop'):
        self.raw, self.finish, self.calls = raw, finish, []

    def generate(self, request):
        self.calls.append(request)
        return ModelResponse(self.raw, {**self.config, 'finish_reason': self.finish})


def setup_run(tmp_path, finish='stop'):
    service = scoped_service(tmp_path / 'repair.sqlite3')
    raw = Path('tests/fixtures/generation/normal.output.json').read_text()
    invalid = json.loads(raw)
    invalid['test_cases'][0]['requirement_ids'] = ['REQ-NOT-DEFINED']
    source = service.generate(text=Path('tests/fixtures/generation/normal.md').read_text(), operator='tester',
                              provider=Provider(json.dumps(invalid), finish))
    return service, service.get_run(source), raw


def test_repair_sends_exact_errors_preserves_original_and_requires_review(tmp_path):
    service, source, raw = setup_run(tmp_path)
    provider = Provider(raw)
    key = uuid4().hex
    child_id = service.repair_validation(source['id'], source['revision'], 'tester', provider, key)
    child = service.get_run(child_id)
    context = provider.calls[0]['prompt']['repair_context']
    assert context['previous_raw_output'] == source['raw_output']
    assert context['validation_errors'] == source['validation']['errors']
    assert context['source'] == {'run_id': source['id'], 'revision': source['revision']}
    assert child['parent_run_id'] == source['id']
    assert child['prompt']['version'] == 'VALIDATION-R1'
    assert child['status'] == 'valid'
    assert all(c['status'] == 'pending_review' for c in child['cases'])
    assert service.gate(child_id)
    unchanged = service.get_run(source['id'])
    for field in ('status', 'raw_output', 'output', 'validation', 'cases'):
        assert unchanged[field] == source[field]
    assert any(e['action'] == 'validation_repair_started' for e in unchanged['events'])
    assert service.repair_validation(source['id'], source['revision'], 'tester', provider, key) == child_id
    assert len(provider.calls) == 1


def test_unsuccessful_repair_stays_invalid_without_retry(tmp_path):
    service, source, _ = setup_run(tmp_path)
    provider = Provider(source['raw_output'])
    child = service.get_run(service.repair_validation(source['id'], source['revision'],
                                                       'tester', provider, uuid4().hex))
    assert child['status'] == 'invalid' and child['cases'] == []
    assert len(provider.calls) == 1


@pytest.mark.parametrize('finish', ['length', 'content_filter'])
def test_incomplete_or_filtered_run_is_not_repairable(tmp_path, finish):
    service, source, raw = setup_run(tmp_path, finish)
    provider = Provider(raw)
    with pytest.raises(ServiceError, match='完整返回'):
        service.repair_validation(source['id'], source['revision'], 'tester', provider, uuid4().hex)
    assert not provider.calls


def test_repair_rejects_stale_revision_and_cross_project(tmp_path):
    service, source, raw = setup_run(tmp_path)
    provider = Provider(raw)
    with pytest.raises(ServiceError):
        service.repair_validation(source['id'], source['revision'] + 1, 'tester', provider, uuid4().hex)
    other = service.create_project('另一个项目', '', 'tester')
    from sdlc.service import Service
    foreign = Service(service.db_path, project_id=other)
    with pytest.raises(ServiceError, match='不属于'):
        foreign.repair_validation(source['id'], source['revision'], 'tester', provider, uuid4().hex)
    assert not provider.calls


def test_ui_repair_requires_confirmation_and_navigates_to_child(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    service, source, raw = setup_run(tmp_path)
    provider = Provider(raw)
    monkeypatch.setenv('SDLC_DB_PATH', service.db_path)
    monkeypatch.setattr('sdlc.providers.GeminiProvider', lambda: provider)
    app = AppTest.from_file(str(Path('app.py').resolve())).run()
    app.selectbox(key='current_project').select(service.project_id).run()
    app.text_input(key='operator').input('tester').run()
    app.radio(key='page').set_value('运行详情与审核').run()
    assert not app.exception
    assert app.button(key='repair_validation').disabled
    assert any('结构或引用校验错误' in item.value for item in app.warning)
    app.checkbox(key=f"repair_confirm_{source['id']}").check().run()
    assert not app.button(key='repair_validation').disabled
    app.button(key='repair_validation').click().run()
    assert not app.exception
    child_id = app.session_state['selected_run']
    assert child_id != source['id']
    assert service.get_run(child_id)['status'] == 'valid'
    assert len(provider.calls) == 1


def legacy_source_run(tmp_path):
    import sqlite3
    service, source, raw = setup_run(tmp_path)
    output = json.loads(raw)
    output['test_cases'][0]['paragraph_ids'] = ['P003']
    original = json.dumps(output)
    validation = {'json_valid': True, 'valid': False,
                  'errors': ['test_cases.0.paragraph_ids: 未覆盖关联规则的来源段落'], 'output': output}
    with sqlite3.connect(service.db_path) as db:
        db.execute('UPDATE runs SET raw_output=?,validation_json=?,output_json=? WHERE id=?',
                   (original, json.dumps(validation), original, source['id']))
    return service, service.get_run(source['id'])


def test_local_revalidation_of_legacy_result_never_contacts_model(tmp_path):
    service, source = legacy_source_run(tmp_path)
    key = uuid4().hex
    child_id = service.revalidate_existing(source['id'], source['revision'], 'tester', key)
    child = service.get_run(child_id)
    assert child['status'] == 'valid'
    assert child['raw_output'] == source['raw_output']
    assert child['validation']['warnings']
    assert child['config']['real_model_call'] is False
    assert child['usage'] is None
    assert child['prompt']['version'] == 'LOCAL-RECHECK-R1'
    assert child['prompt']['source_model_config'] == source['config']
    assert all(c['status'] == 'pending_review' for c in child['cases'])
    assert service.gate(child_id)
    assert service.get_run(source['id'])['validation'] == source['validation']
    assert service.revalidate_existing(source['id'], source['revision'], 'tester', key) == child_id


def test_ui_can_revalidate_locally_without_key_or_model_confirmation(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    service, source = legacy_source_run(tmp_path)
    monkeypatch.setenv('SDLC_DB_PATH', service.db_path)
    for name in ('GEMINI_API_KEY', 'GOOGLE_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    app = AppTest.from_file(str(Path('app.py').resolve())).run()
    app.selectbox(key='current_project').select(service.project_id).run()
    app.text_input(key='operator').input('tester').run()
    app.radio(key='page').set_value('运行详情与审核').run()
    assert not app.button(key='local_revalidate').disabled
    app.button(key='local_revalidate').click().run()
    assert not app.exception
    child = service.get_run(app.session_state['selected_run'])
    assert child['status'] == 'valid' and child['parent_run_id'] == source['id']
    assert child['config']['real_model_call'] is False
