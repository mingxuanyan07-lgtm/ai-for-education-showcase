// Synthetic fixtures only; tests the exact calculation functions embedded in the HTML.
const assert = require('node:assert/strict');
const M = require('../index_dashboard.js');
const row = (question_id, ai_id, run_id, extra = {}) => ({question_id, ai_id, run_id,
  model: 'actual', test_set_id: 'suite', tags: ['概率论'], categories: ['solving_comparison'],
  status: 'completed', first_verdict: 'wrong', final_verdict: 'correct',
  generation_seconds: null, tokens: null, human_review: '待填写', ...extra});
const rows = [row('Q1', 'a', 'r1'), row('Q1', 'a', 'r2', {status: 'timeout', final_verdict: 'not_run'}),
  row('Q2', 'a', 'r3', {generation_seconds: 0, tokens: 10}), row('Q1', 'b', 'r1', {status: 'failed', final_verdict: 'wrong'}),
  row('Qoutside', 'a', 'r4', {test_set_id: ''})];
const plan = {test_set_id: 'suite', question_ids: ['Q1', 'Q2', 'Q3'], ai_ids: ['a', 'b', 'missing-model']};
const selected = M.select(rows, {testSet: 'suite', ai: 'a'});
assert.equal(selected.length, 3);
const s = M.summarize(selected, plan);
assert.equal(s.n, 3);
assert.equal(s.coverage, '66.7% (2/3)'); // retries never increase question coverage
assert.equal(s.final, '66.7% (2/3)'); // timeout/unknown stays in denominator
assert.equal(s.answer_accuracy, '100.0% (2/2)；已判 2/3，待判 1');
assert.equal(s.reference_accuracy, '待判分（已判 0/3）'); // no reference grade inferred from a model verdict
const graded = ['correct', 'wrong', 'partial', 'not_graded'].map((grade_verdict, i) => row('Q' + i, 'a', 'g' + i, {grade_verdict}));
assert.equal(M.summarize(graded, null).reference_accuracy, '33.3% (1/3)；已判 3/4，待判 1');
assert.equal(M.summarize([], null).answer_accuracy, '待判分（已判 0/0）');
assert.equal(M.summarize(Array.from({length: 400}, (_, i) => row('Q' + i, 'a', 'r' + i, {final_verdict: i ? 'wrong' : 'correct'})), null).answer_accuracy,
  '0.3% (1/400)；已判 400/400，待判 0');
assert.equal(s.first, '0.0% (0/3)'); // later correct never overwrites first draft
assert.equal(s.unknown, '33.3% (1/3)');
assert.equal(s.duration, '0.0 秒 (n=1/3)'); // genuine zero, missing values excluded
assert.equal(s.tokens, '10.0 (n=1/3)');
assert.deepEqual(s.missing, ['Q3']);
const groups = M.groups(M.select(rows, {testSet: 'suite'}), plan);
assert.equal(groups.length, 3);
assert.equal(M.summarize(groups.find(g => g.ai === 'missing-model').rows, plan).coverage, '0.0% (0/3)');
assert.equal(M.groups(selected, plan, {ai: 'a'}).length, 1);
assert.equal(M.groups(selected, plan, {model: 'actual'}).length, 1);
assert.equal(M.groups([...selected, row('Q1', 'a', 'new-model', {model: 'other'})], plan, {ai: 'a'}).length, 2);
assert.equal(M.select(rows, {testSet: '__unassigned__'}).length, 1);
assert.equal(M.select(rows, {testSet: 'suite', status: 'failed', tag: '概率论', verdict: 'wrong', category: 'solving_comparison', search: 'Q1'}).length, 1);
assert.equal(M.summarize([], plan).final, '未记录（分母为 0）');
assert.equal(M.summarize([], null).coverage, '未配置题目清单');
assert.equal(M.summarize(rows, plan).coverage, '66.7% (2/3)');
const external = ['correct', 'wrong', 'partial', 'unanswered', 'not_reviewed'].map((external_verdict, i) =>
  row('E' + i, 'a', 'e' + i, {external_verdict, external_protocol: 'shared-v1', external_rubric: 'rubric-v1'}));
