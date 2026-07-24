# CaseGen

AI 测试用例生成管理平台（Demo）

文档 → LLM-Wiki → 需求驱动生成 → AI 评审 → 提示词迭代 → 终版用例。

## 目标场景

金融 / 交易所相关测试用例生成，**质量优先**。

## 技术栈（MVP）

- 前端：Vue 3 + TypeScript + Element Plus
- 后端：Python FastAPI
- 存储：SQLite + 本地 Markdown Wiki
- 模型：可配置的 OpenAI 兼容多模型

## 设计文档

见 [`docs/superpowers/specs/2026-07-24-ai-testcase-platform-design.md`](docs/superpowers/specs/2026-07-24-ai-testcase-platform-design.md)。

## 仓库

远程：`git@github.com:Lemon1013/CaseGen.git`

## 状态

设计规格已就绪；实现计划与代码将按该规格推进。
