"""Tests explicitly substitute a local transport; production never loads fixtures."""
import pytest

from tests.fixture_provider import FixtureProvider


@pytest.fixture(autouse=True)
def isolated_default_transport(monkeypatch):
    monkeypatch.setattr('sdlc.service.configured_provider', FixtureProvider)
