import copy
import json
import shutil

import pytest

from evaluation.cli import (
    DEFAULT_DATASET, REVIEW_CONFIRMATION, digest, freeze_dataset, judge_review,
    make_plan, read_json, score_records, summarize, validate_dataset, write_json,
)


def raw_record(plan, index=0, raw='not JSON', status='completed'):
    return {'planned_run_id': plan['planned_runs'][index]['planned_run_id'],
            'plan_id': plan['plan_id'], 'model_config': plan['model_config'],
            'status': status, 'raw_output': raw, 'latency_ms': 12,
            'total_latency_ms': 13, 'usage': None, 'cost': None,
            **({'error': 'test timeout'} if status == 'failed' else {})}


def valid_output():
    # Synthetic unit-test input. This is not a model result or an evaluation claim.
    return {'schema_version': '1.0', 'features': [], 'roles': [],
            'business_rules': [{'id': 'BR01', 'statement': '已审批金额不能修改',
                                'status': 'clear', 'paragraph_ids': ['P004']}],
            'clarifications': [], 'test_cases': [{
                'case_id': 'TC01', 'title': '已审批金额锁定', 'requirement_ids': ['BR01'],
                'paragraph_ids': ['P004'], 'preconditions': ['存在已审批单'],
                'steps': [{'number': 1, 'action': '尝试修改金额'}],
                'expected_results': ['拒绝且金额不变'], 'priority': 'P0',
                'category': 'exception', 'readiness': 'ready', 'clarification_ids': []}]}


def review_record(plan, raw):
    gold = read_json(DEFAULT_DATASET / 'samples/dev/EXP-001.gold.json')
    return {'planned_run_id': plan['planned_runs'][0]['planned_run_id'],
            'raw_output_sha256': digest(raw.encode()), 'standard_version': '0.1-draft',
            'reviewer': 'unit-test-only', 'reviewed_at': '2026-10-26T09:00:00+08:00',
            'reason': 'Synthetic scoring verification, not a real evaluation.',
            'all_business_assertions_reviewed': True,
            'rule_judgments': [{'rule_id': r['id'],
                                'covered_check_ids': ['C01'] if r['id'] == 'R03' else [],
                                'evidence': 'TC01' if r['id'] == 'R03' else 'No covering case',
                                'reason': 'Check present' if r['id'] == 'R03' else 'Not generated'}
                               for r in gold['key_rules']],
            'clarification_judgments': [], 'false_positive_clarifications': 0,
            'case_judgments': [{'case_id': 'TC01', 'executable': True,
                               'evidence': 'TC01', 'reason': 'Preconditions, action and expectation clear'}],
            'assertions': [{'assertion_id': 'A01', 'case_id': 'TC01',
                            'statement': '已审批单修改金额被拒绝', 'supported': True,
                            'evidence': 'P004 + TC01.expected_results[0]', 'reason': 'Explicit source'}],
            'badcases': []}


def test_dataset_counts_sources_and_draft_status():
    manifest = validate_dataset()
    assert manifest['status'] == 'draft_pending_human_review'
    assert manifest['reviewer'] is None
    assert [s['split'] for s in manifest['samples']].count('dev') == 12
    assert [s['split'] for s in manifest['samples']].count('holdout') == 8


def test_hash_changes_are_detected(tmp_path):
    dataset = tmp_path / 'dataset'
    shutil.copytree(DEFAULT_DATASET / 'samples', dataset / 'samples')
    shutil.copyfile(DEFAULT_DATASET / 'manifest.json', dataset / 'manifest.json')
    sample = dataset / 'samples/dev/EXP-001.md'
    sample.write_text(sample.read_text() + '\n变更\n')
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        validate_dataset(dataset)


def test_plan_is_deterministic_120_and_does_not_claim_execution():
    first, second = make_plan(), make_plan()
    assert first == second
    assert first['kind'] == 'call_plan_only'
    assert first['formal_experiment_eligible'] is False
    assert len(first['planned_runs']) == 120
    assert sum(r['split'] == 'dev' for r in first['planned_runs']) == 72
    assert sum(r['split'] == 'holdout' for r in first['planned_runs']) == 48
    assert all(r['seed'] is None for r in first['planned_runs'])
    assert 'raw_output' not in json.dumps(first)


def test_plan_execution_order_and_seed_cannot_be_silently_changed():
    from evaluation.cli import validate_plan
    manifest = validate_dataset()
    for field, value in [('sequence', 99), ('seed', 123)]:
        plan = make_plan()
        plan['planned_runs'][0][field] = value
        with pytest.raises(ValueError, match='order/sequence/seed'):
            validate_plan(plan, manifest, DEFAULT_DATASET)


