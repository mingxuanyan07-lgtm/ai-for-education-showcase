# 第三方来源与本地修改

本仓库是基于既有框架的工程适配与研究展示。Rethlas 和 DeepTutor 的原有求解、记忆、AgentLoop 等能力归属各自项目；本项目新增的合同、桥接、归档、评测与运行适配见 README。

| 内容 | 来源 | 许可与本仓库范围 |
|---|---|---|
| `Rethlas/` | [frenzymath/Rethlas](https://github.com/frenzymath/Rethlas)，基线提交 `e7e48a6d30e7f0ddf0ed1909a594a1f0cfe2c2b7` | 保留 [Apache-2.0 许可证](Rethlas/LICENSE)。包含本地修改后的角色合同与 MCP 源码；不分发机器登录配置或历史运行数据 |
| `scripts/vendor/katex/` | KaTeX 0.16.22，固定来源见 [provenance.json](scripts/vendor/katex/provenance.json) | 保留 [MIT 许可证](scripts/vendor/katex/LICENSE.txt)；用于离线生成 MathML |
| DeepTutor 适配 | 研究档案使用独立部署的 DeepTutor 1.6.15 | 本仓库只含与其 API 交互的桥接/受限 worker；不分发完整应用或其安装包源码 |
| MATH 抽样结果 | 历史研究中的 MATH-500 抽样实验 | 仅公开聚合统计；不分发数据集原题、参考答案或原始会话 |

`docs/source-manifest.json` 记录筛选导出时的来源文件哈希与档案版本。公开副本新增离线演示、安装与测试入口，并将固定安装路径改为环境变量；这些可移植性变更不代表重新运行了历史模型实验。

第三方内容遵守其各自许可证，Rethlas衍生文件继续遵守其Apache-2.0许可证。本次未为独立新增模块统一指定新的许可证；如需进一步授权，请联系仓库维护者。公开访问与软件许可分别说明。
