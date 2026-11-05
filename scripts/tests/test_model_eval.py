"""模型与工作流评测接入（scripts/model_eval）的离线测试。

全部测试只使用临时项目根、合成夹具与 mock：
- 不调用任何模型或网络服务；
- 不启动真实 dsh headless（detect_install 与 subprocess 均被 mock）；
- 临时项目根建在 work/test-records/ 下，测试结束自动删除。

实现文件 scripts/model_eval/*.py 与 scripts/experiment_records.py 只读。
"""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch
from urllib.parse import unquote
import zipfile

_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS))

from model_eval import cli, dsh, manifest, spec  # noqa: E402
from model_eval.spec import EvalSpecError  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STAGING = PROJECT_ROOT / "work" / "test-records"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_eval"

TEACHING_SPEC = "evaluations/specs/teaching-comparison.example.json"
SOLVING_SPEC = "evaluations/specs/solving-comparison.example.json"
PIPELINE_SPEC = "evaluations/specs/pipeline-comparison.example.json"
TEACHING_PROMPT_REL = "evaluations/inputs/example/teaching-prompt.md"
TEACHING_QUESTION_REL = "evaluations/inputs/example/question.md"
TEACHING_USER_REQUEST_REL = "evaluations/inputs/example/user-request.md"
SOLVING_VERIFIER = "independent-verifier-example"
TEACHING_VERIFIER = "example-reviewer"

RUN_ID_PATTERN = re.compile(r"\d{8}T\d{6}Z_[0-9a-f]{8}\Z")


def sha256(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


class ModelEvalTestCase(unittest.TestCase):
    """临时项目根：复制示例材料，保持 record_manifest 需要的项目内相对路径。"""

    def setUp(self):
        STAGING.mkdir(parents=True, exist_ok=True)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="model-eval-", dir=str(STAGING)))
        self.root = Path(temporary).resolve(strict=True)
        for relative in ("evaluations/inputs/example", "evaluations/specs",
                         "prompt-engineering/prompts", "prompt-engineering/references"):
            shutil.copytree(PROJECT_ROOT / relative, self.root / relative)
        self.docs = self.root / "docs"
        self.work = self.root / "work"
        self.fixtures_dir = self.root / "fixtures"
        self.fixtures_dir.mkdir()
        self.prompt_text = (self.root / TEACHING_PROMPT_REL).read_text(encoding="utf-8")

    # ---- 通用工具 ---------------------------------------------------------
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def cli_json(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, f"命令行应成功，实际 stderr={err!r} stdout={out!r}")
        return json.loads(out)

    def cli_failure(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 2, f"命令行应返回 2，实际 stdout={out!r} stderr={err!r}")
        self.assertIn("评测接入失败：", err)
        return err

    def snapshot_files(self):
        return sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))

    def write_text(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" 保证写入后再读取的原始回答与测试输入逐字节一致。
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def write_json(self, relative, data):
        return self.write_text(relative, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def read_json(self, relative_or_path):
        path = relative_or_path if isinstance(relative_or_path, Path) else self.root / relative_or_path
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def load_spec_json(self, relative):
        return self.read_json(relative)

    def teaching_spec_variant(self, name, mutate=None, batch_id=None):
        data = self.load_spec_json(TEACHING_SPEC)
        if batch_id is not None:
            data["batch"]["batch_id"] = batch_id
        if mutate is not None:
            mutate(data)
        relative = "evaluations/specs/" + name
        self.write_json(relative, data)
        return relative

    def solving_spec_variant(self, name, mutate=None, batch_id=None):
        data = self.load_spec_json(SOLVING_SPEC)
        if batch_id is not None:
            data["batch"]["batch_id"] = batch_id
        if mutate is not None:
            mutate(data)
        relative = "evaluations/specs/" + name
        self.write_json(relative, data)
        return relative

    def metadata(self, **overrides):
        data = {
            "status": "completed",
            "actual_model": "fixture-model-a",
            "provider": "fixture-provider",
            "framework": "fixture-framework",
            "dsh_version": "未记录",
            "tool_permissions": "未记录",
            "generation": {"temperature": 0.2},
            "timing": {"duration_seconds": 12.5},
            "usage": {"input_tokens": 10, "output_tokens": 20, "cost": "未记录"},
            "tool_events": [],
            "error": None,
            "notes": "",
            "round": {"iteration_id": "teaching-initial", "type": "initial",
                      "change_reason": "独立初稿"},
        }
        data.update(overrides)
        return data

    def verification(self, name, *, verifier_id, verdict="correct",
                     round_id="teaching-initial", findings=None):
        data = {
            "round_id": round_id,
            "verifier": {
                "verifier_id": verifier_id,
                "name": "测试独立核验者",
                "kind": "model",
                "model": "fixture-verifier-model",
                "provider": "未记录",
                "framework": "未记录",
                "notes": "",
            },
            "verdict": verdict,
            "summary": "测试核验摘要",
            "findings": findings or [],
        }
        return self.write_json("artifacts/" + name, data)

    def import_cli(self, spec_relative, candidate, response_path, metadata_path, *extra):
        return self.cli_json(
            "import", "--project-root", str(self.root), "--spec", spec_relative,
            "--candidate", candidate, "--response", str(response_path),
            "--metadata", str(metadata_path), *extra)

    def record_paths(self):
        experiments = self.docs / "experiments"
        return sorted(experiments.rglob("record.json")) if experiments.is_dir() else []

    def only_record(self):
        paths = self.record_paths()
        self.assertEqual(len(paths), 1, f"应只有一条实验记录：{paths}")
        return paths[0]

    def record_of(self, result):
        return self.read_json(Path(result["record_dir"]) / "record.json")

    # ---- DSH 导出 ZIP 夹具 ------------------------------------------------
    def session_export_text(self, *, assistant=True, user_messages=1):
        template = json.loads((FIXTURES / "session_v4_events.json").read_text(encoding="utf-8"))
        events = []
        for event in template:
            if event["type"] == "assistant/message" and not assistant:
                continue
            if event["type"] == "user/message":
                for _ in range(user_messages):
                    clone = copy.deepcopy(event)
                    clone["data"]["content"][0]["text"] = self.prompt_text
                    events.append(clone)
                continue
            events.append(copy.deepcopy(event))
        return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)

    def session_export_text_with_id(self, session_id, parent_session=None):
        events = [json.loads(line) for line in self.session_export_text().splitlines()]
        for event in events:
            if event["type"] == "session":
                event["id"] = session_id
                event.pop("parentSession", None)
                if parent_session is not None:
                    event["parentSession"] = parent_session
        return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)

    def write_export_zip(self, name, members):
        path = self.fixtures_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            for member, text in members.items():
                archive.writestr(member, text.encode("utf-8"))
        return path

    def build_session_export(self, *, assistant=True, user_messages=1,
                             name="session-export.zip"):
        session_text = self.session_export_text(assistant=assistant,
                                                user_messages=user_messages)
        path = self.write_export_zip(name, {"session.v4.jsonl": session_text})
        return path, session_text

    def import_export(self, spec_relative, candidate, archive_path, *extra):
        return self.cli_json(
            "import", "--project-root", str(self.root), "--spec", spec_relative,
            "--candidate", candidate, "--dsh-export", str(archive_path), *extra)


