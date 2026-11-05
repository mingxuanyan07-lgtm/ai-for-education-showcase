"""Host-only bounded mathematical data requests. No model-authored code runs."""
from __future__ import annotations

import ast
import asyncio
from fractions import Fraction
import itertools
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import os
import signal
import time

from jsonschema import Draft202012Validator
from .verification import load_json, object_sha, unique_object

VERSION = "host-safe-v1"
MCP_ARGUMENTS = {
    "verify_equivalence": {"left", "right", "samples"},
    "symbolic_calculate": {"operation", "expression", "variable", "lower_bound", "upper_bound", "point", "order", "direction"},
    "matrix_calculate": {"operation", "matrix", "rhs"},
}
BUILTINS = {"exact_arithmetic", "integer_divisors", "count_circular_permutations"}
REQUEST_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["contract_version", "requests"], "properties": {
        "contract_version": {"const": VERSION}, "requests": {"type": "array", "maxItems": 4,
            "items": {"type": "object", "additionalProperties": False,
                "required": ["id", "operation", "arguments", "assumptions", "domain", "purpose", "required"],
                "properties": {"id": {"type": "string", "pattern": "^K[1-9][0-9]*$"},
                    "operation": {"enum": sorted(BUILTINS | set(MCP_ARGUMENTS))},
                    "arguments": {"type": "object"}, "assumptions": {"type": "object"},
                    "domain": {"type": "string", "minLength": 1},
                    "purpose": {"type": "string", "minLength": 1}, "required": {"type": "boolean"}}}}}}


def extract_requests(proof: str) -> list[dict]:
    match = re.match(r"\A\s*<!-- rethlas-compute-v1\s*\n(.*?)\n-->", proof, re.S)
    if not match:
        return []
    if len(match[1].encode("utf-8")) > 32768:
        raise ValueError("Computation requests exceed 32 KiB")
    payload = json.loads(match[1], object_pairs_hook=unique_object)
    Draft202012Validator(REQUEST_SCHEMA).validate(payload)
    requests = payload["requests"]
    if len({row["id"] for row in requests}) != len(requests):
        raise ValueError("Duplicate computation request ID")
    for row in requests:
        if not row["domain"].strip() or not row["purpose"].strip():
            raise ValueError("Computation needs explicit domain and purpose")
        if "assumptions" in row["arguments"]:
            raise ValueError("Assumptions belong in the explicit assumptions field")
    return requests


def exact(expression: str) -> Fraction:
    if not isinstance(expression, str) or len(expression) > 1024:
        raise ValueError("Exact expression must be a short string")
    tree = ast.parse(expression, mode="eval")
    if len(list(ast.walk(tree))) > 96:
        raise ValueError("Expression too complex")
    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) is int and len(str(abs(node.value))) <= 128:
            result = Fraction(node.value)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            result = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add): result = left + right
            elif isinstance(node.op, ast.Sub): result = left - right
            elif isinstance(node.op, ast.Mult): result = left * right
            elif isinstance(node.op, ast.Div): result = left / right
            elif isinstance(node.op, ast.Pow) and right.denominator == 1 and abs(right) <= 16: result = left ** int(right)
            else: raise ValueError("Unsupported exact arithmetic operator")
        else:
            raise ValueError("Only integer arithmetic is accepted; no calls, names or attributes")
        if max(result.numerator.bit_length(), result.denominator.bit_length()) > 4096:
            raise ValueError("Exact arithmetic result too large")
        return result
    return visit(tree.body)