def test_freeze_requires_explicit_review_and_creates_separate_snapshot(tmp_path):
    with pytest.raises(ValueError, match='reviewer'):
        freeze_dataset(DEFAULT_DATASET, tmp_path / 'bad', '1.0', '', REVIEW_CONFIRMATION)
    with pytest.raises(ValueError, match='explicit human review'):
        freeze_dataset(DEFAULT_DATASET, tmp_path / 'bad', '1.0', 'test-reviewer', 'yes')
    result = freeze_dataset(DEFAULT_DATASET, tmp_path / 'frozen', 'unit-test-only',
                            'test-reviewer', REVIEW_CONFIRMATION)
    assert result['status'] == 'frozen'
    assert validate_dataset(tmp_path / 'frozen')['freeze_sha256'] == result['freeze_sha256']
    assert validate_dataset()['status'] == 'draft_pending_human_review'


def test_freeze_tamper_fails(tmp_path):
    output = tmp_path / 'frozen'
    freeze_dataset(DEFAULT_DATASET, output, 'unit-test-only', 'test-reviewer', REVIEW_CONFIRMATION)
    manifest = read_json(output / 'manifest.json')
    manifest['reviewer'] = 'changed'
    (output / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='frozen manifest hash mismatch'):
        validate_dataset(output)


def test_no_results_cannot_create_metrics():
    with pytest.raises(ValueError, match='No raw results'):
        score_records(DEFAULT_DATASET, make_plan(), [], [])


def test_failures_and_missing_runs_keep_denominators_and_null_costs():
    plan = make_plan()
    records = [raw_record(plan, 0, None, 'failed'), raw_record(plan, 1, '{}')]
    scored = score_records(DEFAULT_DATASET, plan, records, [])
    assert len(scored['rows']) == 120
    assert scored['rows'][0]['status'] == 'failed'
    assert scored['rows'][1]['json_valid'] is True
    assert scored['rows'][1]['schema_valid'] is False
    assert scored['rows'][2]['status'] == 'not_executed'
    summary = summarize(scored)
    dev_v1, dev_v2, holdout_v1, holdout_v2 = summary['groups']
    assert dev_v1['json_valid_rate']['denominator'] == 36
    assert dev_v1['failed_calls'] == 1
    assert dev_v2['json_valid_rate']['numerator'] == 1
    assert dev_v1['requirement_coverage']['numerator'] == 0
    assert dev_v1['requirement_coverage']['denominator'] > 0
    assert dev_v1['case_executability'] == {'numerator': 0, 'denominator': 0, 'value': None}
    assert dev_v1['costs'] is None
    assert dev_v1['tokens']['input_tokens']['known_total'] is None
    assert holdout_v1['planned'] == holdout_v2['planned'] == 24
    assert holdout_v1['recorded'] == 0
    assert all(g['provisional'] for g in summary['groups'])


def test_real_validator_and_bound_human_review_drive_metrics():
    plan = make_plan()
    raw = json.dumps(valid_output(), ensure_ascii=False)
    scored = score_records(DEFAULT_DATASET, plan, [raw_record(plan, raw=raw)],
                           [review_record(plan, raw)])
    row = scored['rows'][0]
    assert row['json_valid'] and row['schema_valid'] and row['reviewed']
    assert row['coverage_numerator'] == 1
    assert row['coverage_denominator'] == 4
    assert row['case_numerator'] == row['case_denominator'] == 1
    assert row['assertion_denominator'] == 1
    summary = summarize(scored)['groups'][0]
    assert summary['case_executability']['value'] == 1.0
    assert summary['unsupported_assertion_share']['value'] == 0.0


def test_partial_required_checks_do_not_count_as_covered():
    plan = make_plan()
    raw = json.dumps(valid_output())
    review = review_record(plan, raw)
    review['rule_judgments'][0]['covered_check_ids'] = ['C01']
    row = score_records(DEFAULT_DATASET, plan, [raw_record(plan, raw=raw)], [review])['rows'][0]
    assert row['coverage_numerator'] == 1  # R01 incomplete; only R03 is covered.


