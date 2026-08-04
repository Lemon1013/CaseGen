# CaseGen Wiki 页面约定

## 页面类型

页面类型只能是 source、rule、entity、scenario、regression 或 synthesis。page_key 是小写的点号分段标识，例如 rule.order.insufficient-balance；它不是文件路径，禁止斜杠、反斜杠、连续点号和绝对路径语法。

## Frontmatter

每页使用 YAML frontmatter：

~~~
---
page_key: rule.order.insufficient-balance
title: 余额不足时的下单处理
type: rule
domain: spot-order
aliases: [余额不足下单]
tags: [余额, 下单]
sources:
  - document_id: 12
    chunk_ids: [81, 82]
    clauses: ["3.5.2"]
status: published
revision: 1
updated_at: 2026-08-02
---
~~~

rule 页面至少需要一个有效的结构化来源。页面正文应说明适用条件、规则、影响、异常或边界、测试提示和相关页面；页面间引用使用 [[page_key|显示标题]]。