def builtin(request):
    op, args = request["operation"], request["arguments"]
    if request["assumptions"]:
        raise ValueError("These finite/exact operations take no symbolic assumptions")
    if op == "exact_arithmetic":
        if set(args) - {"expression", "expected"} or "expression" not in args:
            raise ValueError("Invalid exact arithmetic arguments")
        value = exact(args["expression"])
        result = {"ok": True, "exact_result": str(value)}
        if "expected" in args:
            result["matches_expected"] = value == exact(args["expected"])
        return result
    if op == "integer_divisors":
        number = args.get("number")
        if set(args) != {"number"} or type(number) is not int or not 1 <= number <= 10**9:
            raise ValueError("Divisor enumeration needs an integer in [1, 10^9]")
        divisors = set()
        for d in range(1, math.isqrt(number) + 1):
            if number % d == 0: divisors.update((d, number // d))
        return {"ok": True, "divisors": sorted(divisors), "count": len(divisors), "exhaustive": True}
    if op == "count_circular_permutations":
        if set(args) != {"items", "forbidden_pairs", "rotations_identical"}:
            raise ValueError("Invalid circular enumeration arguments")
        items, pairs, rotation = args["items"], args["forbidden_pairs"], args["rotations_identical"]
        if (not isinstance(items, list) or not 2 <= len(items) <= 8
                or any(not isinstance(x, str) or not x or len(x) > 32 for x in items)
                or len(set(items)) != len(items) or type(rotation) is not bool or not isinstance(pairs, list)):
            raise ValueError("Circular enumeration requires 2-8 distinct labels and explicit rotation equivalence")
        forbidden = set()
        for pair in pairs:
            if (not isinstance(pair, list) or len(pair) != 2 or pair[0] == pair[1]
                    or any(x not in items for x in pair)):
                raise ValueError("Invalid forbidden pair")
            forbidden.add(frozenset(pair))
        arrangements = ((items[0],) + p for p in itertools.permutations(items[1:])) if rotation else itertools.permutations(items)
        count = examined = 0
        for p in arrangements:
            examined += 1
            if all(frozenset((p[i], p[(i + 1) % len(p)])) not in forbidden for i in range(len(p))): count += 1
        return {"ok": True, "count": count, "examined": examined, "exhaustive": True,
                "rotations_identical": rotation, "reflections_identical": False}
    raise ValueError("Operation is not a built-in calculation")


def _mcp_tool_schema(tool):
    # MCP Python 2.x uses snake_case fields; 1.x exposed wire camelCase.
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        schema = getattr(tool, "inputSchema", None)
    if not isinstance(schema, dict):
        raise ValueError("Unsupported math-local tool schema envelope")
    return schema


def _mcp_result_payload(response):
    error = getattr(response, "is_error", None)
    if error is None:
        error = getattr(response, "isError", None)
    if type(error) is not bool:
        raise ValueError("Unsupported math-local error envelope")
    if error:
        raise ValueError("math-local returned a tool error")
    payload = getattr(response, "structured_content", None)
    if payload is None:
        payload = getattr(response, "structuredContent", None)
    if not isinstance(payload, dict):
        parts = [json.loads(block.text, object_pairs_hook=unique_object)
                 for block in response.content if getattr(block, "type", None) == "text"]
        payload = next((value for value in parts if isinstance(value, dict) and "ok" in value), None)
    if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
        raise ValueError("Unsupported math-local result envelope")
    return payload


async def _mcp(root: Path, request: dict, timeout: float, log_path: Path, math_runtime_file=None):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    runtime_path = root / "setup/runtime.json"
    if math_runtime_file is not None:
        from deepseek_rethlas.pipeline import _inside
        runtime_path = _inside(root.resolve(), math_runtime_file)
        if runtime_path.suffix != ".json" or runtime_path.stat().st_size > 65536:
            raise ValueError("Invalid bounded project math runtime JSON")
    config = load_json(runtime_path)["shared_mcp"]["math-local"]
    allowed = MCP_ARGUMENTS[request["operation"]]
    if set(request["arguments"]) - allowed:
        raise ValueError("Unsupported math-local arguments")
    arguments = dict(request["arguments"])
    if request["operation"] == "matrix_calculate":
        if request["assumptions"]:
            raise ValueError("Matrix entries must carry explicit numeric values")
    else:
        arguments["assumptions"] = request["assumptions"]
    params = StdioServerParameters(command=config["command"], args=config["args"])
    with log_path.open("x", encoding="utf-8") as log:
        async with stdio_client(params, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                tool = tools.get(request["operation"])
                if tool is None: raise ValueError("Requested calculation is not available")
                Draft202012Validator(_mcp_tool_schema(tool)).validate(arguments)
                response = await session.call_tool(tool.name, arguments, read_timeout_seconds=timeout)
                return _mcp_result_payload(response)


def execute_requests(root, requests, directory, deadline, *, mcp_caller=None, math_runtime_file=None):
    Draft202012Validator(REQUEST_SCHEMA).validate({"contract_version": VERSION, "requests": requests})
    if len({row["id"] for row in requests}) != len(requests):
        raise ValueError("Duplicate computation request ID")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    checks = []
    for request in requests:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Shared computation budget exhausted")
        started = time.monotonic()
        check = {"evidence_id": request["id"], "request": request,
                 "backend": "host-exact-finite" if request["operation"] in BUILTINS else "math-local MCP",
                 "scope": "Only the supplied expression/domain/finite constraints; applicability and the whole proof need independent review."}
        try:
            if request["operation"] in BUILTINS:
                result = builtin(request)
            elif mcp_caller is not None:
                result = mcp_caller(Path(root), request, min(20, remaining), directory / (request["id"] + ".stderr.log"))
            else:
                # An asyncio deadline cannot bound a broken stdio transport's
                # shutdown. A disposable owned process provides the hard bound.
                request_path = directory / (request["id"] + ".request.json")
                with request_path.open("x", encoding="utf-8") as stream:
                    json.dump(request, stream, ensure_ascii=False)
                kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
                argv = [sys.executable, "-X", "utf8", "-m", "rethlas_core.computation_worker",
                    str(Path(root).absolute()), str(request_path.absolute()), str(min(20, remaining)),
                    str((directory / (request["id"] + ".stderr.log")).absolute())]
                if math_runtime_file is not None:
                    argv.append(str(Path(math_runtime_file).absolute()))
                process = subprocess.Popen(argv,
                    cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
                try:
                    stdout, stderr = process.communicate(timeout=min(20, remaining))
                except BaseException:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    if process.poll() is None:
                        process.kill()
                    try:
                        process.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.stdout.close()
                        process.stderr.close()
                    raise
                if process.returncode:
                    raise ValueError("MCP client failed: " + stderr.decode("utf-8", errors="replace")[-4000:])
                result = json.loads(stdout, object_pairs_hook=unique_object)
            check.update(status="completed" if result.get("ok") is True else "failed", result=result)
        except Exception:
            import traceback
            check.update(status="failed", result={"ok": False, "error": traceback.format_exc(limit=2)})
        check["elapsed_seconds"] = time.monotonic() - started
        check["result_sha256"] = object_sha(check["result"])
        checks.append(check)
    return checks


def solver_brief():
    return """\n【可选宿主计算请求 host-safe-v1】
需要检查可计算步骤时，可在完整证明最前面加入唯一的 HTML 注释：
<!-- rethlas-compute-v1
{"contract_version":"host-safe-v1","requests":[{"id":"K1","operation":"exact_arithmetic","arguments":{"expression":"1/3+1/6","expected":"1/2"},"assumptions":{},"domain":"有理数精确运算","purpose":"说明它检查正文的哪一步","required":true}]}
-->
上例仅说明数据格式，不能照抄为本题请求；不需要计算时省略注释。每轮最多4项。
可用 exact_arithmetic(expression,可选expected)、integer_divisors(number正整数不超过10^9)、
count_circular_permutations(items为2至8个不同标签,forbidden_pairs为禁邻标签对,rotations_identical为布尔值)。
还可用 math-local 的 verify_equivalence(left,right,可选samples)、symbolic_calculate(operation,expression,variable等)、
matrix_calculate(operation,matrix,可选rhs)。符号假设放独立assumptions字典，例如 {"x":{"real":true}}。
只提交数学数据，不提交程序代码、shell、文件路径或任意工具名。宿主保存并执行允许列表内的请求。
不自称工具已执行；计算的适用条件仍须在证明中成立，抽样、化简或有限枚举不替代整题证明。
"""
