"""Tests explicitly substitute a local transport; production never loads fixtures."""
import pytest

from tests.fixture_provider import FixtureProvider


@pytest.fixture(autouse=True)
def isolated_default_transport(monkeypatch):
    monkeypatch.setattr('sdlc.service.configured_provider', FixtureProvider)


@pytest.fixture(autouse=True)
def authenticated_legacy_ui_harness(request, monkeypatch):
    """Old workflow tests exercise real authorized services; only browser cookie delivery is mocked.

    Explicit local migration assigns their disposable fixtures, never production/first-registration data.
    Gateway cookie delivery and unowned legacy isolation have separate integration tests.
    """
    if request.module.__name__ not in {'tests.test_app', 'tests.test_project_app', 'tests.test_provider',
                                        'tests.test_validation_repair', 'tests.test_live_generation'}:
        return
    from sdlc.accounts import Accounts
    from scripts.assign_legacy import assign
    import sdlc.account_ui
    import sdlc.jobs
    import sdlc.providers
    contexts = {}

    def enter(path):
        if path not in contexts:
            store = Accounts(path)
            store.register('ui-tester', '测试登录用户', 'disposable-test-password')
            token = store.login('ui-tester', 'disposable-test-password')
            user = store.me(token)
            workspace = store.spaces(token)[0]['id']
            contexts[path] = token, workspace, user
        token, workspace, user = contexts[path]
        with Accounts(path).db() as db:
            projects = [r[0] for r in db.execute('SELECT id FROM projects WHERE id NOT IN (SELECT project_id FROM project_workspaces)')]
        for project in projects:
            assign(path, project, workspace)
        return token, workspace, user

    monkeypatch.setattr(sdlc.account_ui, 'enter', enter)
    monkeypatch.setattr(sdlc.jobs, 'submit_initial', lambda service, job: sdlc.jobs.execute_initial(service, job, sdlc.providers.configured_provider()))
    monkeypatch.setattr(sdlc.jobs, 'submit', lambda *args: None)
