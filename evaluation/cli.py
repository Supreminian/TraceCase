"""Evaluation preparation and scoring; no command invokes a model."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics
from datetime import datetime, timezone

DEFAULT_DATASET = Path(__file__).resolve().parent
BADCASE_TAGS = {'漏边界', '捏造规则', '前置条件缺失', '需求引用错误', '预期不可判断',
                '权限遗漏', '漏澄清', '冲突未识别', '输出格式错误', '服务调用失败'}
REVIEW_CONFIRMATION = 'ALL_20_STANDARDS_REVIEWED'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def strict_loads(value: str):
    def pairs(items):
        result = {}
        for key, item in items:
            require(key not in result, f'duplicate JSON key: {key}')
            result[key] = item
        return result

    def constant(value):
        raise ValueError(f'non-finite JSON constant: {value}')

    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


def read_json(path):
    return strict_loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Results and plans are append-by-file artifacts, never silently overwritten.
    with path.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def read_jsonl(path):
    return [strict_loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines()
            if line.strip()]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def text_required(value, label):
    require(isinstance(value, str) and bool(value.strip()), f'{label} must be nonempty text')


def unique_index(items, key, label):
    require(isinstance(items, list), f'{label} must be a list')
    result = {}
    for item in items:
        require(isinstance(item, dict), f'{label} item must be an object')
        identity = item.get(key)
        text_required(identity, f'{label}.{key}')
        require(identity not in result, f'duplicate {label}: {identity}')
        result[identity] = item
    return result


def local_file(root, relative):
    require(isinstance(relative, str), 'dataset path must be text')
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()), 'dataset path escapes dataset directory')
    return path


def validate_dataset(root=DEFAULT_DATASET):
    root = Path(root)
    manifest = read_json(root / 'manifest.json')
    require(manifest.get('schema_version') == '1.0', 'unsupported manifest schema')
    text_required(manifest.get('version'), 'dataset version')
    status = manifest.get('status')
    require(status in {'draft_pending_human_review', 'frozen'}, 'invalid dataset status')
    if status == 'draft_pending_human_review':
        require(manifest.get('reviewer') is None and manifest.get('reviewed_at') is None,
                'draft must not claim human review')
    else:
        text_required(manifest.get('reviewer'), 'reviewer')
        text_required(manifest.get('reviewed_at'), 'reviewed_at')
        unsigned = {k: v for k, v in manifest.items() if k != 'freeze_sha256'}
        require(manifest.get('freeze_sha256') == digest(canonical(unsigned)),
                'frozen manifest hash mismatch')
    samples = unique_index(manifest.get('samples'), 'id', 'samples')
    require(len(samples) == 20, 'dataset must contain 20 samples')
    require(sum(s['split'] == 'dev' for s in samples.values()) == 12, 'expected 12 dev')
    require(sum(s['split'] == 'holdout' for s in samples.values()) == 8, 'expected 8 holdout')
    for sid, sample in samples.items():
        paths = {kind: local_file(root, sample[kind]) for kind in ('prd', 'gold')}
        for kind, path in paths.items():
            require(digest(path.read_bytes()) == sample[kind + '_sha256'],
                    f'{sid}: {kind} SHA-256 mismatch')
        gold = read_json(paths['gold'])
        require(gold.get('schema_version') == '1.0' and gold.get('sample_id') == sid,
                f'{sid}: gold identity/schema mismatch')
        paragraphs = unique_index(gold.get('paragraphs'), 'id', f'{sid} paragraphs')
        chunks = paths['prd'].read_text(encoding='utf-8').strip().split('\n\n')
        expected = {f'P{i+1:03}': chunk for i, chunk in enumerate(chunks)}
        require({k: p['text'] for k, p in paragraphs.items()} == expected,
                f'{sid}: source paragraph IDs/text mismatch')
        rules = unique_index(gold.get('key_rules'), 'id', f'{sid} key rules')
        require(bool(rules), f'{sid}: must have at least one key rule')
        clarifications = unique_index(gold.get('clarifications'), 'id', f'{sid} clarifications')
        for item in list(rules.values()) + list(clarifications.values()):
            refs = item.get('paragraph_ids')
            require(isinstance(refs, list) and refs and len(refs) == len(set(refs))
                    and set(refs) <= set(paragraphs), f'{sid}: invalid paragraph references')
        for rule in rules.values():
            text_required(rule.get('statement'), f'{sid} rule statement')
            require(type(rule.get('executable')) is bool, f'{sid}: executable must be bool')
            checks = unique_index(rule.get('required_checks'), 'id', f'{sid} checks')
            require(bool(checks) == rule['executable'],
                    f'{sid}: executable rules require checks, ambiguous rules must have none')
            for check in checks.values():
                text_required(check.get('condition'), f'{sid} check condition')
                text_required(check.get('expected'), f'{sid} check expected')
        require(not any(not r['executable'] for r in rules.values()) or clarifications,
                f'{sid}: nonexecutable rule needs clarification standard')
        for question in clarifications.values():
            text_required(question.get('question'), f'{sid} question')
            require(type(question.get('blocking')) is bool, f'{sid}: blocking must be bool')
        require(isinstance(gold.get('prohibited_inferences'), list), 'missing prohibited inferences')
        for item in gold['prohibited_inferences']:
            text_required(item, f'{sid} prohibited inference')
    return manifest


def freeze_dataset(root, output, version, reviewer, confirmation):
    text_required(reviewer, 'reviewer')
    text_required(version, 'frozen dataset version')
    require(confirmation == REVIEW_CONFIRMATION,
            f'explicit human review confirmation required: {REVIEW_CONFIRMATION}')
    root, output = Path(root), Path(output)
    manifest = validate_dataset(root)
    require(not output.exists(), 'freeze output must be a new directory')
    require(not output.resolve().is_relative_to(root.resolve()),
            'freeze output must be outside source dataset directory')
    require(version != manifest['version'], 'frozen version must differ from source version')
    output.mkdir(parents=True)
    for sample in manifest['samples']:
        for kind in ('prd', 'gold'):
            destination = output / sample[kind]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local_file(root, sample[kind]), destination)
    manifest = {**manifest, 'version': version, 'status': 'frozen', 'reviewer': reviewer.strip(),
                'reviewed_at': utc_now(),
                'provenance': 'Human review explicitly attested by the identified reviewer.',
                'source_manifest_sha256': digest((root / 'manifest.json').read_bytes())}
    manifest.pop('freeze_sha256', None)
    manifest['freeze_sha256'] = digest(canonical(manifest))
    write_json(output / 'manifest.json', manifest)
    validate_dataset(output)
    return manifest


def make_plan(root=DEFAULT_DATASET, provider='unconfigured', model='unconfigured',
              parameters=None, revision=None, prompts=None):
    root = Path(root)
    manifest = validate_dataset(root)
    text_required(provider, 'provider')
    text_required(model, 'model')
    parameters = {} if parameters is None else parameters
    require(isinstance(parameters, dict), 'parameters must be a JSON object')
    prompt_records = {}
    for version in ('V1', 'V2'):
        path = (prompts or {}).get(version)
        prompt_records[version] = ({'text': Path(path).read_text(encoding='utf-8'),
                                    'sha256': digest(Path(path).read_bytes())} if path else None)
        if path:
            text_required(prompt_records[version]['text'], f'{version} prompt')
    config = {'provider': provider, 'model': model, 'revision': revision, 'parameters': parameters}
    plan = {'schema_version': '1.0', 'kind': 'call_plan_only',
            'dataset_version': manifest['version'],
            'dataset_manifest_sha256': digest((root / 'manifest.json').read_bytes()),
            'dataset_status': manifest['status'], 'model_config': config, 'prompts': prompt_records,
            'formal_experiment_eligible': manifest['status'] == 'frozen'
                and provider not in {'unconfigured', 'fixture'} and model != 'unconfigured'
                and all(prompt_records.values()),
            'note': 'No calls executed. Draft/unconfigured plans cannot establish Prompt quality.'}
    plan['plan_id'] = digest(canonical(plan))[:16]
    # Fixed interleaving V1/V2 for each replicate; no retry can replace an entry.
    plan['planned_runs'] = [
        {'planned_run_id': f"{plan['plan_id']}-{sample['id']}-{prompt}-r{repeat}",
         'sample_id': sample['id'], 'split': sample['split'], 'prompt_version': prompt,
         'repeat': repeat, 'seed': None, 'sequence': sequence}
        for sequence, (sample, repeat, prompt) in enumerate(
            ((sample, repeat, prompt) for sample in manifest['samples']
             for repeat in (1, 2, 3) for prompt in ('V1', 'V2')), 1)]
    return plan


def validate_plan(plan, manifest, root):
    require(plan.get('schema_version') == '1.0' and plan.get('kind') == 'call_plan_only',
            'unsupported plan')
    require(plan.get('dataset_version') == manifest['version'], 'plan dataset version mismatch')
    require(plan.get('dataset_manifest_sha256') == digest((Path(root) / 'manifest.json').read_bytes()),
            'plan dataset hash mismatch')
    unsigned = {k: v for k, v in plan.items() if k not in {'plan_id', 'planned_runs'}}
    require(plan.get('plan_id') == digest(canonical(unsigned))[:16], 'plan ID/hash mismatch')
    entries = unique_index(plan.get('planned_runs'), 'planned_run_id', 'planned runs')
    samples = {s['id']: s for s in manifest['samples']}
    expected = {(s, v, r) for s in samples for v in ('V1', 'V2') for r in (1, 2, 3)}
    require(len(entries) == 120, 'expected exactly 120 planned runs')
    require({(e['sample_id'], e['prompt_version'], e['repeat']) for e in entries.values()} == expected,
            'plan has missing/duplicate sample/version/repeat combinations')
    for entry in entries.values():
        require(entry['split'] == samples[entry['sample_id']]['split'], 'plan split mismatch')
        require(entry['planned_run_id'] ==
                f"{plan['plan_id']}-{entry['sample_id']}-{entry['prompt_version']}-r{entry['repeat']}",
                'planned run ID mismatch')
    expected_order = [(sample['id'], version, repeat) for sample in manifest['samples']
                      for repeat in (1, 2, 3) for version in ('V1', 'V2')]
    for sequence, (entry, expected_tuple) in enumerate(zip(entries.values(), expected_order, strict=True), 1):
        require((entry['sample_id'], entry['prompt_version'], entry['repeat']) == expected_tuple
                and entry['sequence'] == sequence and entry['seed'] is None,
                'plan execution order/sequence/seed mismatch')
    return entries


def nonnegative(value, label, integer=False):
    require(value is None or (type(value) in (int, float) and math.isfinite(value)
            and value >= 0 and (not integer or type(value) is int)), f'{label}: invalid number')


def validate_raw(record, plan):
    require(record.get('plan_id') == plan['plan_id'], 'raw result plan ID mismatch')
    require(record.get('model_config') == plan['model_config'], 'actual model/parameters mismatch')
    require(record.get('status') in {'completed', 'failed'}, 'status must be completed/failed')
    raw = record.get('raw_output')
    require(raw is None or isinstance(raw, str), 'raw_output must be string or null')
    if record['status'] == 'failed':
        text_required(record.get('error'), 'failure error')
    for field in ('latency_ms', 'total_latency_ms'):
        require(field in record, f'missing {field}; use null if unavailable')
        nonnegative(record[field], field)
    usage = record.get('usage')
    if usage is not None:
        require(isinstance(usage, dict), 'usage must be object/null')
        for key in ('input_tokens', 'output_tokens'):
            require(key in usage, f'missing usage {key}; use null')
            nonnegative(usage[key], key, integer=True)
    cost = record.get('cost')
    if cost is not None:
        require(isinstance(cost, dict), 'cost must be object/null')
        require(cost.get('amount') is not None, 'cost amount missing; use cost=null instead')
        nonnegative(cost['amount'], 'cost amount')
        for key in ('currency', 'pricing_version', 'method'):
            text_required(cost.get(key), f'cost {key}')


def judge_review(review, raw, gold, output, version):
    text_required(review.get('reviewer'), 'reviewer')
    text_required(review.get('reason'), 'review reason')
    text_required(review.get('reviewed_at'), 'reviewed_at')
    try:
        when = datetime.fromisoformat(review['reviewed_at'].replace('Z', '+00:00'))
        require(when.tzinfo is not None, 'reviewed_at must have timezone')
    except ValueError as exc:
        raise ValueError('reviewed_at must be a timezone-aware ISO timestamp') from exc
    require(review.get('standard_version') == version, 'review standard version mismatch')
    require(review.get('raw_output_sha256') == digest(raw.encode('utf-8')), 'review raw hash mismatch')
    require(review.get('all_business_assertions_reviewed') is True,
            'review must attest complete atomic assertion review')
    rules = unique_index(gold['key_rules'], 'id', 'gold rules')
    rule_judgments = unique_index(review.get('rule_judgments'), 'rule_id', 'rule judgments')
    require(set(rule_judgments) == set(rules), 'review must judge all key rules')
    covered = 0
    for rid, judgment in rule_judgments.items():
        for key in ('evidence', 'reason'):
            text_required(judgment.get(key), f'rule {rid} {key}')
        checks = judgment.get('covered_check_ids')
        allowed = {c['id'] for c in rules[rid]['required_checks']}
        require(isinstance(checks, list) and len(checks) == len(set(checks))
                and set(checks) <= allowed, f'unknown/duplicate checks for {rid}')
        covered += bool(rules[rid]['executable'] and set(checks) == allowed)
    questions = unique_index(gold['clarifications'], 'id', 'gold clarifications')
    judgments = unique_index(review.get('clarification_judgments'), 'clarification_id',
                             'clarification judgments')
    require(set(judgments) == set(questions), 'review must judge all clarification standards')
    identified = 0
    for judgment in judgments.values():
        require(type(judgment.get('identified')) is bool, 'identified must be boolean')
        for key in ('evidence', 'reason'):
            text_required(judgment.get(key), f'clarification {key}')
        identified += judgment['identified']
    case_judgments = unique_index(review.get('case_judgments'), 'case_id', 'case judgments')
    cases = {case['case_id'] for case in output['test_cases']}
    require(set(case_judgments) == cases, 'review must judge every generated case')
    executable = 0
    readiness = {c['case_id']: c['readiness'] for c in output['test_cases']}
    for cid, judgment in case_judgments.items():
        require(type(judgment.get('executable')) is bool, 'executable must be boolean')
        require(not judgment['executable'] or readiness[cid] == 'ready',
                'needs_clarification case cannot be counted executable')
        for key in ('evidence', 'reason'):
            text_required(judgment.get(key), f'case {cid} {key}')
        executable += judgment['executable']
    assertions = unique_index(review.get('assertions'), 'assertion_id', 'assertions')
    seen = set()
    for assertion in assertions.values():
        require(assertion.get('case_id') in cases, 'assertion references unknown case')
        require(type(assertion.get('supported')) is bool, 'supported must be boolean')
        for key in ('statement', 'evidence', 'reason'):
            text_required(assertion.get(key), f'assertion {key}')
        identity = (assertion['case_id'], assertion['statement'].strip())
        require(identity not in seen, 'duplicate assertion within one case')
        seen.add(identity)
    false_positives = review.get('false_positive_clarifications')
    require(type(false_positives) is int and false_positives >= 0, 'false positives must be count')
    badcases = review.get('badcases')
    require(isinstance(badcases, list), 'badcases must be list')
    for badcase in badcases:
        require(isinstance(badcase.get('tags'), list) and badcase['tags']
                and set(badcase['tags']) <= BADCASE_TAGS, 'invalid badcase tags')
        for key in ('evidence', 'reason'):
            text_required(badcase.get(key), f'badcase {key}')
        require(isinstance(badcase.get('standard_refs'), list) and badcase['standard_refs'],
                'badcase requires standard references')
        require(set(badcase['standard_refs']) <= set(rules) | set(questions),
                'badcase references unknown standard')
    return {'coverage_numerator': covered, 'clarification_numerator': identified,
            'case_numerator': executable, 'case_denominator': len(cases),
            'unsupported_numerator': sum(not a['supported'] for a in assertions.values()),
            'assertion_denominator': len(assertions), 'false_positive_clarifications': false_positives,
            'badcases': badcases, 'reviewer': review['reviewer'], 'reviewed_at': review['reviewed_at']}


def score_records(root, plan, raw_records, reviews):
    from sdlc.domain import prepare_input, validate_output

    root = Path(root)
    manifest = validate_dataset(root)
    entries = validate_plan(plan, manifest, root)
    raw_index = unique_index(raw_records, 'planned_run_id', 'raw results')
    review_index = unique_index(reviews, 'planned_run_id', 'reviews')
    require(bool(raw_index), 'No raw results: metrics cannot be produced from a call plan.')
    require(set(raw_index) <= set(entries), 'unknown raw run; retries must be recorded separately')
    require(set(review_index) <= set(raw_index), 'review has no matching raw result')
    samples = {s['id']: s for s in manifest['samples']}
    rows = []
    for rid, entry in entries.items():
        sample = samples[entry['sample_id']]
        gold = read_json(root / sample['gold'])
        row = {**entry, 'json_valid': False, 'schema_valid': False, 'status': 'not_executed',
               'reviewed': False, 'coverage_numerator': 0,
               'coverage_denominator': len(gold['key_rules']),
               'ambiguous_rule_count': sum(not r['executable'] for r in gold['key_rules']),
               'clarification_numerator': 0, 'clarification_denominator': len(gold['clarifications']),
               'case_numerator': 0, 'case_denominator': 0,
               'unsupported_numerator': 0, 'assertion_denominator': 0,
               'false_positive_clarifications': None, 'latency_ms': None, 'total_latency_ms': None,
               'usage': None, 'cost': None, 'badcases': [], 'validation_errors': []}
        if rid in raw_index:
            record = raw_index[rid]
            validate_raw(record, plan)
            row.update({key: record.get(key) for key in
                        ('status', 'latency_ms', 'total_latency_ms', 'usage', 'cost')})
            raw = record.get('raw_output')
            row['raw_output_sha256'] = digest(raw.encode('utf-8')) if raw is not None else None
            paragraphs = prepare_input(text=(root / sample['prd']).read_text(encoding='utf-8'))['paragraphs']
            valid = validate_output(raw or '', paragraphs)
            row['json_valid'] = record['status'] == 'completed' and valid['json_valid']
            row['schema_valid'] = record['status'] == 'completed' and valid['valid']
            row['validation_errors'] = valid['errors']
            if rid in review_index:
                require(row['schema_valid'], 'case/assertion review requires schema-qualified output')
                row.update(judge_review(review_index[rid], raw, gold, valid['output'], manifest['version']))
                row['reviewed'] = True
        rows.append(row)
    return {'schema_version': '1.0', 'kind': 'scored_results', 'plan_id': plan['plan_id'],
            'dataset_version': manifest['version'], 'dataset_manifest_sha256': plan['dataset_manifest_sha256'],
            'formal_experiment_eligible': plan['formal_experiment_eligible'],
            'created_at': utc_now(), 'planned_count': len(entries), 'recorded_count': len(raw_index),
            'raw_records_sha256': digest(canonical(raw_records)),
            'review_records_sha256': digest(canonical(reviews)), 'rows': rows}


def rate(rows, numerator, denominator):
    n = sum(row[numerator] for row in rows)
    d = sum(row[denominator] for row in rows)
    return {'numerator': n, 'denominator': d, 'value': n / d if d else None}


def timing(values):
    values = sorted(value for value in values if value is not None)
    return {'n': len(values), 'median': statistics.median(values) if values else None,
            'p95_nearest_rank': values[math.ceil(.95 * len(values))-1] if values else None}


def summarize(scored):
    require(scored.get('kind') == 'scored_results' and scored.get('recorded_count', 0) > 0,
            'summary needs recorded results, not a plan or fabricated empty metrics')
    rows = scored['rows']
    require(len(rows) == scored['planned_count'] == 120, 'summary must retain all 120 rows')
    groups = []
    for split in ('dev', 'holdout'):
        for prompt in ('V1', 'V2'):
            selected = [r for r in rows if r['split'] == split and r['prompt_version'] == prompt]
            n = len(selected)
            recorded = sum(r['status'] != 'not_executed' for r in selected)
            judged = [r for r in selected if r['schema_valid'] and r['reviewed']]
            costs = {}
            for row in selected:
                if row['cost'] is not None:
                    cost = row['cost']
                    key = (cost['currency'], cost['pricing_version'], cost['method'])
                    costs.setdefault(key, []).append(cost['amount'])
            tokens = {}
            for key in ('input_tokens', 'output_tokens'):
                known = [r['usage'][key] for r in selected if r['usage'] and r['usage'][key] is not None]
                tokens[key] = {'n': len(known), 'known_total': sum(known) if known else None}
            tags = {tag: sum(tag in b['tags'] for r in judged for b in r['badcases'])
                    for tag in sorted(BADCASE_TAGS)}
            group = {'split': split, 'prompt_version': prompt, 'planned': n,
                'recorded': recorded, 'not_executed': n-recorded,
                'failed_calls': sum(r['status'] == 'failed' for r in selected),
                'unjudged_schema_valid': sum(r['schema_valid'] and not r['reviewed'] for r in selected),
                'excluded_case_assertion_runs': n-len(judged),
                'provisional': recorded < n or any(r['schema_valid'] and not r['reviewed'] for r in selected),
                'json_valid_rate': {'numerator': sum(r['json_valid'] for r in selected),
                                    'denominator': n, 'value': sum(r['json_valid'] for r in selected)/n},
                'schema_pass_rate': {'numerator': sum(r['schema_valid'] for r in selected),
                                     'denominator': n, 'value': sum(r['schema_valid'] for r in selected)/n},
                'requirement_coverage': rate(selected, 'coverage_numerator', 'coverage_denominator'),
                'ambiguous_rule_instances': sum(r['ambiguous_rule_count'] for r in selected),
                'clarification_recall': rate(selected, 'clarification_numerator', 'clarification_denominator'),
                'case_executability': rate(judged, 'case_numerator', 'case_denominator'),
                'unsupported_assertion_share': rate(judged, 'unsupported_numerator', 'assertion_denominator'),
                'false_positive_clarifications': sum(r['false_positive_clarifications'] for r in judged) if judged else None,
                'badcase_label_counts': tags if judged else None,
                'latency_ms': timing([r['latency_ms'] for r in selected]),
                'total_latency_ms': timing([r['total_latency_ms'] for r in selected]), 'tokens': tokens,
                'costs': [{'currency': k[0], 'pricing_version': k[1], 'method': k[2], 'n': len(v),
                           'known_total': sum(v)} for k, v in costs.items()] or None,
                'cost_known_n': sum(r['cost'] is not None for r in selected)}
            if recorded == 0:
                for metric in ('json_valid_rate', 'schema_pass_rate', 'requirement_coverage',
                               'clarification_recall'):
                    group[metric]['value'] = None
            groups.append(group)
    return {'kind': 'evaluation_summary', 'plan_id': scored['plan_id'],
            'dataset_version': scored['dataset_version'],
            'formal_experiment_eligible': scored['formal_experiment_eligible'],
            'note': 'Provisional groups retain planned denominators. Do not report Prompt improvements '
                    'from drafts, fixtures, missing calls, or missing human review. '
                    'Three repeats are not three independent PRDs.',
            'groups': groups}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('validate', 'plan', 'freeze', 'score'):
        command = commands.add_parser(name)
        command.add_argument('--dataset', type=Path, default=DEFAULT_DATASET)
        if name != 'validate':
            command.add_argument('--output', type=Path, required=True)
        if name == 'plan':
            command.add_argument('--provider', default='unconfigured')
            command.add_argument('--model', default='unconfigured')
            command.add_argument('--revision')
            command.add_argument('--parameters', default='{}')
            command.add_argument('--prompt-v1', type=Path)
            command.add_argument('--prompt-v2', type=Path)
        if name == 'freeze':
            command.add_argument('--version', required=True)
            command.add_argument('--reviewer', required=True)
            command.add_argument('--confirm-reviewed', required=True,
                                 help=f'after reviewing all 20 standards, type {REVIEW_CONFIRMATION}')
        if name == 'score':
            command.add_argument('--plan', type=Path, required=True)
            command.add_argument('--results', type=Path, required=True)
            command.add_argument('--reviews', type=Path, required=True)
    summary = commands.add_parser('summarize')
    summary.add_argument('--scores', type=Path, required=True)
    summary.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'validate':
            manifest = validate_dataset(args.dataset)
            print(json.dumps({'valid': True, 'samples': 20, 'dev': 12, 'holdout': 8,
                              'status': manifest['status'], 'version': manifest['version']}))
            return
        if args.command == 'freeze':
            freeze_dataset(args.dataset, args.output, args.version, args.reviewer, args.confirm_reviewed)
            print(f'Frozen reviewed dataset saved to {args.output}')
            return
        if args.command == 'plan':
            result = make_plan(args.dataset, args.provider, args.model, strict_loads(args.parameters),
                               args.revision, {'V1': args.prompt_v1, 'V2': args.prompt_v2})
        elif args.command == 'score':
            result = score_records(args.dataset, read_json(args.plan), read_jsonl(args.results),
                                   read_jsonl(args.reviews))
        else:
            result = summarize(read_json(args.scores))
        write_json(args.output, result)
        print(f'{args.command}: saved {args.output}; no model was called')
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f'error: {exc}\n')


if __name__ == '__main__':
    main()