class SpecValidationTests(ModelEvalTestCase):
    """第 10 类（规格校验）与核验者身份规则。"""

    def prepare_failure(self, spec_relative):
        return self.cli_failure("prepare", "--project-root", str(self.root),
                                "--spec", spec_relative, "--dry-run")

    def test_all_example_specs_load_with_expected_categories_and_candidates(self):
        for relative, category in ((TEACHING_SPEC, "teaching_comparison"),
                                   (SOLVING_SPEC, "solving_comparison"),
                                   (PIPELINE_SPEC, "pipeline_comparison")):
            with self.subTest(spec=relative):
                loaded = spec.load_spec(relative, self.root)
                self.assertEqual(loaded["category"], category)
                self.assertEqual(len(loaded["candidates"]), 2)
                self.assertTrue(loaded["question"]["text"].strip())
                self.assertTrue(loaded["prompt"]["text"].strip())
                self.assertEqual(loaded["verification_plan"]["required"], True)
        pipeline = spec.load_spec(PIPELINE_SPEC, self.root)
        rethlas = pipeline["candidates"][0]
        self.assertTrue(rethlas["tools"])
        self.assertTrue(rethlas["organization"])

    def test_workbench_name_as_configured_model_is_rejected(self):
        for value in ("dsh", "DSH", "codex", "Rethlas", "deepseek harness"):
            with self.subTest(configured_model=value):
                def mutate(data, configured_model=value):
                    data["candidates"][0]["configured_model"] = configured_model
                relative = self.teaching_spec_variant("bad-model.json", mutate)
                error = self.prepare_failure(relative)
                self.assertIn("工作台/框架名称当模型名", error)

    def test_configured_model_equal_to_framework_is_rejected(self):
        def mutate(data):
            data["candidates"][0]["configured_model"] = data["candidates"][0]["framework"]
        relative = self.teaching_spec_variant("bad-framework-model.json", mutate)
        error = self.prepare_failure(relative)
        self.assertIn("不是模型名", error)

    def test_duplicate_ai_id_is_rejected(self):
        def mutate(data):
            data["candidates"][1]["ai_id"] = data["candidates"][0]["ai_id"]
        relative = self.teaching_spec_variant("duplicate-ai-id.json", mutate)
        error = self.prepare_failure(relative)
        self.assertIn("ai_id 重复", error)

    def test_independent_false_is_rejected(self):
        def mutate(data):
            data["design"]["independent_from_other_models"] = False
        relative = self.teaching_spec_variant("not-independent.json", mutate)
        error = self.prepare_failure(relative)
        self.assertIn("independent_from_other_models 必须为 true", error)

    def test_priority_old_answer_materials_are_rejected(self):
        for banned in ("prior_teaching_outputs", "prior_answers"):
            with self.subTest(banned=banned):
                def mutate(data, key=banned):
                    data["materials"][key] = ["evaluations/inputs/example/verified-proof.md"]
                relative = self.teaching_spec_variant("prior-answers.json", mutate)
                error = self.prepare_failure(relative)
                self.assertIn("不能用于独立比较", error)

    def test_solving_spec_without_verifier_is_rejected(self):
        def mutate(data):
            data["verification_plan"].pop("verifier", None)
        relative = self.solving_spec_variant("solving-no-verifier.json", mutate)
        error = self.prepare_failure(relative)
        self.assertIn("必须明确指定独立核验者", error)

    def test_verifier_identity_equal_to_candidate_is_rejected_at_spec_load(self):
        def mutate(data):
            data["verification_plan"]["verifier"]["verifier_id"] = \
                data["candidates"][0]["ai_id"]
        relative = self.teaching_spec_variant("verifier-conflict.json", mutate)
        error = self.prepare_failure(relative)
        self.assertIn("核验者身份必须与受测模型/工作流分开", error)

    def test_metadata_status_secret_and_verification_identity_rules(self):
        response = self.write_text("artifacts/answer.md", "测试回答。\n")
        metadata_path = self.write_json(
            "artifacts/metadata-secret.json",
            {**self.metadata(), "api_key": "sk-should-not-be-imported"})
        with self.assertRaises(EvalSpecError) as caught:
            manifest.import_candidate(
                self.root, TEACHING_SPEC, "dsh",
                response_path=str(response), metadata_path=str(metadata_path))
        self.assertIn("疑似凭据字段", str(caught.exception))

        metadata_path = self.write_json(
            "artifacts/metadata-status.json", self.metadata(status="success"))
        with self.assertRaises(EvalSpecError) as caught:
            manifest.import_candidate(
                self.root, TEACHING_SPEC, "dsh",
                response_path=str(response), metadata_path=str(metadata_path))
        self.assertIn("status 必须是", str(caught.exception))
        self.assertFalse(self.docs.exists())


class PrepareAndDryRunTests(ModelEvalTestCase):
    """第 1 类：prepare/import 的 dry-run 不调用模型、不写记录、不建索引。"""

    def test_prepare_dry_run_writes_nothing_and_calls_no_model(self):
        before = self.snapshot_files()
        with patch("model_eval.dsh.detect_install") as detect:
            preview = self.cli_json("prepare", "--project-root", str(self.root),
                                    "--spec", TEACHING_SPEC, "--dry-run")
        detect.assert_not_called()
        self.assertTrue(preview["dry_run"])
        self.assertFalse(preview["wrote"])
        self.assertEqual(preview["candidates"][0]["run_id"], "预览（未保存）")
        self.assertEqual(preview["category"], "teaching_comparison")
        self.assertIn("未调用模型", preview["notice"])
        self.assertEqual(before, self.snapshot_files())
        self.assertFalse(self.docs.exists())
        self.assertFalse(self.work.exists())

    def test_prepare_stages_task_packages_without_records_or_index(self):
        with patch("model_eval.dsh.detect_install") as detect:
            prepared = self.cli_json("prepare", "--project-root", str(self.root),
                                     "--spec", TEACHING_SPEC)
        detect.assert_not_called()
        self.assertFalse(prepared["dry_run"])
        self.assertTrue(prepared["wrote"])
        self.assertEqual(prepared["directory"], "work/evaluations/example-teaching-comparison")
        self.assertEqual(prepared["plan"], "work/evaluations/example-teaching-comparison/plan.json")
        for candidate in prepared["candidates"]:
            self.assertRegex(candidate["run_id"], RUN_ID_PATTERN)
            self.assertTrue((self.root / candidate["directory"] / "import-template.json").is_file())
            self.assertTrue((self.root / candidate["prompt"]).is_file())
        plan = self.read_json(prepared["plan"])
        self.assertEqual(set(plan["candidates"]), {"dsh", "other-model"})
        readme = (self.root / prepared["candidates"][0]["readme"]).read_text(encoding="utf-8")
        self.assertIn("未调用任何模型", readme)
        self.assertIn("未调用任何模型", prepared["notice"])
        self.assertFalse(self.docs.exists())

    def test_import_dry_run_writes_nothing_and_hashes_actual_answer(self):
        answer = "dry-run 原始回答。\n"
        response = self.write_text("artifacts/response-dry.md", answer)
        metadata_path = self.write_json("artifacts/metadata-dry.json", self.metadata())
        before = self.snapshot_files()
        with patch("model_eval.dsh.detect_install") as detect:
            preview = self.cli_json(
                "import", "--project-root", str(self.root), "--spec", TEACHING_SPEC,
                "--candidate", "dsh", "--response", str(response),
                "--metadata", str(metadata_path), "--dry-run")
        detect.assert_not_called()
        self.assertTrue(preview["dry_run"])
        self.assertFalse(preview["wrote"])
        self.assertEqual(preview["run_id"], "预览（未保存）")
        self.assertEqual(preview["response_sha256"], sha256(answer))
        self.assertIn("未创建实验记录", preview["notice"])
        self.assertEqual(before, self.snapshot_files())
        self.assertFalse(self.docs.exists())
        self.assertFalse(self.work.exists())


