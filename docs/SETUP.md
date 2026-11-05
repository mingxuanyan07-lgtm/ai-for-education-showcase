# 配置与复现范围

离线体验只需 Python 3.12+、Node.js 22+ 和根目录 `requirements.txt`。在仓库根目录运行：

```bash
python -m pip install -r requirements.txt
python scripts/run_offline_tests.py
node scripts/tests/test_index_dashboard.cjs
python examples/offline_demo.py --output demo-output
```

用浏览器打开 `demo-output/index.html`。这一路径使用合成夹具；绑定报告中的 correct 是预设测试数据，模型调用为0，不是数学证明通过的结果。临时记录归为安装测试，不进入真实数学实验索引。

## 真实工作流需自行配置

真实模型流程依赖外部 Codex/DSH、模型路由、独立 DeepTutor 与可选 math-local，公开演示不自动安装或启动它们。当前适配基于研究时冻结的接口，其他版本需另行验证。

| 配置 | 含义 |
|---|---|
| `setup/runtime.json` | 原版 Codex 适配的模型、CLI命令与共享MCP。`CONFIGURE_MODEL` 为占位值 |
| `setup/deepseek-rethlas*.json` | 分阶段流程的历史路由、角色/时间/Token准入限额；不含凭据 |
| `DSH_CLI`、`DSH_INSTALL_ROOT` | 本机 DSH 的命令与安装目录 |
| `DEEPTUTOR_ROOT`、`DEEPTUTOR_PYTHON` | 独立 DeepTutor 目录与其 Python，不复用主项目依赖 |
| `setup/hybrid-math-runtime.json` | `CONFIGURE_MATH_PYTHON`、`CONFIGURE_MATH_SERVER` 需要替换为本机数学MCP命令与服务器 |
| `setup/hybrid-tutor.json` | 显式组合合同；修改数学运行配置后须重新计算其文件SHA256并更新绑定字段 |

原版两个角色还需要创建各自的 `.codex/config.toml` 与 MCP/子角色路径。示例见角色目录的 `config.example.toml`；替换占位路径与模型并检查 CLI 版本兼容性后再使用。保持 `workspace-write` 与审批边界，不通过放宽权限消除配置错误。

登录、API key和本机配置均由使用者在自己的环境管理。不要把它们放入公开仓库；该仓库也没有可复用的登录状态。

## 运行策略

先使用 CLI 的 `--help` 或 `--dry-run` 检查计划与路径；真实调用前明确题面、学生背景、提供方、模型和预算。以响应记录核对实际模型，不以配置字段代替实际型号。默认失败关闭、无跨提供方自动回退。

证明核验、教学核验、当前会话终审与共同外审职责不同。模型自报 correct 不允许绕过宿主内容绑定；宿主合同通过也不等于数学语义正确。新教学内容需另行复核。

## 测试范围

离线测试检查数据合同、预算、角色边界、绑定、状态、原子写入及归档展示。CI分别记录 Ubuntu/Windows 结果，平台专属测试的跳过会显式显示。离线测试和历史模型实验的统计分别保留，不能互相替代。
