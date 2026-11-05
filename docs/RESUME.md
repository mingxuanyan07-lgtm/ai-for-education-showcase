# 简历项目表述

项目名称可写为：**可审计数学教学 AI 工作台**。公开代码地址：<https://github.com/mingxuanyan07-lgtm/ai-for-education-showcase>。

技术关键词：Python、LLM 工作流、MCP、JSON Schema、SHA256、SQLite、Windows 进程适配、HTML/MathML、实验评测。

## 可直接粘贴的三条

- 基于开源 Rethlas 与 DeepTutor，采用 AI 辅助开发多角色数学教学实验平台，组织求解、独立核验、有限修订与教学整理，完成 Windows 运行适配及受限异步教学桥接。
- 实现题面—证明—核验报告的 SHA256 绑定、逐命题依赖与教学合同校验，结合共享预算、状态记忆和失败留证，构建可追溯的运行与发布流程。
- 搭建统一实验归档、分阶段用量统计和离线 MathML 阅读界面；围绕17道 MATH 抽样题组织多批对照，项目累计归档97份数学实验记录，保留个案、复测和失败，分别报告正确性、教学表现、可靠性与成本。

上述记录数包含复测、迭代与失败，不是独立题目数。真实实验的覆盖与审阅分歧见 [RESULTS.md](RESULTS.md)；公开仓库的合成离线演示没有调用模型。

## 面试时可以展开

| 主题 | 可以说明的具体设计 | 源码 |
|---|---|---|
| 为什么需要宿主校验 | 防止题面/证明/报告错配与模型自述完成；哈希合同和数学语义审阅分别负责不同问题 | [verification.py](../scripts/rethlas_core/verification.py) |
| 怎样避免无界修复 | 共享角色尝试、时间与已观测 Token 准入，明确未知用量与失败终态 | [budget.py](../scripts/rethlas_core/budget.py) |
| 怎样组合两个框架 | 将绑定证明交给独立教学 worker，收窄上下文、工具与调用范围，再单独核验教学稿 | [bridge.py](../scripts/hybrid_tutor/bridge.py) · [deeptutor_worker.py](../scripts/hybrid_tutor/deeptutor_worker.py) |
| 怎样做可信评测 | 独立实验身份、不可变证据、最终输出绑定的外审，保留缺测、未作答与分歧 | [experiment_records.py](../scripts/experiment_records.py) · [benchmark_evaluation.py](../scripts/benchmark_evaluation.py) |
| 从失败中学到什么 | 模型正文、教学附加内容、流程交付和评审一致性需要分别检查；不能只用最终数值答案评价整篇教学稿 | [实验结果与限制](RESULTS.md) |

## 表述边界

本项目新增贡献是工程适配、合同、桥接、记录与评测设计。Rethlas 原有证明框架、DeepTutor 原生 AgentLoop、语言模型和第三方库应保留来源说明；不宜写成“从零原创完整 AI 框架”或声称全部代码均为个人手写。

现有证据不支持“准确率提升某百分比”“显著提高学习效果”“完整基准领先”等表述。简历应强调具体实现与可复查工作量，实测数字须连同样本、分母和缺测边界解释。

本公开版为 source-available 展示，本轮没有为本项目新增代码授予新的开源许可证。许可与来源见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。