class ManualImportTests(ModelEvalTestCase):
    """第 2、3、4、5、12 类：手动导入、追加、失败状态、核验者分离与人工评价。"""

    def test_shared_evaluation_design_and_actual_role_usage_survive_import(self):
        design = {"system_id": "A", "task_materials": {"independent_sessions": True},
                  "external_review_plan": {"protocol_id": "shared-v1", "rubric_id": "rubric-v1"}}
        spec_path = self.teaching_spec_variant("shared.json", lambda d: d.update(evaluation=design))
        response = self.write_text("artifacts/shared-answer.md", "原始回答")
        actual = {"roles": [{"role_id": "solver", "actual_model": "observed", "calls": 2}],
                  "usage": {"calls": 2, "total_tokens": 30, "wall_seconds": 12.5}}
        metadata = self.write_json("artifacts/shared-meta.json", self.metadata(evaluation=actual))
        result = self.import_cli(spec_path, "dsh", response, metadata)
        record = self.record_of(result)
        self.assertEqual(record["evaluation"]["external_review_plan"]["protocol_id"], "shared-v1")
        self.assertEqual(record["evaluation"]["roles"], actual["roles"])
        self.assertFalse(record.get("external_reviews"))

    def test_import_preserves_test_set_and_batch_identity_for_index(self):
        spec = self.teaching_spec_variant("suite.json", lambda d: d["batch"].update(test_set_id="suite-v1"))
        response = self.write_text("artifacts/answer.md", "原始回答")
        metadata = self.write_json("artifacts/meta.json", self.metadata())
        result = self.import_cli(spec, "dsh", response, metadata)
        record = self.record_of(result)
        self.assertEqual(record["test_set_id"], "suite-v1")
        self.assertEqual(record["iterations"][0]["settings"]["batch_id"], "example-teaching-comparison")
        html = Path(result["index_reading_path"]).read_text(encoding="utf-8")
        self.assertIn('"test_set_id": "suite-v1"', html)

    def test_manual_import_saves_prompt_answer_metadata_and_evidence_hashes(self):
        answer = "讲解：先配方，\\(x^2-2x+2=(x-1)^2+1>0\\)。完整原始回答。\n"
        response = self.write_text("artifacts/response-a.md", answer)
        metadata_path = self.write_json("artifacts/metadata-a.json", self.metadata())
        result = self.import_cli(TEACHING_SPEC, "dsh", response, metadata_path)

        self.assertEqual(result["candidate_id"], "dsh")
        self.assertEqual(result["ai_id"], "dsh-example-teaching")
        self.assertRegex(result["run_id"], RUN_ID_PATTERN)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["actual_model"], "fixture-model-a")
        self.assertEqual(result["verification_verdict"], "not_run")
        self.assertEqual(result["prompt_source"], "spec_prompt_copy")

        run_dir = Path(result["record_dir"])
        self.assertEqual(Path(result["record_path"]), run_dir / "测试记录.md")
        self.assertEqual(Path(result["reading_path"]), run_dir / "测试记录.html")
        self.assertTrue(Path(result["reading_path"]).is_file())
        self.assertTrue(Path(result["index_reading_path"]).is_file())
        record = self.record_of(result)

        self.assertEqual(record["question_id"], "example-q01")
        self.assertEqual(record["ai_id"], "dsh-example-teaching")
        self.assertEqual(record["run_id"], result["run_id"])
        self.assertEqual(record["model"], "fixture-model-a")
        self.assertEqual(record["framework"], "DSH（DeepSeek Harness）工作台")
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["question"],
                         (self.root / TEACHING_QUESTION_REL).read_text(encoding="utf-8"))
        self.assertEqual(record["user_prompt"],
                         (self.root / TEACHING_USER_REQUEST_REL).read_text(encoding="utf-8"))
        self.assertIn("学生背景", record["requirements"])
        self.assertIn("教学要求", record["requirements"])

        item = record["iterations"][0]
        self.assertEqual(item["iteration_id"], "teaching-initial")
        self.assertEqual(item["prompt"], self.prompt_text)
        self.assertEqual(item["answer"], answer)
        settings = item["settings"]
        self.assertEqual(settings["actual_model"], "fixture-model-a")
        self.assertEqual(settings["actual_model_source"], "actual_model")
        self.assertEqual(settings["actual_provider"], "fixture-provider")
        self.assertEqual(settings["configured_model"], "未记录")
        self.assertEqual(settings["evaluation_category"], "teaching_comparison")
        self.assertEqual(settings["materials"]["prompt"], sha256(self.prompt_text))
        self.assertEqual(settings["materials"]["question"],
                         sha256((self.root / TEACHING_QUESTION_REL).read_bytes()))
        self.assertTrue((self.root / settings["staged_metadata"]).is_file())
        self.assertEqual(settings["round"]["iteration_id"], "teaching-initial")

        # 原始回答、规格与完整实际输入都按内容哈希保存为证据。
        artifacts = {entry["sha256"]: entry for entry in record["artifacts"]}
        answer_sha = sha256(answer)
        self.assertIn(answer_sha, artifacts)
        self.assertEqual((run_dir / artifacts[answer_sha]["snapshot"]).read_text(encoding="utf-8"),
                         answer)
        spec_bytes = (self.root / TEACHING_SPEC).read_bytes()
        self.assertIn(sha256(spec_bytes), artifacts)
        self.assertTrue(any(entry["source"] == "evaluations/specs/teaching-comparison.example.json"
                            for entry in record["artifacts"]))

        self.assertEqual(item["verification"][0]["verdict"], "not_run")
        self.assertNotEqual(item["verification"][0]["verdict"], "correct")

    def test_different_models_get_independent_ai_ids_and_run_ids(self):
        response_a = self.write_text("artifacts/response-model-a.md", "模型 A 的讲解。\n")
        metadata_a = self.write_json(
            "artifacts/metadata-model-a.json", self.metadata(actual_model="actual-model-a"))
        result_a = self.import_cli(TEACHING_SPEC, "dsh", response_a, metadata_a)

        response_b = self.write_text("artifacts/response-model-b.md", "模型 B 的讲解。\n")
        metadata_b = self.write_json(
            "artifacts/metadata-model-b.json", self.metadata(actual_model="actual-model-b"))
        result_b = self.import_cli(TEACHING_SPEC, "other-model", response_b, metadata_b)

        self.assertNotEqual(result_a["ai_id"], result_b["ai_id"])
        self.assertNotEqual(result_a["run_id"], result_b["run_id"])
        self.assertRegex(result_a["run_id"], RUN_ID_PATTERN)
        self.assertRegex(result_b["run_id"], RUN_ID_PATTERN)
        self.assertEqual(result_a["ai_id"], "dsh-example-teaching")
        self.assertEqual(result_b["ai_id"], "other-model-example-teaching")
        record_a, record_b = self.record_of(result_a), self.record_of(result_b)
        self.assertEqual(record_a["question_id"], record_b["question_id"])
        self.assertNotEqual(record_a["run_id"], record_b["run_id"])
        self.assertNotEqual(record_a["model"], record_b["model"])
        self.assertEqual(len(self.record_paths()), 2)
        index = (self.docs / "实验索引.md").read_text(encoding="utf-8")
        self.assertEqual(index.count("[打开]"), 2)

    def test_same_candidate_reimport_keeps_run_id_and_appends_iteration(self):
        first_response = self.write_text("artifacts/first.md", "第一份独立讲解。\n")
        first_metadata = self.write_json("artifacts/first.json", self.metadata())
        first = self.import_cli(TEACHING_SPEC, "dsh", first_response, first_metadata)

        second_metadata = self.write_json(
            "artifacts/second.json",
            self.metadata(round={"iteration_id": "teaching-second", "type": "initial",
                                 "change_reason": "第二份独立讲解"}))
        second_response = self.write_text("artifacts/second.md", "第二份独立讲解。\n")
        second = self.import_cli(TEACHING_SPEC, "dsh", second_response, second_metadata)

        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(second["record_dir"], first["record_dir"])
        record = self.record_of(first)
        self.assertEqual([item["iteration_id"] for item in record["iterations"]],
                         ["teaching-initial", "teaching-second"])
        self.assertEqual(record["iterations"][0]["answer"], "第一份独立讲解。\n")
        self.assertEqual(record["iterations"][1]["answer"], "第二份独立讲解。\n")
        self.assertEqual(len(self.record_paths()), 1)

    def test_reimport_same_iteration_different_answer_is_rejected_and_keeps_original(self):
        original = "原始首稿回答，不能被改写。\n"
        first_response = self.write_text("artifacts/original.md", original)
        first_metadata = self.write_json("artifacts/original.json", self.metadata())
        first = self.import_cli(TEACHING_SPEC, "dsh", first_response, first_metadata)
        record_path = Path(first["record_dir"]) / "record.json"
        before = record_path.read_bytes()

        rewritten = self.write_text("artifacts/rewritten.md", "改写后的回答。\n")
        rewritten_metadata = self.write_json("artifacts/rewritten.json", self.metadata())
        error = self.cli_failure(
            "import", "--project-root", str(self.root), "--spec", TEACHING_SPEC,
            "--candidate", "dsh", "--response", str(rewritten),
            "--metadata", str(rewritten_metadata))

        self.assertIn("Refusing to overwrite original answer", error)
        self.assertEqual(record_path.read_bytes(), before)
        record = self.read_json(record_path)
        self.assertEqual(record["iterations"][0]["answer"], original)
        markdown = Path(first["record_path"]).read_text(encoding="utf-8")
        self.assertIn(original, markdown)
        self.assertNotIn("改写后的回答", markdown)

    def test_revision_append_preserves_first_answer(self):
        first_response = self.write_text("artifacts/initial.md", "首稿讲解。\n")
        first = self.import_cli(TEACHING_SPEC, "dsh", first_response,
                                self.write_json("artifacts/initial.json", self.metadata()))
        revision_metadata = self.write_json(
            "artifacts/revision.json",
            self.metadata(round={"iteration_id": "revision-1",
                                 "type": "verification_revision",
                                 "revision_of": "teaching-initial",
                                 "change_reason": "按核验反馈修订"}))
        revision_response = self.write_text("artifacts/revision.md", "修订稿讲解。\n")
        revision = self.import_cli(TEACHING_SPEC, "dsh", revision_response, revision_metadata)

        self.assertEqual(revision["run_id"], first["run_id"])
        record = self.record_of(first)
        self.assertEqual([item["iteration_id"] for item in record["iterations"]],
                         ["teaching-initial", "revision-1"])
        self.assertEqual(record["iterations"][0]["answer"], "首稿讲解。\n")
        self.assertEqual(record["iterations"][1]["answer"], "修订稿讲解。\n")
        markdown = Path(first["record_path"]).read_text(encoding="utf-8")
        self.assertIn("首稿讲解。", markdown)
        self.assertIn("修订稿讲解。", markdown)
        self.assertEqual(len(record["updates"]), 2)

    def test_completed_without_answer_or_with_empty_answer_is_rejected(self):
        metadata_path = self.write_json("artifacts/completed.json", self.metadata())
        with self.assertRaises(EvalSpecError) as caught:
            manifest.import_candidate(self.root, TEACHING_SPEC, "dsh",
                                      response_path=None, metadata_path=str(metadata_path))
        self.assertIn("没有回答不能记为完成", str(caught.exception))

        empty = self.write_text("artifacts/empty.md", "")
        error = self.cli_failure(
            "import", "--project-root", str(self.root), "--spec", TEACHING_SPEC,
            "--candidate", "dsh", "--response", str(empty),
            "--metadata", str(metadata_path))
        self.assertIn("空回答不能记为", error)
        self.assertFalse(self.docs.exists())

    def test_failed_timeout_unverified_are_not_recorded_as_success(self):
        for status in ("failed", "timeout", "unverified"):
            with self.subTest(status=status):
                spec_relative = self.teaching_spec_variant(
                    f"status-{status}.json", batch_id=f"fixture-status-{status}")
                metadata_path = self.write_json(
                    f"artifacts/metadata-{status}.json",
                    self.metadata(status=status, actual_model="未记录", provider="未记录",
                                  timing={"duration_seconds": 900, "timed_out": status == "timeout"}))
                result = manifest.import_candidate(
                    self.root, spec_relative, "dsh", response_path=None,
                    metadata_path=str(metadata_path))
                record = self.record_of(result)
                self.assertEqual(record["status"], status)
                self.assertNotEqual(record["status"], "completed")
                self.assertTrue(record["error"])
                verdicts = [report["verdict"] for report in record["iterations"][0]["verification"]]
                self.assertEqual(verdicts, ["not_run"])
                self.assertNotIn("correct", verdicts)
                markdown = Path(result["record_path"]).read_text(encoding="utf-8")
                self.assertIn(f"运行状态：{status}", markdown)
                self.assertIn("数学复核：not_run。", markdown)
                self.assertNotIn("数学复核：correct", markdown)
                index = (self.docs / "实验索引.md").read_text(encoding="utf-8")
                self.assertIn(f"| {status} |", index)

    def test_failed_record_cannot_attach_correct_verification(self):
        metadata_path = self.write_json(
            "artifacts/failed.json",
            self.metadata(status="failed", actual_model="未记录", provider="未记录"))
        verification_path = self.verification(
            "verification-correct.json", verifier_id=TEACHING_VERIFIER, verdict="correct")
        with self.assertRaises(EvalSpecError) as caught:
            manifest.import_candidate(
                self.root, TEACHING_SPEC, "dsh", response_path=None,
                metadata_path=str(metadata_path),
                verification_path=str(verification_path))
        self.assertIn("不能附带 correct 核验结论", str(caught.exception))
        self.assertFalse(self.docs.exists())

    def test_verifier_identity_equal_to_candidate_is_rejected_on_verification(self):
        response = self.write_text("artifacts/answer.md", "回答。\n")
        metadata_path = self.write_json("artifacts/metadata.json", self.metadata())
        verification_path = self.verification(
            "verification-self.json", verifier_id="dsh-example-teaching")
        with self.assertRaises(EvalSpecError) as caught:
            manifest.import_candidate(
                self.root, TEACHING_SPEC, "dsh",
                response_path=str(response), metadata_path=str(metadata_path),
                verification_path=str(verification_path))
        self.assertIn("核验者身份必须与受测模型/工作流分开", str(caught.exception))
        self.assertFalse(self.docs.exists())

    def test_valid_verification_import_records_correct_verdict(self):
        response = self.write_text(
            "artifacts/solving-answer.md",
            "解：由 \\(x^2-2x+2=(x-1)^2+1>0\\) 得证。\n")
        metadata_path = self.write_json(
            "artifacts/solving-metadata.json",
            self.metadata(actual_model="actual-model-solving",
                          round={"iteration_id": "solving-initial", "type": "initial",
                                 "change_reason": "独立初稿"}))
        verification_path = self.verification(
            "solving-verification.json", verifier_id=SOLVING_VERIFIER,
            verdict="correct", round_id="solving-initial",
            findings=[{"type": "note", "text": "逐步核验通过。"}])
        result = manifest.import_candidate(
            self.root, SOLVING_SPEC, "codex-direct",
            response_path=str(response), metadata_path=str(metadata_path),
            verification_path=str(verification_path))
        record = self.record_of(result)
        self.assertEqual(record["status"], "completed")
        report = record["iterations"][0]["verification"][0]
        self.assertEqual(report["verdict"], "correct")
        self.assertEqual(report["verifier"]["verifier_id"], SOLVING_VERIFIER)
        self.assertEqual(report["verification_report"]["notes"], ["逐步核验通过。"])
        self.assertEqual(record["iterations"][0]["settings"]["stage"], "solving")
        markdown = Path(result["record_path"]).read_text(encoding="utf-8")
        self.assertIn("数学复核：correct。", markdown)

    def test_verify_subcommand_appends_independent_verdict_only(self):
        response = self.write_text("artifacts/solving-dsh.md", "解：配方即可。\n")
        metadata_path = self.write_json(
            "artifacts/solving-dsh.json",
            self.metadata(round={"iteration_id": "solving-initial", "type": "initial",
                                 "change_reason": "独立初稿"}))
        imported = self.import_cli(SOLVING_SPEC, "dsh", response, metadata_path)
        record_path = Path(imported["record_dir"]) / "record.json"
        self.assertEqual(self.record_of(imported)["iterations"][0]["verification"][0]["verdict"],
                         "not_run")
        verification_path = self.verification(
            "solving-correct.json", verifier_id=SOLVING_VERIFIER,
            verdict="correct", round_id="solving-initial")
        before = record_path.read_bytes()

        preview = self.cli_json(
            "verify", "--project-root", str(self.root), "--spec", SOLVING_SPEC,
            "--candidate", "dsh", "--verification", str(verification_path), "--dry-run")
        self.assertTrue(preview["dry_run"])
        self.assertFalse(preview["wrote"])
        self.assertEqual(preview["verdict"], "correct")
        self.assertEqual(record_path.read_bytes(), before)

        verified = self.cli_json(
            "verify", "--project-root", str(self.root), "--spec", SOLVING_SPEC,
            "--candidate", "dsh", "--verification", str(verification_path))
        self.assertEqual(verified["verdict"], "correct")
        self.assertEqual(verified["verifier_id"], SOLVING_VERIFIER)
        record = self.read_json(record_path)
        self.assertEqual(record["iterations"][0]["verification"][0]["verdict"], "correct")
        markdown = Path(imported["record_path"]).read_text(encoding="utf-8")
        self.assertIn("数学复核：correct。", markdown)

        conflict = self.verification(
            "solving-conflict.json", verifier_id="dsh-example-solving",
            verdict="correct", round_id="solving-initial")
        error = self.cli_failure(
            "verify", "--project-root", str(self.root), "--spec", SOLVING_SPEC,
            "--candidate", "dsh", "--verification", str(conflict))
        self.assertIn("核验者身份必须与受测模型/工作流分开", error)

    def test_verify_rejects_failed_record(self):
        metadata_path = self.write_json(
            "artifacts/solving-failed.json",
            self.metadata(status="failed", actual_model="未记录", provider="未记录",
                          round={"iteration_id": "solving-initial", "type": "initial",
                                 "change_reason": "独立初稿"}))
        imported = manifest.import_candidate(
            self.root, SOLVING_SPEC, "codex-direct", response_path=None,
            metadata_path=str(metadata_path))
        self.assertEqual(self.record_of(imported)["status"], "failed")
        verification_path = self.verification(
            "solving-failed-verification.json", verifier_id=SOLVING_VERIFIER,
            verdict="correct", round_id="solving-initial")
        error = self.cli_failure(
            "verify", "--project-root", str(self.root), "--spec", SOLVING_SPEC,
            "--candidate", "codex-direct", "--verification", str(verification_path))
        self.assertIn("不能把失败/超时记录补充为", error)
        record = self.record_of(imported)
        self.assertEqual(record["iterations"][0]["verification"][0]["verdict"], "not_run")

    def test_annotate_writes_only_user_supplied_reviews_and_rejects_empty(self):
        response = self.write_text("artifacts/annotate.md", "等待人工评价的讲解。\n")
        imported = self.import_cli(TEACHING_SPEC, "dsh", response,
                                   self.write_json("artifacts/annotate.json", self.metadata()))
        run_dir = Path(imported["record_dir"])
        record_path = run_dir / "record.json"
        review_path = run_dir / "人工评价.md"
        self.assertIn("待本人填写", review_path.read_text(encoding="utf-8"))
        record = self.read_json(record_path)
        self.assertNotIn("human_review_supplements", record)

        error = self.cli_failure(
            "annotate", "--project-root", str(self.root), "--spec", TEACHING_SPEC,
            "--candidate", "dsh")
        self.assertIn("补充记录需要 --task-review 或 --human-review", error)

        empty = self.write_text("artifacts/empty-review.txt", "  \n")
        error = self.cli_failure(
            "annotate", "--project-root", str(self.root), "--spec", TEACHING_SPEC,
            "--candidate", "dsh", "--task-review", str(empty))
        self.assertIn("task_review 文件为空", error)
        error = self.cli_failure(
            "annotate", "--project-root", str(self.root), "--spec", TEACHING_SPEC,
            "--candidate", "dsh", "--human-review", str(empty))
        self.assertIn("人工评价文件为空", error)

        task_review = self.write_text(
            "artifacts/task-review.txt",
            "自动检查说明：配方依据清楚，仍需人工确认。\n")
        human_review = self.write_text(
            "artifacts/human-review.txt",
            "用户提供的真实评价值得保留：推导可以更慢一些。\n")
        before = record_path.read_bytes()
        preview = self.cli_json(
            "annotate", "--project-root", str(self.root), "--spec", TEACHING_SPEC,
            "--candidate", "dsh", "--task-review", str(task_review),
            "--human-review", str(human_review), "--dry-run")
        self.assertTrue(preview["dry_run"])
        self.assertIn("task_review", preview["fields"])
        self.assertIn("human_review", preview["fields"])
        self.assertEqual(record_path.read_bytes(), before)

        result = self.cli_json(
            "annotate", "--project-root", str(self.root), "--spec", TEACHING_SPEC,
            "--candidate", "dsh", "--task-review", str(task_review),
            "--human-review", str(human_review))
        self.assertEqual(result["run_id"], imported["run_id"])
        record = self.read_json(record_path)
        self.assertIn("自动检查说明", record["task_review"])
        self.assertEqual(len(record["human_review_supplements"]), 1)
        supplement = run_dir / record["human_review_supplements"][0]
        self.assertEqual(supplement.read_text(encoding="utf-8").strip(),
                         "用户提供的真实评价值得保留：推导可以更慢一些。")
        self.assertIn("待本人填写", review_path.read_text(encoding="utf-8"))
        markdown = Path(imported["record_path"]).read_text(encoding="utf-8")
        self.assertIn("补充评价1", markdown)


