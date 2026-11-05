# 公开版本范围与验证

本版本从研究档案源码筛选导出，采用独立 Git 历史。包含核心适配、合成测试/模板、匿名判定矩阵与聚合结果；原始对话、真实请求、会话数据库、个人评价、截图、本机安装证据与登录配置留在独立研究档案中。

## 导出后的实际修改

- 把 DeepTutor/DSH 的固定安装路径改成环境变量与通用配置，提供无凭据的运行配置示例。
- 补齐离线评测所需的合成示例材料与规格，保留第三方许可证、来源和本地修改标记。
- 新增不调用模型的合同演示、匿名判定表复算入口、完整测试入口与 CI。
- 本机全测发现一次 Windows 临时拒绝原子替换。公开副本对 winerror 5/32/33 加入有界重试，最多6次、合计等待0.5秒；耗尽或其他错误仍失败，旧文件保留。新增4项对应回归测试。
- 首次 Ubuntu CI 发现超时清理测试硬编码了 Windows 的 taskkill 断言。测试保留全部超时/单次启动检查，按平台分别检查 Windows 进程树清理或 POSIX 子进程终止；未跳过该测试，生产终止逻辑未改动。

这些公开版本的工程调整没有重跑历史模型任务；历史成绩仍来自原实验版本。初次导出测试的缺失材料与错误也记录在 [本机验证摘要](validation/local-windows.json) 中。

## 验证记录

| 验证 | 实际范围 |
|---|---|
| Windows 本机 Python 全套 | 357项发现、357项执行、357项通过；Python 3.13.9；[摘要](validation/local-windows.json) |
| JavaScript 合成指标测试 | 本机 Node 24.22.1，通过 |
| 离线演示 | 4项宿主合同检查通过，实际模型调用0；不判断数学语义 |
| 公开判定表复算 | `python examples/check_benchmark_summary.py` 校验分母、计数与分歧敏感性；不重做原始回答审阅 |
| 远程 CI | Ubuntu/Windows，Python 3.12、Node22；[以实际运行结果为准](https://github.com/mingxuanyan07-lgtm/ai-for-education-showcase/actions/workflows/offline-checks.yml) |

本机检查与远程 CI 的平台、版本分别报告。CI 的平台专属跳过项和失败均显示在测试摘要中，不将“已配置 CI”写成“已通过所有平台”。

## 公开访问与许可

公开仓库为 `ai-for-education-showcase`，完整研究档案继续独立保留。根README及文档可直接在GitHub阅读；离线演示生成的HTML在本机浏览器打开。

第三方框架和许可证见 [来源说明](../THIRD_PARTY_NOTICES.md)。本次未为独立新增模块统一指定新的许可证；Rethlas衍生文件继续遵守原许可证。
