/* Local derived data only. All record text enters the DOM through textContent. */
"use strict";
const IndexMetrics = (() => {
  const unique = values => [...new Set(values)];
  const judged = value => ["correct", "wrong", "partial"].includes(value);
  const ratio = (n, d) => {
    if (!d) return "未记录（分母为 0）";
    const tenths = Math.floor((n * 2000 + d) / (2 * d));
    return `${Math.floor(tenths / 10)}.${tenths % 10}% (${n}/${d})`;
  };
  const options = [
    ["external_accuracy", "主指标：共同外部审阅准确率", true],
    ["external_coverage", "共同外审覆盖率", true],
    ["audit", "人工抽查完成比例", false],
    ["teaching_evaluated", "教学质量已评比例", true], ["interaction_evaluated", "互动能力已评比例", true],
    ["answer_accuracy", "流程证据：内部核验正确比例", false],
    ["reference_accuracy", "历史 grading 判分比例（非共同外审）", false],
    ["coverage", "测试集覆盖率", true], ["completed", "运行完成率", true],
    ["verified", "流程证据：内部核验覆盖", false], ["first", "流程证据：首稿核验通过率", false],
    ["final", "流程证据：末轮核验通过率", false], ["wrong", "流程证据：末轮 wrong 比例", false],
    ["unknown", "末轮未核验／未记录", false], ["timeout", "运行超时率", true],
    ["failed", "运行失败率", false], ["interrupted", "运行中断率", false],
    ["duration", "平均生成耗时", false], ["tokens", "平均生成 Token", false],
    ["observed_seconds", "主阶段耗时（同口径均值）", true],
    ["observed_tokens", "已观测 Token（同口径均值）", true],
    ["observed_calls", "已观测调用（同口径均值）", true],
    ["system_calls", "原始 usage 调用（同口径均值）", false], ["system_tokens", "原始 usage Token（同口径均值）", false],
    ["wall_seconds", "原始 usage 耗时（同口径均值）", false],
    ["teaching", "含教学稿比例", false], ["reviewed", "人工评价填写率", false]
  ];
  function summarize(rows, plan) {
    const n = rows.length;
    const count = test => rows.filter(test).length;
    const qids = unique(rows.map(r => r.question_id));
    const planned = plan?.question_ids || [];
    const covered = qids.filter(q => planned.includes(q));
    const externalJudged = value => [...["correct", "wrong", "partial"], "unanswered"].includes(value);
    const signatures = unique(rows.filter(r => r.external_protocol || r.external_rubric).map(r => JSON.stringify([r.external_protocol, r.external_rubric])));
    const accuracy = key => {
      if (key === "external_verdict" && signatures.length > 1) return "外审协议／量表混合，请筛选后比较";
      const scored = count(r => key === "external_verdict" ? externalJudged(r[key]) : judged(r[key]));
      const correct = count(r => r[key] === "correct");
      const pending = n - scored;
      return scored ? `${ratio(correct, scored)}；已判 ${scored}/${n}，待判 ${pending}` : `待判分（已判 0/${n}）`;
    };
    const average = (key, unit) => {
      const values = rows.map(r => r[key]).filter(x => typeof x === "number" && Number.isFinite(x) && x >= 0);
      return values.length ? `${(values.reduce((a, b) => a + b, 0) / values.length).toFixed(1)}${unit} (n=${values.length}/${n})` : "未记录 (n=0)";
    };
    const scopedAverage = (key, unit, scopePrefix = key) => {
      const available = rows.filter(r => typeof r[key] === "number" && Number.isFinite(r[key]) && r[key] >= 0);
      if (!available.length) return "未记录 (n=0)；见逐次运行的缺测说明";
      const scopes = unique(available.map(r => r[scopePrefix + "_scope_id"] || "missing"));
      if (scopes.includes("missing")) return `口径未记录，不合并（已计量 ${available.length}/${n}）`;
      if (scopes.length > 1) return `口径不同，不合并（${scopes.length}类；已计量 ${available.length}/${n}）`;
      const mean = available.reduce((total, r) => total + r[key], 0) / available.length;
      const scope = available[0][scopePrefix + "_scope"] || scopes[0];
      return `${mean.toFixed(1)}${unit} (n=${available.length}/${n})；${scope}`;
    };
    return {
      n, questions: qids.length, missing: planned.filter(q => !covered.includes(q)),
      outside: qids.filter(q => planned.length && !planned.includes(q)),
      coverage: planned.length ? ratio(covered.length, planned.length) : "未配置题目清单",
      answer_accuracy: accuracy("final_verdict"), reference_accuracy: accuracy("grade_verdict"),
      external_accuracy: accuracy("external_verdict"),
      external_coverage: ratio(count(r => externalJudged(r.external_verdict)), n),
      audit: ratio(count(r => r.human_audit_status === "completed"), n),
      teaching_evaluated: ratio(count(r => r.external_dimensions?.teaching_quality?.status === "evaluated"), n),
      interaction_evaluated: ratio(count(r => r.external_dimensions?.interaction?.status === "evaluated"), n),
      system_calls: scopedAverage("system_calls", "", "observed_calls"),
      system_tokens: scopedAverage("system_tokens", "", "observed_tokens"),
      wall_seconds: scopedAverage("wall_seconds", " 秒", "observed_seconds"),
      observed_seconds: scopedAverage("observed_seconds", " 秒"),
      observed_tokens: scopedAverage("observed_tokens", ""), observed_calls: scopedAverage("observed_calls", ""),
      completed: ratio(count(r => r.status === "completed"), n),
      verified: ratio(count(r => judged(r.final_verdict)), n),
      first: ratio(count(r => r.first_verdict === "correct"), n),
      final: ratio(count(r => r.final_verdict === "correct"), n),
      wrong: ratio(count(r => r.final_verdict === "wrong"), n),
      unknown: ratio(count(r => !judged(r.final_verdict)), n),
      timeout: ratio(count(r => r.status === "timeout"), n),
      failed: ratio(count(r => r.status === "failed"), n),
      interrupted: ratio(count(r => r.status === "interrupted"), n),
      duration: average("generation_seconds", " 秒"), tokens: average("tokens", ""),
      teaching: ratio(count(r => r.teaching_rounds > 0), n),
      reviewed: ratio(count(r => r.human_review === "已填写"), n)
    };
  }
  function select(rows, f) {
    return rows.filter(r =>
      (!f.testSet || (f.testSet === "__unassigned__" ? !r.test_set_id : r.test_set_id === f.testSet)) &&
      (!f.ai || r.ai_id === f.ai) && (!f.model || r.model === f.model) &&
      (!f.protocol || r.external_protocol === f.protocol) && (!f.rubric || r.external_rubric === f.rubric) &&
      (!f.system || r.system_id === f.system) && (!f.budget || r.budget_condition === f.budget) &&
      (!f.tag || (r.tags.length ? r.tags : ["未分类"]).includes(f.tag)) &&
      (!f.status || r.status === f.status) && (!f.verdict || r.final_verdict === f.verdict) &&
      (!f.category || (r.categories.length ? r.categories : ["未记录"]).includes(f.category)) &&
      (!f.search || [r.question_label || "", r.question_id, r.run_id, r.ai_id, r.model, r.actual_model || "", r.configured_model || "", ...r.tags].join(" ").toLowerCase().includes(f.search.toLowerCase()))
    );
  }
  function groups(rows, plan, filters = {}) {
    const result = new Map();
    rows.forEach(r => {
      const key = JSON.stringify([r.ai_id, r.model, r.external_protocol || "", r.external_rubric || "", r.evaluation?.budget || {}]);
      if (!result.has(key)) result.set(key, {ai: r.ai_id, model: r.model, protocol: r.external_protocol || "未记录", rubric: r.external_rubric || "未记录", budget: r.budget_condition || "未记录", rows: []});
      result.get(key).rows.push(r);
    });
    // A planned but entirely missing candidate must still show zero coverage.
    if (!filters.model) (plan?.ai_ids || []).forEach(ai => {
      if ((!filters.ai || filters.ai === ai) && ![...result.values()].some(g => g.ai === ai))
        result.set(JSON.stringify([ai, "未记录"]), {ai, model: "未记录", protocol: "未记录", rubric: "未记录", budget: "未记录", rows: []});
    });
    return [...result.values()].sort((a, b) => a.ai.localeCompare(b.ai) || a.model.localeCompare(b.model));
  }
  function csv(rows) {
    const fields = ["test_set_id", "question_label", "question_id", "ai_id", "model", "actual_model", "configured_model", "model_source", "recorded_model", "framework", "run_id", "status",
      "system_id", "external_verdict", "external_protocol", "external_rubric", "external_reviewer", "external_review_id", "human_audit_status", "budget_condition",
      "evaluation", "external_dimensions", "system_calls", "system_tokens", "wall_seconds",
      "observed_seconds", "observed_seconds_label", "observed_seconds_scope", "observed_seconds_scope_id",
      "observed_tokens", "observed_tokens_label", "observed_tokens_scope", "observed_tokens_scope_id",
      "observed_input_tokens", "observed_output_tokens", "observed_cached_input_tokens", "estimated_tokens",
      "observed_calls", "observed_calls_label", "observed_calls_scope", "observed_calls_scope_id",
      "metering_coverage", "metering_exclusions", "metering_evidence_files",
      "first_iteration", "first_verdict", "final_iteration", "final_verdict", "rounds", "prompt_revisions",
      "grade_verdict", "grade_method", "grade_reference", "grade_evaluator",
      "teaching_rounds", "generation_seconds", "tokens", "human_review", "record_link"];
    const cell = value => {
      let text = typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? "");
      if (/^[\s]*[=+\-@]/.test(text) || /^[\t\r\n]/.test(text)) text = "'" + text;
      return '"' + text.replace(/"/g, '""') + '"';
    };
    return "\ufeff" + [fields, ...rows.map(r => fields.map(f => r[f]))].map(row => row.map(cell).join(",")).join("\r\n");
  }
  return {options, summarize, select, groups, csv, unique};
})();
if (typeof module !== "undefined") module.exports = IndexMetrics;
if (typeof document !== "undefined") (() => {
  const data = JSON.parse(document.getElementById("index-data").textContent);
  const byId = id => document.getElementById(id);
  const el = (tag, text, attrs = {}) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    Object.entries(attrs).forEach(([k, v]) => item.setAttribute(k, v));
    return item;
  };
  const controls = {};
  const all = data.samples;
  const {unique, select, summarize, groups} = IndexMetrics;
  const labelFor = r => r.question_label || data.question_label_map?.[r.question_id] || r.question_id;
  const topicFor = questionId => data.question_topics?.[questionId] || "未分类";
  function renderOverview() {
    const labels = data.question_label_map || {};
    const labelEntries = Object.entries(labels);
    const cards = [
      ["MATH题目", `${labelEntries.length} 道`, "按 MATH1、MATH2… 查找"],
      ["独立运行", `${all.length} 次`, "同一道题可有多次架构测试"],
      ["测试架构", "A / B / C", "Rethlas / DeepTutor / 组合"],
      ["人工评价", `${all.filter(r => r.human_review === "已填写").length} 份`, "空白表示仍待填写"]
    ];
    byId("overview-cards").replaceChildren(...cards.map(([title, value, note]) => {
      const card = el("div", undefined, {class: "overview-card"});
      card.append(el("strong", value), el("span", title), el("small", note));
      return card;
    }));
    const batches = data.test_sets || [];
    byId("batch-summary").replaceChildren(...batches.map(set => {
      const item = el("div", undefined, {class: "batch-item"});
      item.append(el("strong", set.name), el("span", `${set.question_ids.length} 道题 · ${set.ai_ids.length} 个架构／工作流`));
      return item;
    }));
    const mapRows = labelEntries.map(([questionId, label]) => [label, topicFor(questionId)]);
    table(byId("question-map"), ["显示编号", "题型"], mapRows);
  }
  function dropdown(id, title, values) {
    const label = el("label", title);
    const input = el("select", undefined, {id});
    input.append(el("option", "全部", {value: ""}));
    values.forEach(v => input.append(el("option", v.label ?? v, {value: v.value ?? v})));
    label.append(input); byId("filters").append(label); controls[id] = input;
    input.addEventListener("change", update);
  }
  const setNames = new Map(data.test_sets.map(s => [s.test_set_id, s.name]));
  const setIds = unique([...setNames.keys(), ...all.map(r => r.test_set_id).filter(Boolean)]);
  dropdown("testSet", "测试集", [...setIds.map(id => ({value: id, label: `${setNames.get(id) || id} · ${id}`})),
    {value: "__unassigned__", label: "未分组（历史记录）"}]);
  dropdown("ai", "AI／工作流", unique([...all.map(r => r.ai_id), ...data.test_sets.flatMap(s => s.ai_ids)]).sort());
  dropdown("model", "模型（实际或已注明的配置）", unique(all.map(r => r.model)).sort());
  dropdown("tag", "学科／标签", unique(all.flatMap(r => r.tags.length ? r.tags : ["未分类"])).sort());
  dropdown("category", "评测类型", unique(all.flatMap(r => r.categories.length ? r.categories : ["未记录"])).sort());
  dropdown("status", "运行状态", unique(all.map(r => r.status)).sort());
  dropdown("verdict", "末轮回答核验", unique(all.map(r => r.final_verdict)).sort());
  dropdown("protocol", "共同外审协议", unique(all.map(r => r.external_protocol).filter(Boolean)).sort());
  dropdown("rubric", "共同外审量表", unique(all.map(r => r.external_rubric).filter(Boolean)).sort());
  dropdown("system", "系统编号", unique(all.map(r => r.system_id)).sort());
  dropdown("budget", "预算条件", unique(all.map(r => r.budget_condition)).sort());
  const searchLabel = el("label", "搜索题目／测试编号");
  controls.search = el("input", undefined, {id: "search", type: "search", placeholder: "输入编号、模型或标签"});
  searchLabel.append(controls.search); byId("filters").append(searchLabel);
  controls.search.addEventListener("input", update);
  const key = "education-index-metrics:" + location.pathname;
  let saved;
  try {
    const value = JSON.parse(localStorage.getItem(key));
    // Version metric preferences so the new core accuracy options appear for existing readers.
    if (value?.version === 4 && Array.isArray(value.selected)) saved = value.selected;
  } catch (_) { /* file/private mode */ }
  const checks = new Map();
  IndexMetrics.options.forEach(([id, title, defaultOn]) => {
    const label = el("label");
    const input = el("input", undefined, {type: "checkbox", value: id});
    input.checked = saved ? saved.includes(id) : defaultOn;
    checks.set(id, input); label.append(input, document.createTextNode(title));
    byId("metric-options").append(label);
    input.addEventListener("change", () => {
      try { localStorage.setItem(key, JSON.stringify({version: 4, selected: [...checks].filter(([, c]) => c.checked).map(([id]) => id)})); } catch (_) { /* optional persistence */ }
      update();
    });
  });
  function table(container, headers, rows) {
    const t = el("table"), head = el("thead"), tr = el("tr");
    headers.forEach(h => tr.append(el("th", h, {scope: "col"}))); head.append(tr); t.append(head);
    const body = el("tbody");
    rows.forEach(row => {
      const line = el("tr");
      row.forEach(value => { const td = el("td"); td.append(value instanceof Node ? value : document.createTextNode(String(value))); line.append(td); });
      body.append(line);
    });
    t.append(body); container.replaceChildren(t);
  }
  function state() { return Object.fromEntries(Object.entries(controls).map(([k, c]) => [k, k === "search" ? c.value.trim() : c.value])); }
  const runtimeText = r => {
    const parts = [r.observed_seconds_scope, r.observed_tokens_scope, r.observed_calls_scope,
      ...(r.metering_exclusions || [])].filter(Boolean);
    return unique(parts).join("；") || "计量范围未记录，缺失不按0处理。";
  };
  const runtimeValue = (r, field, label) => r[label] || (r[field] == null ? "未记录（见计量说明）" : String(r[field]));
  function update() {
    const filters = state(), rows = select(all, filters);
    const plan = data.test_sets.find(s => s.test_set_id === filters.testSet);
    const selected = IndexMetrics.options.filter(([id]) => checks.get(id).checked);
    const totals = summarize(rows, plan), grouped = groups(rows, plan, filters);
    const intersection = grouped.length ? grouped.map(g => unique(g.rows.map(r => r.question_id)))
      .reduce((a, b) => a.filter(q => b.includes(q))) : [];
    byId("selection").textContent = `当前筛选：${rows.length} 个独立运行，${totals.questions} 道题，${grouped.length} 个模型／工作流组；各组共同有记录的题目 ${intersection.length} 道。`;
    table(byId("runtime-results"), ["题目／测试编号", "AI／工作流", "模型", "主阶段耗时", "已观测 Token", "已观测调用", "实际返回模型", "配置模型", "计量范围／未测部分", "记录"], rows.map(r => [
      labelFor(r) + " / " + r.run_id, r.ai_id, r.model,
      runtimeValue(r, "observed_seconds", "observed_seconds_label"), runtimeValue(r, "observed_tokens", "observed_tokens_label"),
      runtimeValue(r, "observed_calls", "observed_calls_label"), r.actual_model || "未记录", r.configured_model || "未记录", runtimeText(r), el("a", "打开记录", {href: r.record_link})]));
    if (!rows.length) byId("runtime-results").append(el("p", "没有符合筛选条件的实验。"));
    byId("cards").replaceChildren(...selected.map(([id, title]) => {
      const card = el("div", title, {class: "metric-card"}); card.append(el("strong", totals[id], {class: id.startsWith("observed_") ? "scoped-metric" : ""})); return card;
    }));
    table(byId("comparison"), ["AI／工作流", "记录中的模型", "外审协议", "量表", "预算条件", "独立运行", "去重题数", ...selected.map(([, title]) => title)],
      grouped.map(g => { const s = summarize(g.rows, plan); return [g.ai, g.model, g.protocol, g.rubric, g.budget, s.n, s.questions, ...selected.map(([id]) => s[id])]; }));
    const qualityText = value => value?.status === "evaluated" ? value.items.map(x => `${x.criterion} ${x.score}/${x.max_score}：${x.reason}；证据：${x.evidence}`).join("；") : value?.status === "not_evaluated" ? "未评测" : "待评";
    table(byId("dimension-results"), ["题目／运行", "数学正确性（共同外审）", "教学质量", "互动能力", "运行可靠性", "成本与效率", "材料／角色／预算"], rows.map(r => [
      labelFor(r) + " / " + r.run_id, r.external_verdict + "；人工抽查：" + r.human_audit_status,
      qualityText(r.external_dimensions.teaching_quality), qualityText(r.external_dimensions.interaction), r.status,
      `${runtimeValue(r, "observed_seconds", "observed_seconds_label")}；${runtimeValue(r, "observed_tokens", "observed_tokens_label")}；${runtimeText(r)}`,
      el("a", "评测详情", {href: r.details_link})]));
    const missing = byId("missing"); missing.replaceChildren();
    if (plan?.question_ids.length) grouped.forEach(g => {
      const stats = summarize(g.rows, plan), d = el("details"), title = `${g.ai} / ${g.model}：当前筛选未覆盖 ${stats.missing.length} 题`;
      d.append(el("summary", title), el("p", stats.missing.map(q => labelFor({question_id: q})).join("、") || "清单内题目均有运行记录。"));
      if (stats.outside.length) d.append(el("p", "清单外题目（不计覆盖）：" + stats.outside.map(q => labelFor({question_id: q})).join("、")));
      missing.append(d);
    });
    const extra = [["duration", "generation_seconds", "生成秒数"], ["tokens", "tokens", "Token"]].filter(([id]) => checks.get(id).checked);
    table(byId("samples"), ["题目", "测试集", "标签", "AI／工作流", "模型", "测试编号", "运行状态", "流程证据：首稿核验", "流程证据：末轮核验", "历史 grading（非共同外审）", "判分方法", "回答轮数", "提示迭代", "教学稿", "人工评价", ...extra.map(([, , title]) => title), "记录与评价"],
      rows.map(r => {
        const links = el("span");
        [["打开", r.record_link], ["原文", r.source_link], ["评价", r.review_link], ["评测详情", r.details_link],
          ...(r.supplement_links || []).map((link, n) => [`补充${n + 1}`, link])].forEach(([text, href]) => links.append(el("a", text, {href})));
        return [labelFor(r), setNames.get(r.test_set_id) || r.test_set_id || "未分组", r.tags.join("、") || "未分类", r.ai_id, r.model, r.run_id,
          r.status, r.first_verdict, r.final_verdict, r.grade_verdict === "not_graded" ? "待判分" : r.grade_verdict,
          r.grade_method, r.answers, r.prompt_revisions, r.teaching_rounds, r.human_review,
          ...extra.map(([, field]) => r[field] ?? "未记录"), links];
      }));
    if (!rows.length) byId("samples").append(el("p", "没有符合筛选条件的实验。"));
  }
  renderOverview();
  byId("reset").addEventListener("click", () => {
    Object.values(controls).forEach(c => { c.value = ""; });
    IndexMetrics.options.forEach(([id, , on]) => { checks.get(id).checked = on; });
    try { localStorage.removeItem(key); } catch (_) { /* optional persistence */ }
    update();
  });
  byId("export").addEventListener("click", () => {
    const url = URL.createObjectURL(new Blob([IndexMetrics.csv(select(all, state()))], {type: "text/csv;charset=utf-8"}));
    const a = el("a", "", {href: url, download: "math_evaluation_samples.csv"}); document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  update();
  byId("dashboard").hidden = false;
  byId("static-index").hidden = true;
})();