class DshExportImportTests(ModelEvalTestCase):
    """第 7 类：DSH Web 导出 ZIP 的手动导入路径（全部为合成夹具）。"""

    def test_export_zip_records_observed_events_as_actual_metadata(self):
        archive, session_text = self.build_session_export()
        result = self.import_export(TEACHING_SPEC, "dsh", archive)
        self.assertEqual(result["source"], "dsh_session_export_zip")
        self.assertEqual(result["actual_model"], "actual-model-x")
        self.assertEqual(result["status"], "completed")
        record = self.record_of(result)
        item = record["iterations"][0]

        self.assertEqual(record["model"], "actual-model-x")
        self.assertEqual(item["answer"],
                         "完整讲解：先配方，\\(x^2-2x+2=(x-1)^2+1>0\\)。因此原不等式对一切实数成立。")
        # assistant/message 中的 reasoning 块不得进入最终回答。
        self.assertNotIn("内部推理", item["answer"])
        # 实际输入取自会话事件，且与规格 prompt 完全一致。
        self.assertEqual(item["prompt"], self.prompt_text)
        settings = item["settings"]
        self.assertEqual(settings["prompt_source"], "session_actual_input")
        self.assertTrue(settings["actual_input_matches_prepared_prompt"])
        self.assertEqual(settings["actual_input_sha256"], sha256(self.prompt_text))
        self.assertEqual(settings["actual_model"], "actual-model-x")
        self.assertEqual(settings["actual_provider"], "provider-y")
        self.assertEqual(settings["dsh_version"], "未记录")
        # 模型/提供方/推理强度来自 config；工具表位于 header.tools（不是 config.tools）。
        self.assertEqual(settings["generation"]["request_headers"][0]["model"], "actual-model-x")
        self.assertEqual(settings["generation"]["request_headers"][0]["provider"], "provider-y")
        self.assertEqual(settings["generation"]["request_headers"][0]["reasoningEffort"], "high")
        self.assertEqual(settings["generation"]["request_headers"][0]["tools"],
                         ["pwsh", "apply_patch"])
        self.assertEqual(settings["generation"]["request_headers"][0]["tool_count"], 2)
        self.assertEqual(settings["generation"]["tools"], ["apply_patch", "pwsh"])
        self.assertEqual(settings["generation"]["sandbox_mode"], "workspace-write")
        self.assertEqual(settings["generation"]["permission_preset"], "workspace-write")
        self.assertEqual(settings["usage"]["total"]["inputTokens"], 10)
        self.assertEqual(settings["usage"]["total"]["outputTokens"], 20)
        self.assertEqual(settings["usage"]["total"]["totalTokens"], 30)
        self.assertEqual(settings["dsh_session"]["session_id"], "session-fixture-model-eval")
        self.assertEqual(settings["dsh_session"]["format_version"], 4)
        self.assertEqual(settings["dsh_session"]["session_origin"], "fixture-origin")
        self.assertEqual(settings["dsh_session"]["sandbox_mode"], "workspace-write")
        self.assertEqual(settings["dsh_session"]["permission_preset"], "workspace-write")
        self.assertIn("pwsh", settings["dsh_session"]["tools"])
        self.assertIn("apply_patch", settings["dsh_session"]["tools"])
        tool_events = item["tool_checks"]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0]["name"], "pwsh")
        self.assertEqual(tool_events[0]["status"], "ok")
        # tool/result 中的 reasoning 块不得被保存为工具结果。
        self.assertEqual(tool_events[0]["result_text"], "ok")
        self.assertNotIn("内部工具推理", tool_events[0]["result_text"])
        # 会话原始 JSONL 作为证据按内容哈希保存。
        artifacts = {entry["sha256"] for entry in record["artifacts"]}
        self.assertIn(sha256(session_text), artifacts)
        self.assertTrue(Path(result["reading_path"]).is_file())

    def test_export_zip_with_explicit_dsh_version_records_it(self):
        archive, _session = self.build_session_export(name="session-versioned.zip")
        spec_relative = self.teaching_spec_variant(
            "teaching-versioned.json", batch_id="fixture-teaching-versioned")
        result = self.import_export(spec_relative, "dsh", archive, "--dsh-version", "0.2.1-rc.2")
        record = self.record_of(result)
        settings = record["iterations"][0]["settings"]
        self.assertEqual(settings["dsh_version"], "0.2.1-rc.2")
        self.assertEqual(record["model"], "actual-model-x")

    def test_export_zip_without_assistant_is_rejected(self):
        archive, _session = self.build_session_export(
            assistant=False, name="session-no-assistant.zip")
        error = self.cli_failure(
            "import", "--project-root", str(self.root), "--spec", TEACHING_SPEC,
            "--candidate", "dsh", "--dsh-export", str(archive))
        self.assertIn("assistant 回答", error)
        self.assertFalse(self.docs.exists())

    def test_export_zip_with_empty_assistant_text_is_rejected(self):
        events = [json.loads(line) for line in self.session_export_text().splitlines()]
        for event in events:
            if event["type"] == "assistant/message":
                event["data"]["message"]["content"] = [{"type": "text", "text": "   "}]
        session_text = "".join(json.dumps(event, ensure_ascii=False) + "\n"
                               for event in events)
        archive = self.write_export_zip(
            "session-empty-assistant.zip", {"session.v4.jsonl": session_text})
        error = self.cli_failure(
            "import", "--project-root", str(self.root), "--spec", TEACHING_SPEC,
            "--candidate", "dsh", "--dsh-export", str(archive))
        self.assertIn("没有最终 assistant 回答", error)
        self.assertFalse(self.docs.exists())

    def test_export_zip_with_two_user_messages_is_rejected(self):
        archive, _session = self.build_session_export(
            user_messages=2, name="session-two-user.zip")
        error = self.cli_failure(
            "import", "--project-root", str(self.root), "--spec", TEACHING_SPEC,
            "--candidate", "dsh", "--dsh-export", str(archive))
        self.assertIn("2 条用户消息", error)
        self.assertFalse(self.docs.exists())

    def test_export_zip_with_multiple_root_sessions_is_rejected(self):
        text = self.session_export_text()
        archive = self.write_export_zip(
            "session-multi-root.zip",
            {"session.v3.jsonl": text, "session.v4.jsonl": text})
        error = self.cli_failure(
            "import", "--project-root", str(self.root), "--spec", TEACHING_SPEC,
            "--candidate", "dsh", "--dsh-export", str(archive))
        self.assertIn("多个根会话日志", error)
        self.assertIn("--dsh-session-id", error)
        self.assertFalse(self.docs.exists())

    def test_export_zip_child_session_is_excluded_by_parent_header(self):
        root_text = self.session_export_text_with_id("session-root-fixture")
        child_text = self.session_export_text_with_id(
            "session-child-fixture", parent_session="session-root-fixture")
        archive = self.write_export_zip("session-watermark-child.zip", {
            "exports/root/session.v4.jsonl": root_text,
            "exports/child/session.v4.jsonl": child_text,
        })
        parsed = dsh.parse_session_export_zip(archive)
        self.assertEqual(parsed["session_id"], "session-root-fixture")
        self.assertEqual(parsed["provenance"]["member"], "exports/root/session.v4.jsonl")
        self.assertEqual(parsed["parent_session"], "未记录")

    def test_ambiguous_export_requires_dsh_session_id(self):
        archive = self.write_export_zip("session-watermark-ambiguous.zip", {
            "exports/run-a/session.v4.jsonl": self.session_export_text_with_id("session-run-a"),
            "exports/run-b/session.v4.jsonl": self.session_export_text_with_id("session-run-b"),
        })
        with self.assertRaises(EvalSpecError) as caught:
            dsh.parse_session_export_zip(archive)
        self.assertIn("请用 --dsh-session-id", str(caught.exception))
        error = self.cli_failure(
            "import", "--project-root", str(self.root), "--spec", TEACHING_SPEC,
            "--candidate", "dsh", "--dsh-export", str(archive))
        self.assertIn("--dsh-session-id", error)
        self.assertFalse(self.docs.exists())
        with self.assertRaises(EvalSpecError) as caught:
            dsh.parse_session_export_zip(archive, session_id="session-missing")
        self.assertIn("没有唯一的 session id", str(caught.exception))

        result = self.import_export(TEACHING_SPEC, "other-model", archive,
                                    "--dsh-session-id", "session-run-b")
        record = self.record_of(result)
        settings = record["iterations"][0]["settings"]
        self.assertEqual(settings["dsh_session"]["session_id"], "session-run-b")
        self.assertEqual(record["model"], "actual-model-x")

    def test_export_zip_dry_run_parses_without_writing(self):
        archive, session_text = self.build_session_export(name="session-dry.zip")
        before = self.snapshot_files()
        preview = self.cli_json(
            "import", "--project-root", str(self.root), "--spec", TEACHING_SPEC,
            "--candidate", "dsh", "--dsh-export", str(archive), "--dry-run")
        self.assertTrue(preview["dry_run"])
        self.assertFalse(preview["wrote"])
        self.assertEqual(preview["source"], "dsh_session_export_zip")
        self.assertEqual(preview["actual_model"], "actual-model-x")
        self.assertEqual(preview["actual_input_sha256"], sha256(self.prompt_text))
        self.assertEqual(before, self.snapshot_files())
        self.assertFalse(self.docs.exists())
        self.assertFalse(self.work.exists())