assert.equal(M.summarize(external, null).external_accuracy, '25.0% (1/4)；已判 4/5，待判 1');
assert.equal(M.summarize(external, null).external_coverage, '80.0% (4/5)');
assert.equal(M.summarize(rows, null).external_accuracy, '待判分（已判 0/5）');
assert.ok(M.summarize([...external, {...external[0], external_protocol: 'other-v2'}], null).external_accuracy.includes('混合'));
assert.equal(M.select(external, {protocol: 'shared-v1', rubric: 'rubric-v1'}).length, 5);
assert.equal(M.groups([external[0], {...external[0], external_protocol: 'other-v2'}], null).length, 2);
assert.equal(M.groups([external[0], {...external[0], evaluation: {budget: {max_calls: 10}}}], null).length, 2);
const csv = M.csv([row('=2+2', '@candidate', 'r,"1')]);
assert.ok(csv.startsWith('\ufeff'));
assert.ok(csv.includes('"\'=2+2"'));
assert.ok(csv.includes('"\'@candidate"'));
assert.ok(csv.includes('"r,""1"'));
const measured = row('Q1', 'a', 'm1', {
  model: '配置 deepseek-flash（实际型号未返回）', actual_model: '未记录', configured_model: 'deepseek-flash',
  observed_seconds: 99.64, observed_seconds_scope: 'DeepSeek分阶段主流程，终审另计', observed_seconds_scope_id: 'deepseek_staged_wall',
  observed_tokens: 18963, observed_tokens_scope: 'DeepSeek角色已观测小计', observed_tokens_scope_id: 'deepseek_roles',
  observed_calls: 4, observed_calls_scope: '宿主角色调用', observed_calls_scope_id: 'host_role_invocations',
  observed_tokens_label: '18,963（已观测小计）', metering_coverage: 'partial', metering_exclusions: ['Codex终审未测'],
  system_tokens: null
});
const measuredAgain = {...measured, run_id: 'm2', observed_seconds: 200, observed_tokens: 10000};
const observed = M.summarize([measured, measuredAgain], null);
assert.ok(observed.observed_seconds.startsWith('149.8 秒 (n=2/2)'));
assert.ok(observed.observed_tokens.startsWith('14481.5 (n=2/2)'));
assert.ok(observed.observed_tokens.includes('DeepSeek角色已观测小计'));
assert.ok(observed.system_tokens.startsWith('未记录 (n=0)')); // partial total never becomes full-system total
assert.ok(M.summarize([measured, {...measured, observed_tokens_scope_id: 'native_api_attempts'}], null).observed_tokens.includes('口径不同，不合并'));
assert.ok(M.summarize([{...measured, observed_tokens_scope_id: undefined}], null).observed_tokens.includes('口径未记录，不合并'));
assert.ok(M.summarize([measured, {...measured, observed_tokens: null}], null).observed_tokens.includes('n=1/2'));
assert.ok(M.summarize([{...measured, observed_tokens: 0}], null).observed_tokens.startsWith('0.0'));
assert.ok(M.summarize([{...measured, observed_tokens: null, estimated_tokens: 3514}], null).observed_tokens.includes('未记录'));
assert.equal(M.select([measured], {model: measured.model}).length, 1);
assert.equal(M.select([measured], {search: 'deepseek-flash'}).length, 1);
const measuredCsv = M.csv([measured]);
assert.ok(measuredCsv.includes('"actual_model","configured_model"'));
assert.ok(measuredCsv.includes('"observed_tokens"'));
assert.ok(measuredCsv.includes('"deepseek_roles"'));
assert.ok(measuredCsv.includes('Codex终审未测'));
assert.ok(M.options.find(([id]) => id === 'observed_tokens')[2]);
assert.ok(M.options.find(([id]) => id === 'observed_seconds')[2]);
console.log('Index dashboard calculations: passed');
