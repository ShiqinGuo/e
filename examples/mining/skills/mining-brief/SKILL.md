---
name: mining-brief
description: 使用新闻、文档与行情三个 MCP 生成 Pilbara／Pilgangoora 锂矿简报，包含新闻摘要、资源量数据、价格走势、风险提示和来源链接。
---

# Mining brief

| MCP | 方法与用途 |
| --- | --- |
| mining-news | `search(query, days)` 查新闻；`fetch_article(url)` 读原文；异步获取用 `get_task(task_id)` 查询到完成。 |
| mining-documents | `extract_resources(pdf_url, standard)` 提交资源抽取；`get_extraction(extraction_id)` 查询状态；完成后 `get_extraction_result(extraction_id)` 读取分类、品位、日期和证据。PDF 解析由服务端统一完成。 |
| mining-market | `list_instruments()` 选具体 slug；`get_price(commodity, date, mode)` 取报价；`get_trend(commodity, days)` 比较同一品种的实际观测。 |

Pilgangoora 可复用已完成抽取 `9fa80d39-6baf-548e-8259-3569e7c4c975`，来源为 https://announcements.asx.com.au/asxpdf/20250611/pdf/06kmc3l7r1bjsm.pdf 。先核对完成状态，再读取结果；不重复提交。其他报告按文档方法处理，UNKNOWN 不自动重试。

以带时区的当前时间确定报告日。输出中文 Markdown：新闻摘要、资源量数据、价格走势、风险提示、引用源链接。区分公告日、资源基准日和报价日，资源量不能替代储量；现货、期货与季度价分别列示，变化对应实际起止观测，缺失数据明确说明。来源使用工具返回的原始 URL。
