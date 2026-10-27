import copy
import json
from uuid import uuid4

import pytest

from sdlc.providers import FixtureProvider, ModelResponse, list_samples
from sdlc.service import ServiceError
from tests.helpers import scoped_service as Service


class RevisionProvider:
    config = {'provider': 'test', 'model': 'review-test', 'endpoint': 'https://test.invalid/'}

    def __init__(self, mutation=None, fail=False):
        self.calls = []
        self.mutation, self.fail = mutation, fail

    def generate(self, request):
        self.calls.append(request)
        if self.fail:
            raise TimeoutError('test-only')
        context = request['prompt']['review_context']
        output = copy.deepcopy(context['current_output'])
        ids = {f['case_id'] for f in context['feedback']}
        for case in output['test_cases']:
            if case['case_id'] in ids:
                case['title'] += '（根据退回意见修订）'
        if self.mutation:
            self.mutation(output, ids)
        return ModelResponse(json.dumps(output, ensure_ascii=False), self.config)


@pytest.fixture
def reviewed(tmp_path):
    service = Service(tmp_path / 'test.sqlite3')
    text = next(s['text'] for s in list_samples() if s['id'] == 'normal')
    run_id = service.generate(text=text, operator='tester')
    cases = service.get_run(run_id)['cases']
    for index, case in enumerate(cases):
        service.review(run_id, case['case_id'], case['version'], case['revision'],
                       'return' if index == 0 else 'approve', 'tester',
                       '步骤需明确执行角色' if index == 0 else '符合原文', uuid4().hex)
    return service, service.get_run(run_id)


def test_revision_snapshots_feedback_preserves_approvals_content_and_restarts_review(reviewed):
    service, source = reviewed
    provider = RevisionProvider()
    request_id = uuid4().hex
    child_id = service.revise_returned(source['id'], source['revision'], 'tester', provider, request_id)
    child = service.get_run(child_id)
    assert child['status'] == 'valid'
    assert child['parent_run_id'] == source['id']
    assert child['prompt']['version'] == 'REVIEW-R1'
    context = child['prompt']['review_context']
    assert context['source'] == {'run_id': source['id'], 'revision': source['revision']}
    assert len(context['feedback']) == 1
    assert context['feedback'][0]['reason'] == '步骤需明确执行角色'
    assert all(c['status'] == 'pending_review' and c['version'] == 1 for c in child['cases'])
    assert child['cases'][0]['content']['title'] != source['cases'][0]['content']['title']
    assert [c['content'] for c in child['cases'][1:]] == [c['content'] for c in source['cases'][1:]]
    assert [c['status'] for c in service.get_run(source['id'])['cases']] == [c['status'] for c in source['cases']]
    assert service.gate(child_id)
    assert service.revise_returned(source['id'], source['revision'], 'tester', provider, request_id) == child_id
    assert len(provider.calls) == 1
    with pytest.raises(ServiceError, match='变化'):
        service.revise_returned(source['id'], source['revision'], 'tester', provider, uuid4().hex)


def change_approved(output, ids):
    next(c for c in output['test_cases'] if c['case_id'] not in ids)['title'] += 'changed'


def change_rule(output, ids):
    output['business_rules'][0]['statement'] += 'invented'


def change_case_id(output, ids):
    output['test_cases'][0]['case_id'] = 'REPLACED-ID'


@pytest.mark.parametrize('mutation', [change_approved, change_rule, change_case_id])
def test_revision_cannot_change_approved_case_or_requirement(reviewed, mutation):
    service, source = reviewed
    child_id = service.revise_returned(source['id'], source['revision'], 'tester', RevisionProvider(mutation), uuid4().hex)
    child = service.get_run(child_id)
    assert child['status'] == 'invalid'
    assert any('修订' in e for e in child['validation']['errors'])
    assert child['raw_output'] and not child['cases']
    assert service.gate(child_id)


def test_failed_iteration_retains_parent_and_failure(reviewed):
    service, source = reviewed
    child_id = service.revise_returned(source['id'], source['revision'], 'tester', RevisionProvider(fail=True), uuid4().hex)
    assert service.get_run(child_id)['status'] == 'failed'
    assert service.get_run(source['id'])['cases'] == source['cases']
    assert service.get_run(child_id)['prompt']['review_context']['feedback']


def test_incomplete_review_and_no_return_cannot_start_model(tmp_path):
    service = Service(tmp_path / 'test.sqlite3')
    text = next(s['text'] for s in list_samples() if s['id'] == 'normal')
    run_id = service.generate(text=text, operator='tester')
    provider = RevisionProvider()
    with pytest.raises(ServiceError, match='所有用例审核'):
        service.revise_returned(run_id, service.get_run(run_id)['revision'], 'tester', provider, uuid4().hex)
    for c in service.get_run(run_id)['cases']:
        service.review(run_id, c['case_id'], c['version'], c['revision'], 'approve', 'tester', '已核对', uuid4().hex)
    with pytest.raises(ServiceError, match='没有退回'):
        service.revise_returned(run_id, service.get_run(run_id)['revision'], 'tester', provider, uuid4().hex)
    assert not provider.calls


def test_fixture_is_not_a_revision_engine(reviewed):
    service, source = reviewed
    with pytest.raises(ServiceError, match='固定样例'):
        service.revise_returned(source['id'], source['revision'], 'tester', FixtureProvider(), uuid4().hex)


def test_unchanged_returned_case_remains_visible_as_warning(reviewed):
    service, source = reviewed
    def undo_edit(output, ids):
        for c in output['test_cases']:
            if c['case_id'] in ids:
                c['title'] = c['title'].removesuffix('（根据退回意见修订）')
    child_id = service.revise_returned(source['id'], source['revision'], 'tester', RevisionProvider(undo_edit), uuid4().hex)
    child = service.get_run(child_id)
    assert child['status'] == 'valid'
    assert child['validation']['warnings']
    assert all(c['status'] == 'pending_review' for c in child['cases'])