class DshProbeAndMetadataTests(ModelEvalTestCase):
    """detect_install 版本标签与会话元数据标签；不启动真实 dsh、不读取真实安装。"""

    def test_detect_install_labels_electron_version_not_dsh_version(self):
        fake_root = self.root / "fake-dsh"
        (fake_root / "app" / "resources" / "runtime" / "cli" / "bin").mkdir(parents=True)
        (fake_root / "app" / "version").write_text("44.7.0\n", encoding="utf-8")
        (fake_root / "app" / "resources" / "runtime" / "versions.json").write_text(
            json.dumps({"node": "22.0.0", "pnpm": "9.0.0"}), encoding="utf-8")
        (fake_root / "app" / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd").write_text(
            "@echo off\n", encoding="utf-8")
        fake_home = self.root / "fake-dsh-home"
        env = {"DSH_INSTALL_ROOT": str(fake_root), "DSH_HOME": str(fake_home)}

        info = dsh.detect_install(env=env)  # probe=False：只读文件，不运行任何进程

        self.assertEqual(info["install_root"], str(fake_root))
        self.assertEqual(info["install_root_source"], "env:DSH_INSTALL_ROOT")
        self.assertEqual(info["desktop_app_version"], "44.7.0")
        self.assertEqual(info["electron_version"], "44.7.0")
        self.assertIn("Electron", info["version_note"])
        self.assertIn("不是 DSH 版本", info["version_note"])
        self.assertEqual(info["runtime_version"], "未记录")
        self.assertEqual(info["runtime_version_source"], "未探测（probe=False）")
        self.assertEqual(info["node_runtime_version"], "22.0.0")
        self.assertEqual(info["pnpm_runtime_version"], "9.0.0")
        self.assertEqual(info["dsh_home"], str(fake_home))
        self.assertEqual(info["dsh_home_source"], "env:DSH_HOME")
        self.assertEqual(info["cli"], str(fake_root / "app" / "resources" / "runtime" / "cli"
                                           / "bin" / "dsh.cmd"))
        self.assertEqual(info["cli_source"], "安装目录")

    def test_metadata_from_session_uses_runtime_and_electron_labels(self):
        events = json.loads((FIXTURES / "session_v4_events.json").read_text(encoding="utf-8"))
        for event in events:
            if event["type"] == "user/message":
                event["data"]["content"][0]["text"] = self.prompt_text
        parsed = dsh.parse_session_records(events)
        self.assertEqual(parsed["actual_model"], "actual-model-x")
        self.assertEqual(parsed["actual_provider"], "provider-y")
        self.assertEqual(parsed["session_origin"], "fixture-origin")
        self.assertNotIn("内部推理", parsed["final_answer"])
        metadata = dsh.metadata_from_session(
            parsed, dsh_version="0.2.1-rc.2", desktop_app_version="44.7.0",
            source="fixture")
        self.assertEqual(metadata["dsh_version"], "0.2.1-rc.2")
        self.assertEqual(metadata["framework"], "DSH（DeepSeek Harness）")
        self.assertEqual(metadata["generation"]["tools"], ["apply_patch", "pwsh"])
        notes = metadata["notes"]
        self.assertIn("DSH 运行时版本 0.2.1-rc.2", notes)
        self.assertIn("Electron 外壳版本 44.7.0", notes)
        self.assertIn("不是 DSH 版本", notes)
        self.assertNotIn("DSH Desktop 版本", notes)


