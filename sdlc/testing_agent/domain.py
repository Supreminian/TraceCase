"""Typed stage outputs. Business facts always carry document paragraph references."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from sdlc.domain import (BusinessRule, Clarification, Feature, Role, StrictModel,
                         TestCase, Text)


class RequirementAnalysis(StrictModel):
    features: list[Feature]
    roles: list[Role]
    business_rules: list[BusinessRule]
    clarifications: list[Clarification]
    risks: list[Text] = Field(default_factory=list)


class TestCondition(StrictModel):
    __test__ = False
    id: Text
    module: Text
    description: Text
    requirement_ids: list[Text] = Field(min_length=1)
    paragraph_ids: list[Text] = Field(min_length=1)
    method: Literal['equivalence', 'boundary', 'decision_table', 'state_transition',
                    'scenario', 'permission', 'exploratory']
    priority: Literal['P0', 'P1', 'P2']
    status: Literal['planned', 'needs_clarification', 'out_of_scope'] = 'planned'
    rationale: Text


class CoveragePlan(StrictModel):
    conditions: list[TestCondition] = Field(min_length=1)


class CaseDraft(StrictModel):
    case: TestCase
    condition_ids: list[Text] = Field(min_length=1)
    role: str = ''
    test_data: list[str] = Field(default_factory=list)
    cleanup: list[str] = Field(default_factory=list)
    method: str = 'scenario'


class CaseBatch(StrictModel):
    cases: list[CaseDraft] = Field(min_length=1, max_length=12)


class RevisionProposal(StrictModel):
    case: TestCase
    explanation: Text
    unresolved: list[str] = Field(default_factory=list)


class EvidenceFinding(StrictModel):
    case_id: Text
    claim: Text
    verdict: Literal['unsupported', 'uncertain']
    paragraph_ids: list[Text] = Field(min_length=1)
    rationale: Text


class EvidenceReview(StrictModel):
    findings: list[EvidenceFinding] = Field(default_factory=list)


FEEDBACK_ROUTES = {
    'unclear_steps': 'revision', 'unclear_expected': 'revision',
    'missing_boundary': 'revision', 'missing_permission': 'revision',
    'missing_state': 'revision', 'unsupported_rule': 'revision',
    'duplicate': 'merge_proposal', 'requirement_changed': 'requirement_confirmation',
    'style': 'revision',
}

TASK_STATUSES = {'queued', 'running', 'waiting_answer', 'waiting_review', 'partial',
                 'waiting_confirmation', 'completed', 'failed', 'cancelled'}