@pytest.mark.parametrize('change,match', [
    (lambda r: r.update(raw_output_sha256='wrong'), 'review raw hash mismatch'),
    (lambda r: r['rule_judgments'].pop(), 'all key rules'),
    (lambda r: r['case_judgments'].clear(), 'every generated case'),
    (lambda r: r.update(all_business_assertions_reviewed=False), 'atomic assertion'),
    (lambda r: r.update(reviewed_at='2026-10-26T09:00:00'), 'timezone-aware'),
])
def test_review_cannot_silently_drop_denominator_or_bind_wrong_output(change, match):
    plan = make_plan()
    raw = json.dumps(valid_output())
    review = review_record(plan, raw)
    change(review)
    with pytest.raises(ValueError, match=match):
        score_records(DEFAULT_DATASET, plan, [raw_record(plan, raw=raw)], [review])


def test_duplicate_run_cannot_replace_failure():
    plan = make_plan()
    failure = raw_record(plan, raw=None, status='failed')
    with pytest.raises(ValueError, match='duplicate raw results'):
        score_records(DEFAULT_DATASET, plan, [failure, failure], [])


def test_config_mismatch_and_unknown_retry_rejected():
    plan = make_plan()
    record = raw_record(plan)
    record['model_config'] = {**plan['model_config'], 'model': 'different'}
    with pytest.raises(ValueError, match='model/parameters mismatch'):
        score_records(DEFAULT_DATASET, plan, [record], [])
    record = raw_record(plan)
    record['planned_run_id'] += '-retry'
    with pytest.raises(ValueError, match='unknown raw run'):
        score_records(DEFAULT_DATASET, plan, [record], [])


def test_failed_or_invalid_outputs_cannot_enter_case_metrics():
    plan = make_plan()
    raw = json.dumps(valid_output())
    with pytest.raises(ValueError, match='schema-qualified'):
        score_records(DEFAULT_DATASET, plan, [raw_record(plan, raw=raw, status='failed')],
                      [review_record(plan, raw)])


def test_invalid_numeric_metadata_rejected():
    plan = make_plan()
    record = raw_record(plan)
    record['latency_ms'] = float('nan')
    with pytest.raises(ValueError, match='invalid number'):
        score_records(DEFAULT_DATASET, plan, [record], [])


def test_zero_unknown_cost_is_not_invented_and_currencies_not_combined():
    plan = make_plan()
    a, b = raw_record(plan, 0), raw_record(plan, 2)
    a['cost'] = {'amount': .01, 'currency': 'USD', 'pricing_version': 'test-1', 'method': 'test'}
    b['cost'] = {'amount': .07, 'currency': 'CNY', 'pricing_version': 'test-1', 'method': 'test'}
    summary = summarize(score_records(DEFAULT_DATASET, plan, [a, b], []))['groups'][0]
    assert summary['cost_known_n'] == 2
    assert {c['currency']: c['known_total'] for c in summary['costs']} == {'USD': .01, 'CNY': .07}


def test_summary_and_output_artifacts_not_silently_overwritten(tmp_path):
    output = tmp_path / 'plan.json'
    write_json(output, make_plan())
    with pytest.raises(FileExistsError):
        write_json(output, {})
    with pytest.raises(ValueError, match='recorded results'):
        summarize(make_plan())


def test_ambiguous_gold_rule_cannot_receive_coverage_from_clarification():
    gold = read_json(DEFAULT_DATASET / 'samples/dev/EXP-002.gold.json')
    raw = json.dumps(valid_output())
    plan = make_plan()
    review = review_record(plan, raw)
    review['rule_judgments'] = [
        {'rule_id': 'R01', 'covered_check_ids': [], 'evidence': 'Question', 'reason': 'Still unknown'},
        {'rule_id': 'R02', 'covered_check_ids': [], 'evidence': 'Absent', 'reason': 'Missing'}]
    review['clarification_judgments'] = [
        {'clarification_id': 'Q01', 'identified': True, 'evidence': 'Question', 'reason': 'Matches'}]
    metrics = judge_review(review, raw, gold, valid_output(), '0.1-draft')
    assert metrics['coverage_numerator'] == 0
    assert metrics['clarification_numerator'] == 1


def test_clarification_draft_counts_in_case_denominator_only():
    output = copy.deepcopy(valid_output())
    output['test_cases'][0]['readiness'] = 'needs_clarification'
    raw = json.dumps(output)
    review = review_record(make_plan(), raw)
    review['case_judgments'][0]['executable'] = False
    gold = read_json(DEFAULT_DATASET / 'samples/dev/EXP-001.gold.json')
    result = judge_review(review, raw, gold, output, '0.1-draft')
    assert result['case_denominator'] == 1
    assert result['case_numerator'] == 0
    review['case_judgments'][0]['executable'] = True
    with pytest.raises(ValueError, match='needs_clarification'):
        judge_review(review, raw, gold, output, '0.1-draft')