class RunDshPreviewTests(ModelEvalTestCase):
    """第 8 类：run-dsh 预览只返回命令，不写记录、不执行。"""

    INSTALL = {"cli": "/fake/dsh.cmd", "runtime_version": "0.2.1-rc.2",
               "desktop_app_version": "44.7.0", "runtime_version_source": "fixture"}

    def dsh_headless_spec(self):
        def mutate(data):
            data["candidates"][0]["invocation"] = {"mode": "dsh_headless",
                                                   "profile": "headless"}
            data["candidates"][0]["budget"] = {"max_calls": 1, "timeout_seconds": 120}
        return self.teaching_spec_variant("dsh-headless-teaching.json", mutate,
                                          batch_id="fixture-dsh-headless")

    def run_preview(self, spec_relative, *extra):
        with patch("model_eval.dsh.detect_install", return_value=dict(self.INSTALL)):
            with patch("model_eval.cli.dsh.detect_install",
                       return_value=dict(self.INSTALL)) as detect:
                code, out, err = self.run_cli(
                    "run-dsh", "--project-root", str(self.root), "--spec", spec_relative,
                    "--candidate", "dsh", *extra)
        return code, out, err, detect

    def test_run_dsh_preview_does_not_execute_or_write_records(self):
        spec_relative = self.dsh_headless_spec()
        before = self.snapshot_files()
        code, out, err, detect = self.run_preview(spec_relative)
        self.assertEqual(code, 0, err)
        preview = json.loads(out)
        detect.assert_called_once_with(probe=True)

        self.assertTrue(preview["dry_run"])
        self.assertFalse(preview["wrote"])
        self.assertEqual(preview["candidate_id"], "dsh")
        self.assertEqual(preview["dsh"]["cli"], "/fake/dsh.cmd")
        self.assertEqual(preview["dsh"]["runtime_version"], "0.2.1-rc.2")
        self.assertEqual(preview["dsh"]["desktop_app_version"], "44.7.0")
        self.assertEqual(preview["timeout_seconds"], 120)
        argv = preview["argv"]
        self.assertEqual(argv[0], str(Path("/fake/dsh.cmd")))
        for expected in ("--profile", "headless", "--json"):
            self.assertIn(expected, argv)
        # 完整 prompt 只经 stdin，不进入 argv 或命令显示。
        self.assertNotIn(self.prompt_text, argv)
        self.assertNotIn(self.prompt_text, preview["command"])
        self.assertIn("stdin", preview["command"])
        self.assertEqual(preview["stdin_sha256"], sha256(self.prompt_text))
        self.assertIn("尚未执行 DSH", preview["notice"])
        self.assertEqual(before, self.snapshot_files())
        self.assertFalse(self.docs.exists())
        self.assertFalse(self.work.exists())

    def test_run_dsh_requires_headless_invocation_mode(self):
        with patch("model_eval.dsh.detect_install") as detect:
            code, _out, err = self.run_cli(
                "run-dsh", "--project-root", str(self.root), "--spec", TEACHING_SPEC,
                "--candidate", "dsh")
        self.assertEqual(code, 2)
        self.assertIn("不是 dsh_headless", err)
        detect.assert_not_called()

    def test_run_dsh_without_cli_is_rejected(self):
        spec_relative = self.dsh_headless_spec()
        missing = {**self.INSTALL, "cli": "未记录"}
        with patch("model_eval.dsh.detect_install", return_value=dict(missing)):
            with patch("model_eval.cli.dsh.detect_install", return_value=dict(missing)):
                code, _out, err = self.run_cli(
                    "run-dsh", "--project-root", str(self.root), "--spec", spec_relative,
                    "--candidate", "dsh")
        self.assertEqual(code, 2)
        self.assertIn("未找到 dsh 命令行入口", err)


class RunHeadlessTests(ModelEvalTestCase):
    """第 9 类：超时映射、平台对应的终止方式与不重试。"""

    def run_headless_with_mocks(self, process, *, timeout_seconds=5):
        run_mock = Mock()
        with patch("model_eval.dsh.subprocess.Popen", return_value=process) as popen:
            with patch("model_eval.dsh.subprocess.run", run_mock):
                result = dsh.run_headless(
                    "完整任务文本", cli=Path("/fake/dsh.cmd"), cwd=self.root / "workdir",
                    timeout_seconds=timeout_seconds)
        return result, popen, run_mock

    def test_timeout_maps_to_timeout_status_and_stops_process_once(self):
        process = Mock()
        process.pid = 1234
        process.poll.return_value = None
        process.returncode = None
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["dsh"], timeout=5),
            ("", ""),
        ]
        result, popen, run_mock = self.run_headless_with_mocks(process)

        self.assertEqual(result["status"], "timeout")
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["argv"],
                         [str(Path("/fake/dsh.cmd")), "--profile", "headless", "--json"])
        popen.assert_called_once()  # 超时后不重试、不重新启动进程
        process.communicate.assert_has_calls([
            call("完整任务文本", timeout=5),
            call(timeout=30),
        ])
        if os.name == "nt":
            run_mock.assert_called_once()
            taskkill = run_mock.call_args.args[0]
            self.assertEqual(taskkill[0], "taskkill")
            self.assertIn("/T", taskkill)
            self.assertIn("/PID", taskkill)
            self.assertIn("1234", taskkill)
            self.assertFalse(run_mock.call_args.kwargs.get("check", True))
            process.kill.assert_not_called()
        else:
            run_mock.assert_not_called()
            process.kill.assert_called_once_with()

    def test_completed_process_maps_to_completed_without_taskkill(self):
        process = Mock()
        process.pid = 4321
        process.poll.return_value = 0
        process.returncode = 0
        process.communicate.return_value = ('{"type":"final","text":"ok"}\n', "")
        result, popen, run_mock = self.run_headless_with_mocks(process)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn('"final"', result["stdout"])
        popen.assert_called_once()
        run_mock.assert_not_called()

    def test_nonzero_exit_maps_to_failed(self):
        process = Mock()
        process.pid = 777
        process.poll.return_value = 1
        process.returncode = 1
        process.communicate.return_value = ("", "fixture stderr")
        result, _popen, run_mock = self.run_headless_with_mocks(process)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("fixture stderr", result["stderr"])
        run_mock.assert_not_called()

    def test_launch_error_maps_to_failed(self):
        with patch("model_eval.dsh.subprocess.Popen", side_effect=OSError("无法启动夹具")):
            result = dsh.run_headless(
                "完整任务文本", cli=Path("/fake/dsh.cmd"), cwd=self.root,
                timeout_seconds=5)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["timed_out"])
        self.assertIn("无法启动 dsh", result["stderr"])

    def test_parse_headless_events_final_answer_and_tool_events(self):
        text = (FIXTURES / "headless_events.jsonl").read_text(encoding="utf-8")
        parsed = dsh.parse_headless_events(text)
        self.assertEqual(parsed["session_id"], "session-headless-fixture")
        self.assertEqual(parsed["event_count"], 13)
        # final 事件才是最终回答；普通 text/thinking 不冒充最终回答。
        self.assertEqual(parsed["final_answer"], "最终讲解：配方后可得结论。")
        self.assertEqual(parsed["texts"], ["先展示思路"])
        self.assertEqual(parsed["thinking"], ["内部推理不应被当作最终回答。"])
        self.assertEqual(len(parsed["tool_events"]), 2)
        ok_event, error_event = parsed["tool_events"]
        self.assertEqual(ok_event["name"], "pwsh")
        self.assertEqual(ok_event["call_id"], "c1")
        self.assertEqual(ok_event["arguments"], {"command": "echo ok"})
        self.assertEqual(ok_event["status"], "completed")
        self.assertFalse(ok_event["is_error"])
        self.assertEqual(ok_event["result_text"], "ok")
        self.assertEqual(error_event["name"], "apply_patch")
        self.assertEqual(error_event["call_id"], "c2")
        self.assertEqual(error_event["arguments"], {"patch": "fixture"})
        self.assertEqual(error_event["status"], "error")
        self.assertTrue(error_event["is_error"])
        self.assertEqual(error_event["result_text"], "failed")
        self.assertEqual(parsed["turn_end"], {"turn": 1, "reason": "complete"})
        phases = [phase["phase"] for phase in parsed["phases"]]
        self.assertEqual(phases, ["turn_start", "step_start", "step_end", "step_end",
                                  "turn_end"])
        # usage 由 step_end 的 usage 汇总，并保留采样次数。
        self.assertEqual(parsed["usage"]["total"],
                         {"inputTokens": 15, "outputTokens": 27, "totalTokens": 42})
        self.assertEqual(parsed["usage"]["step_samples"], 2)
        with self.assertRaises(EvalSpecError) as caught:
            dsh.parse_headless_events("这不是 JSON")
        self.assertIn("不是 JSON", str(caught.exception))


class TeachingReadingHtmlTests(ModelEvalTestCase):
    """第 6、11 类：教学稿未提供核验时显示尚未独立核验；HTML/索引可读。"""

    def test_html_math_index_links_and_not_verified_wording(self):
        answer = (
            "讲解：对任意实数 \\(x\\)，配方得\n\n"
            "\\[ x^2-2x+2=(x-1)^2+1>0 \\]\n\n"
            "完整原始回答：平方非负，因此结论成立。\n")
        response = self.write_text("artifacts/teaching-answer.md", answer)
        imported = self.import_cli(TEACHING_SPEC, "dsh", response,
                                   self.write_json("artifacts/teaching.json", self.metadata()))

        html_path = Path(imported["reading_path"])
        html_text = html_path.read_text(encoding="utf-8")
        self.assertIn("<math", html_text)
        self.assertIn("尚未独立核验", html_text)
        self.assertNotIn("not_run", html_text)
        self.assertNotIn("数学复核：correct", html_text)

        markdown = Path(imported["record_path"]).read_text(encoding="utf-8")
        self.assertIn("数学复核：not_run。", markdown)
        self.assertNotIn("数学复核：correct", markdown)
        self.assertIn(answer.strip(), markdown)
        self.assertNotIn("运行状态：", markdown)
        self.assertNotIn("尚未独立核验", markdown)

        run_relative = (Path(imported["record_dir"]).relative_to(self.docs)).as_posix()
        index_markdown = (self.docs / "实验索引.md").read_text(encoding="utf-8")
        self.assertIn(f"{run_relative}/测试记录.html", index_markdown)
        self.assertIn(f"{run_relative}/测试记录.md", index_markdown)
        index_html_path = Path(imported["index_reading_path"])
        self.assertTrue(index_html_path.is_file())
        index_html = unquote(index_html_path.read_text(encoding="utf-8"))
        self.assertIn(f"{run_relative}/测试记录.html", index_html)
        self.assertIn("example-q01", index_html)
        self.assertIn("<table>", index_html)


if __name__ == "__main__":
    unittest.main()
