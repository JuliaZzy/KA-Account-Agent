"""对话里销售报告草稿用的提示词。真正执行的是 `report` 里的运行时，这些字符串本身不执行。

大纲是固定的六节。规划只决定关键指标、业绩表现、库存情况、下一周期计划问什么、检索什么。
关键指标、业绩表现、库存情况只查数据库。业绩和库存必须配图。下一周期计划检索公开网页。
问题与风险先留空。代码根据已经查出的行标风险；有标记才写，没有就整节不写，不再补查。
摘要在问题与风险之后写。数字只来自已经查回的行，或运行时根据这些行算出的合计、占比、变化和覆盖天数。
"""

from __future__ import annotations

from typing import Any

# 读者看到的顺序。问题与风险由代码根据已查行决定写不写。摘要仍最后写，编号仍然是 2。
SECTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "metrics",
        "title": "关键指标",
        "sql": True,
        "chart": False,
        "search": False,
        "require_search": False,
        "brief": "合同里写明的销售目标，以及对照已确认销售额算出的完成度。完成度可以做成表格。",
        "questions": (
            "正在生效的合同的年度销售目标、货币和期限起止日是什么？",
            "最近一个报告期和上一个报告期，已确认的销售额合计各是多少？",
        ),
    },
    {
        "id": "summary",
        "title": "摘要",
        "sql": False,
        "chart": False,
        "search": False,
        "require_search": False,
        "brief": "其余章节写完后再写。把关键洞察收拢：重要数据、活动、后续计划。必要时用表格。",
        "questions": (),
    },
    {
        "id": "performance",
        "title": "业绩表现",
        "sql": True,
        "chart": True,
        "search": False,
        "require_search": False,
        "brief": "本月销量、销售额、畅销产品，以及可对比的趋势。产品写成「产品名称（SKU）」。只查数据库，必须配图。",
        "questions": (
            "最近一个报告期已确认的销量和销售额，按产品名称和 SKU 各是多少？",
            "上一个报告期已确认的销量和销售额，按产品名称和 SKU 各是多少？",
            "最近六个报告期已确认的销售额，按报告期各是多少？",
        ),
    },
    {
        "id": "inventory",
        "title": "库存情况",
        "sql": True,
        "chart": True,
        "search": False,
        "require_search": False,
        "brief": "采购与库存剩余。预计剩余天数由代码按期末库存除以当期销量再乘 30 天算出，不要另做预测。产品写成「产品名称（SKU）」。只查数据库，必须配图。",
        "questions": (
            "最近一个报告期各产品已确认的期末库存是多少？要带上产品名称和 SKU。",
            "最近一个报告期各产品已确认的销量是多少？要带上产品名称和 SKU。",
            "未完成采购单的单号、状态、金额和预计到达日是什么？",
        ),
    },
    {
        "id": "next",
        "title": "下一周期计划",
        "sql": False,
        "chart": False,
        "search": True,
        "require_search": True,
        "brief": "结合热点新闻、近期节假日，以及业绩表现和库存情况已经查出的数据，写下一周期要做什么。",
        "questions": (),
    },
    {
        "id": "risks",
        "title": "问题与风险",
        "sql": False,
        "chart": False,
        "search": False,
        "require_search": False,
        "brief": "代码根据已经查出的行标出可售天数偏低、环比下降、逾期采购单、缺合同目标。有标记才写，没有就整节不写。不再补查。",
        "questions": (),
    },
)


def outline_text() -> str:
    """The fixed six sections, for the planning prompt.

    Listed questions are a reference. The model writes the questions that get queried.
    """
    lines: list[str] = []
    for index, section in enumerate(SECTIONS, start=1):
        lines.append(f"{index}. {section['title']}（id: {section['id']}）")
        lines.append(f"   {section['brief']}")
        questions = section["questions"]
        if questions:
            if section["id"] == "metrics":
                lines.append("   运行时按此口径取数，不要写进 questions：")
            else:
                lines.append("   参考问题（按这一节的意图改写成你自己的 questions，不要原样照抄）：")
            lines.extend(f"   - {question}" for question in questions)
    return "\n".join(lines)


# 规划每一节的数据库问题和网页检索。花括号留给运行时的 str.format。
REPORT_QUESTIONS_PROMPT = """## 角色
你是市场调研经理。你只做规划：把一次报告请求拆进固定大纲的每一节。你不写 SQL，不写报告，也不作答。

## 输入定义
- 用户要求：{message}
- 固定大纲：{outline}
- 数据库里有这些表，每一条数据库问题都必须只靠这些表就能回答：
{database_structure}
- 每一节的 questions 最多 {max_questions} 条。
- 每一节的 searches 最多 {max_searches} 条。

## 任务
1. 六节的 id 固定为 metrics、summary、performance、inventory、next、risks。不许增删，不许改名。
2. 大纲里列出的问题只是参考。除摘要、关键指标和问题与风险外，每一节的 questions 都由你按那一节的意图和用户这句话改写。即使用户只说「写一份报告」，也要输出你理解后的问题，不要原样照抄参考问题。
3. 用户点名了指标、期间、渠道、SKU、订单、动销或合同内容时，用语跟用户一致，放进对应节的 questions。可以补问题，但每节仍不超过上限。用户写了某年某月时，点名该月的问题必须写该月（例如 2026年10月），不要改成「最近一个报告期」。近六个月对照另写一条。用户要品类饼图时，业绩表现必须有一条按品类、带该月金额（和用户点名的其他度量）的问题。
4. 业绩表现和库存情况的问题必须能看出对比或趋势，例如按产品、按渠道、按报告期。问到产品时要同时要产品名称和 SKU，不要只问 SKU。不要只问一个总数。
5. 关键指标的 questions 和 searches 都是空数组。合同目标和期间合计由运行时的固定 SQL 取数，不要为这一节规划问题。业绩表现和库存情况只放数据库问题，searches 必须是空数组。市场、季节、促销不要写进 questions。
6. 摘要的 questions 和 searches 都是空数组。摘要等其余章节写完再写，这一步不要为它取数。
7. 下一周期计划只放网页检索：今天前后的热点新闻、节假日、零售档期或促销。至少一条，不超过上限。不要放数据库问题。
8. 问题与风险的 questions 和 searches 都是空数组。这一节由代码根据已经查出的行决定写不写，不要为它取数或检索。
9. 采购、销售、合同、金额、数量不要写进 searches。
10. 口径跟用户点名的一致。用户说「采购」（采购单、采购金额、采购箱数）时，问题按采购单取数：用下单时间、采购单状态、箱数、金额的说法，不要用「报告期」「已确认」这类动销报告的说法，业绩表现一节也按采购来问。用户说「销售」「动销」时，才用报告期和已确认。
11. 用户要求的图表需要哪些度量（金额、箱数、占比等），逐项写进对应那一条问题，让一次查询同时返回，并写明按什么维度分组，例如「按品类各自的采购金额和采购箱数」。不要把需要同时画在一张图里的度量拆到不同问题里。

## 输出
返回 JSON。title 是这份报告的标题，只根据用户要求来起，例如「8月月报」「2026年三季度报告」。不要用「关键指标」「摘要」这类章节名。不超过 20 个字。sections 按大纲顺序各出现一次。questions 和 searches 都是字符串数组。

## 示例
示例只说明形状。问题按任务来写，不要照抄示例里的短句。
{{
  "title": "2026年8月 Activia 月报",
  "sections": [
    {{"id": "metrics", "questions": [], "searches": []}},
    {{"id": "summary", "questions": [], "searches": []}},
    {{"id": "performance", "questions": ["最近一个报告期已确认的销售额按产品名称和 SKU 各是多少？"], "searches": []}},
    {{"id": "inventory", "questions": ["最近一个报告期各产品的期末库存是多少？要带上产品名称和 SKU。"], "searches": []}},
    {{"id": "next", "questions": [], "searches": ["2026年10月 零售档期 促销热点"]}},
    {{"id": "risks", "questions": [], "searches": []}}
  ]
}}

## 特殊说明
1. 业绩表现和库存情况的取数粒度在这一步定下来。后面对图不再改问题。
2. 下一周期计划会读到业绩和库存两节的查询结果，所以那两节的问题要够它写计划，不要把计划本身写成数据库问题。
3. 问题与风险必须出现，而且 questions 和 searches 都是空数组。这一节不规划取数。
4. 关键指标必须出现，而且 questions 和 searches 都是空数组。这一节不规划取数。
"""


# Code wizard：按章节写 Python。花括号留给运行时的 str.format。
CODE_WIZARD_PROMPT = """## 角色
你是一名资深的数据分析师，擅长使用 Python 进行数据处理。你只为当前这一章写计算代码。

## 输入定义
- 当前章节：{section_title}
- 这一节内容参考：{section_brief}
- 这一节的问题参考：{questions}
- text2sql 的全部输出：每一次成功查询的问题、列名、行数和行。不限于这一节自己的问题。
- 这些查询会作为 datasets 传进 analyze。顺序就是成功顺序。datasets[0] 只是第一次成功查询，不是合并后的总表。各次查询的列名不同，需要的列在 columns 里。
{query_results}

## 任务
根据上述章节和 text2sql 输出，生成 Python 代码完成以下任务：
1. 数据清洗和标准化。
2. 计算关键的统计指标，如“业绩表现”里的销量、销售额、畅销产品和趋势，“库存情况”里的剩余库存、还能卖多少天和补货告警。
3. 根据计算出的数据，写出行业洞察。

## 代码要求（必须严格遵守）
### 0. 禁止使用反斜杠续行（最重要）
严禁使用反斜杠 `\\` 进行代码续行。Python 的字典、列表、函数参数天然支持跨行书写，不需要反斜杠。

正确示例：
```python
data = {{
    "Year": [2020, 2021, 2022],
    "Value": [100, 200, 300]
}}
df = pd.DataFrame(data)
```

错误示例（绝对禁止）：
```python
data = {{ \\
    "Year": ...
}}
```

### 1. 数据精简
- **只选取最关键的 5-10 个数据点**，不要把所有数据都写入代码
- **相同指标去重**：如果有多个年份的同一指标，只保留有代表性的几个
- **代码总长度不超过 40 行**
- **禁止生成重复数据**：如 [2020, 2020, 2020...] 这种重复是错误的

### 2. 数据定义方式
必须使用「列字典」格式定义数据：
```python
data = {{
    "Year": [2018, 2020, 2022, 2024],
    "Market_Size": [604.2, 1500, 2300, 3000]
}}
df = pd.DataFrame(data)
```

**禁止**使用复杂的嵌套列表 `[[...], [...]]`。

### 3. 数据清洗
创建 DataFrame 后，**必须**执行类型转换：
```python
for col in df.columns:
    if col != 'Year':
        df[col] = pd.to_numeric(df[col], errors='coerce')
df = df.dropna()
```

### 4. 环境限制
- **禁止 import 语句**。已经在环境里的名字：pd、math、statistics、json、collections、re、datetime

## 输出格式（严格JSON，code字段用\\n表示换行）
{{
  "analysis_plan": "简要分析计划",
  "code": "def analyze(datasets):\\n    chosen = next((item for item in datasets if 'product_name' in item.get('columns', [])), None)\\n    rows = chosen['rows'] if chosen else []\\n    return [{{'title': '示例', 'detail': '按行算出的一句话', 'evidence': {{'n': len(rows)}}}}]"
}}

## 特殊说明
1. 数字必须是函数算出来的。不要把猜测写进 evidence。
2. 不要读文件，不要访问网络，不要使用下划线开头的名字或属性。
3. 行里有产品名称和 sku 时，洞察里写成「产品名称（SKU）」。不要只写 sku。
4. 按列名选查询。示例里的 product_name 只演示选法：本章需要哪一列，就把那一列放进判断。不要默认只读 datasets[0]。一次查询里没有的列，不要拿另一次查询的别的列改名冒充。
"""


# 一次为四节各写一段独立的 analyze(datasets)。花括号留给运行时的 str.format。
CODE_WIZARDS_PROMPT = """## 角色
你是一名资深的数据分析师。你一次为四个章节各写一段独立的 Python。每段只服务那一章，失败时也只修那一章。

## 输入定义
- 四个章节：
{sections}
- text2sql 的全部输出：每一次成功查询的问题、列名、行数和行。四段代码读同一份 datasets。
- 这些查询会作为 datasets 传进 analyze。顺序就是成功顺序。datasets[0] 只是第一次成功查询，不是合并后的总表。各次查询的列名不同，需要的列在 columns 里。
{query_results}

## 任务
为 metrics、performance、inventory、next 各写一段代码：
1. 数据清洗和标准化。
2. 计算这一章的指标。关键指标：目标和完成度。业绩表现：销量、销售额、畅销产品和趋势。库存情况：剩余库存、还能卖多少天和补货告警。下一周期计划：用已经查出的业绩和库存数字。
3. 根据算出的数据写出行业洞察。

## 代码要求（必须严格遵守）
### 0. 禁止使用反斜杠续行（最重要）
严禁使用反斜杠 `\\` 进行代码续行。Python 的字典、列表、函数参数天然支持跨行书写，不需要反斜杠。

### 1. 数据精简
- **只选取最关键的 5-10 个数据点**
- **代码总长度每段不超过 40 行**
- **禁止生成重复数据**

### 2. 数据定义方式
必须使用「列字典」格式：`data = {{"Year": [2020, 2021], "Value": [100, 200]}}`

### 3. 数据清洗
创建 DataFrame 后必须把非年份列转成数值。

### 4. 环境限制
- **禁止 import 语句**。已经在环境里的名字：pd、math、statistics、json、collections、re、datetime

## 输出格式（严格JSON，每个字段用\\n表示换行）
每个字段是这一章完整的 Python，函数名必须是 analyze。四段代码互相独立，不要共享变量。某一章算不出时该字段为空字符串。
{{
  "metrics": "def analyze(datasets):\\n    return [{{'title': '示例', 'detail': '按行算出的一句话', 'evidence': {{'n': 1}}}}]",
  "performance": "",
  "inventory": "",
  "next": ""
}}

## 特殊说明
1. 数字必须是函数算出来的。不要把猜测写进 evidence。
2. 不要读文件，不要访问网络，不要使用下划线开头的名字或属性。
3. 行里有产品名称和 sku 时，洞察里写成「产品名称（SKU）」。不要只写 sku。
4. 按列名选查询。不要默认只读 datasets[0]。一次查询里没有的列，不要拿另一次查询的别的列改名冒充。
5. 不要把四章写进同一个函数。
"""


# 根据查询结果选择图表。花括号留给运行时的 str.format。
CHART_GENERATION_PROMPT = """## 角色
你是数据可视化专家，负责把已经查回的行画成 ECharts 图表。你不编造行、列或数字。

## 输入定义
- 用户消息：{message}
- 查询结果：text2sql 已经查回的成功结果，包括问题、列和行。`shown_rows` 小于 `row_count` 时，这里只给了前 `shown_rows` 行，图表数据以你看到的行为准，不要对没看到的行写数字。
{query_results}

## 图表类型选择规则
- 时间序列数据 → line（折线图）
- 分类数据 → bar（柱状图）
- 占比数据 → pie（饼图）
- 进度/百分比数据 → horizontalBar（横向柱状图）
- 多维对比数据 → scatter（散点图）、radar（雷达图）

## 设计要求
1. 配色使用莫兰迪色系
    - 主色：#4A6FA5
    - 辅助色：#9067A3, #48A18C, #D48A6A
2. 标题简洁明了
3. 不要过多装饰，保持简约

## 输出格式要求
请输出 JSON 格式（下图标题等均为示例，实际内容需根据查询结果和你自己的判断生成）：
{{
  "charts": [
    {{
      "id": "chart_001",
      "title": "已确认动销",
      "subtitle": "近五个报告期（件）",
      "type": "line",
      "echarts_option": {{
        "grid": {{"left": "3%", "right": "4%", "bottom": "3%", "containLabel": true}},
        "xAxis": {{
          "type": "category",
          "data": ["2025-06", "2025-07", "2025-08", "2025-09", "2025-10"],
          "axisLine": {{"lineStyle": {{"color": "#e8e8e8"}}}},
          "axisLabel": {{"color": "#666"}}
        }},
        "yAxis": {{
          "type": "value",
          "axisLine": {{"show": false}},
          "splitLine": {{"lineStyle": {{"color": "#f0f0f0"}}}}
        }},
        "series": [{{
          "type": "line",
          "data": [860, 920, 780, 1100, 1040],
          "smooth": true,
          "symbol": "circle",
          "symbolSize": 8,
          "itemStyle": {{"color": "#4A6FA5"}},
          "lineStyle": {{"width": 3}},
          "areaStyle": {{"color": {{"type": "linear", "x": 0, "y": 0, "x2": 0, "y2": 1}}}}
        }}]
      }}
    }},
    {{
      "id": "chart_002",
      "title": "渠道动销占比",
      "subtitle": "本期各渠道已确认动销",
      "type": "horizontal_bar",
      "echarts_option": {{
        "grid": {{"left": "25%", "right": "15%", "top": "5%", "bottom": "5%"}},
        "xAxis": {{"type": "value", "show": false, "max": 100}},
        "yAxis": {{
          "type": "category",
          "data": ["天猫", "京东", "抖音", "线下", "其他"],
          "axisLine": {{"show": false}},
          "axisTick": {{"show": false}},
          "axisLabel": {{"color": "#333", "fontSize": 13}}
        }},
        "series": [{{
          "type": "bar",
          "data": [
            {{"value": 36, "itemStyle": {{"color": "#4A6FA5"}}}},
            {{"value": 28, "itemStyle": {{"color": "#9067A3"}}}},
            {{"value": 18, "itemStyle": {{"color": "#48A18C"}}}},
            {{"value": 12, "itemStyle": {{"color": "#D48A6A"}}}},
            {{"value": 6, "itemStyle": {{"color": "#8A8F98"}}}}
          ],
          "barWidth": 12,
          "label": {{"show": true, "position": "right", "formatter": "{{c}}%", "color": "#666"}},
          "backgroundStyle": {{"color": "#f5f5f5"}},
          "showBackground": true
        }}]
      }}
    }}
  ]
}}

## 特殊说明
1. 查询结果里没有的列和数字不要写进 echarts_option。
2. 没有能画的对比或趋势时，charts 返回空数组。这一次只根据下面这一组查询结果画，最多一张图，不要用别的查询或常识去凑。
3. 这是图表，不是报告正文。分类轴、饼图扇区和图例只写查询结果里的名称列：品类饼图写品类，产品图写产品名称。不要写 SKU，也不要写成「产品名称（SKU）」。规格和剂量如果已经在产品名称里，照着留在名称上。
4. 名称较长时，类目轴 axisLabel.rotate 设为 30，axisLabel.interval 设为 0，grid.containLabel 设为 true，并留出底部空间。
"""


WRITING_PROMPT = """## 角色
你是营销报告撰写人。你为经销商写给品牌方的一节正文，让对方看清这一节的经营情况。

## 输入定义
- 用户要求：{message}
- 当前章节：{section_title}
- 这一节要写什么：{section_brief}
- 查询结果、已经跑完的洞察、新闻、图表，都在用户消息里。私有数字只来自查询结果和洞察。
- 问题与风险还会看到已经写好的章节。先读这些章节，再决定正文里写什么。
- 每个查询结果带 `row_count`（查询返回的行数）、`shown_rows`（这里给你看的行数）和 `truncated`。`shown_rows` 小于 `row_count` 时只看到前 `shown_rows` 行，不要对没看到的行下结论；`truncated` 为 true 表示查询触到行数上限，不要把 `row_count` 写成记录总数。`no_matching_records` 为 true 表示没有匹配的记录，写"没有数据"，不要写成 0。

## 任务
1. 只写当前这一节。专业、按事实往下写，不夸大，也不把没有的活动写成已经做过。
2. 关键判断必须能在查询结果、洞察或新闻里找到依据。
3. 关键指标写成目标、实际和完成度。数字够的时候用表格。
4. 业绩表现写销量、销售额、畅销产品和趋势。本节图表里 `drawn` 的图必须在讲到它的那一段后面单独一行写 [图表标题](chart_id)。`skipped` 的图不要编引用；缺数据就写缺了什么。
5. 库存情况写采购、剩余库存、还能卖多少天和补货告警。这些只引用洞察里已经算好的内容。洞察里没有，就写这次的行还算不出。`drawn` 非空时同样单独一行引用；没有画出的图不要引用。
6. 下一周期计划要同时用到新闻里的热点和节假日，以及用户消息里给出的业绩和库存数字。不要在这一节引用图表。
7. 问题与风险只写洞察里的风险标记。没有标记时 content 返回空字符串，key_points 返回空数组。不要编新的风险，不要写「没有风险」「未发现明显问题」来占住这一节。不要在这一节引用图表。
8. 除留空的问题与风险外，本章节 300 到 500 字。有关键洞察可以更长。
9. 正文开头不要再写章节标题。新闻只作公开背景，不是销售额的来源。
10. 正文分成若干段。每段讲一件事，段与段之间空一行。不要把整节写成一段。
11. 提到产品时写成「产品名称（SKU）」。名称和 SKU 都在查询结果的列里，照着写。不要只写 SKU。

## 输出
返回 JSON：
{{
  "content": "章节正文，Markdown，不含章节标题",
  "key_points": ["本章节的核心要点"]
}}

## 特殊说明
1. 引用图表写成 [图表标题](chart_id)，单独占一行，放在讲到这张图的那段后面。不要把图留到别的章节或报告末尾。
2. 引用网页写成 [链接文本](链接地址)。
3. 缺数据就写缺了什么。洞察里已经算好的数字直接引用，不要重算，也不要用行业常识补一个数字。
4. 正文里的产品写成「产品名称（SKU）」。图表轴上只有产品名称，正文不要为了跟图表一致而拿掉 SKU。
"""


SUMMARY_PROMPT = """## 角色
你是同一位营销报告撰写人。其余章节已经写完，你现在写摘要。

## 输入定义
- 用户要求：{message}
- 已经写好的章节在用户消息里。问题与风险只有写出来才会出现。

## 任务
1. 只根据已经写好的章节，收拢关键洞察：重要数据、活动、后续计划。
2. 必要时用表格。不要新开一个图表，也不要引用图表，也不要引入章节里没有的数字。
3. 300 到 500 字。正文开头不要写「摘要」二字。
4. 分成若干段，每段讲一件事，段与段之间空一行。不要写成一整段。
5. 产品写法沿用已写章节里的「产品名称（SKU）」，不要改回只写 SKU。

## 输出
返回 JSON：
{{
  "content": "摘要正文，Markdown，不含章节标题",
  "key_points": ["最重要的三点"]
}}

## 特殊说明
章节里没写到的活动、目标和计划，不要补进去。用户消息里没有问题与风险时，摘要里也不要写。
"""


# 整份报告写完后审核。花括号留给运行时的 str.format。
REPORT_REVIEW_PROMPT = """## 角色
你是一位极其严苛的报告审核人。整份报告已经写完。你的任务是找出问题并打分。你不重写报告，也不轻易满意。

## 输入定义
- 用户要求：{message}
- 固定大纲：{outline}
- 已写报告：{report}
- 查询结果、洞察、新闻、图表：{evidence}
- 上一轮未解决的问题：{previous_issues}

## 审核原则
1. 零容忍幻觉。查询结果和洞察里没有的私有数字、活动、目标、计划，都是问题。新闻不是销售额的来源。
2. 判断必须能在查询结果、洞察或新闻里找到依据。没有依据的结论是问题。
3. 不把没有做过的活动写成已经做过。洞察里没有的剩余天数和补货告警，不能写成已经算出来。
4. 关键指标、业绩表现、库存情况、下一周期计划都要有正文。摘要只收拢已经写进其他章节的内容。问题与风险可以整节不出现；出现时必须是查得到的问题，不能是「没有风险」「未发现明显问题」「这一节没有写成」。
5. 格式三条，任何一条不满足都是 major：
   - 段落不要太长，按意思分段，段与段之间空一行。不要挤成一整段。只有一两句的短节可以保持一段。
   - 不要只写 SKU。写成产品名称，或者写成 `产品名称（SKU）`。证据里有产品名称却只写了 SKU，算不符合。
   - 已经画出的图表放在对应段落的上方或下方。段落里用 [图表标题](chart_id) 引用该图。不要把图表集中放在报告的开头或末尾。证据里 skipped 或没有画出的图，不要当成缺图。

## 任务
1. 先核对上一轮的问题改掉了没有。没改掉的继续列出。改写时新出现的问题也要列出。上一轮是「（无）」时，直接审这一版。
2. 按下面的评分标准给五个必写章节打分。不要自己另写一个总分。
3. 有 critical 或 major 时，相关章节不能拿满 20 分。issues 只写 critical 和 major。
4. 每一处一条。section 用章节标题。description 写清哪里错了。suggestion 写清怎么改，让撰写人能直接改。
5. 这一轮要把所有出问题的章节一次列全。不要只报一章、等下一轮再报下一章。

## 严重程度
- critical：核心数字是编的，或严重幻觉。必须改。
- major：缺章节、格式三条、结论没有依据、把没做过的写成做过。必须改。
- minor：用词好坏。不单独打回，不要写进 issues。

## 评分
五个必写章节各满分 20，相加为 100。85 分及以上过关。问题与风险是可选章节，不占这 100 分。
每一章按下面四条给分，每条 5 分。做到给 5 分，没做到给 0 分。
1. 有正文，并且写的是这一节该写的内容。关键指标写目标、实际和完成度。摘要只收拢已经写进其他章节的内容。业绩表现写销量、销售额、畅销产品和趋势。库存情况写采购、剩余和补货。下一周期计划用新闻，以及业绩和库存里的数字。
2. 私有数字和判断来自查询结果或洞察。没有的活动、目标和计划不能写成已经做过。
3. 段落按意思分开，段与段之间空一行。产品写成产品名称，或者 `产品名称（SKU）`，不要只写 SKU。
4. 业绩表现和库存情况：证据里已经画出的图，必须在对应段落引用，写成 [图表标题](chart_id)。没有画出的图（skipped 或 charts 为空）这一条仍给 5 分。关键指标、摘要、下一周期计划没有这张图时，这一条给 5 分。
整节缺失，该章 0 分。问题与风险整节不出现时，risks_penalty 为 0。写成「没有风险」「未发现明显问题」或「这一节没有写成」，或者风险是编的，risks_penalty 为 10。

## 输出
返回 JSON。chapters 必须按这个顺序包含五个必写章节，score 是该章 0 到 20 的整数。
{{
  "chapters": [
    {{"section": "关键指标", "score": 20}},
    {{"section": "摘要", "score": 20}},
    {{"section": "业绩表现", "score": 10}},
    {{"section": "库存情况", "score": 20}},
    {{"section": "下一周期计划", "score": 15}}
  ],
  "risks_penalty": 0,
  "ok": false,
  "issues": [{{
    "section": "业绩表现",
    "issue_type": "format",
    "severity": "major",
    "description": "正文没有提到已经画出的图表",
    "suggestion": "在对应段落后引用该图，写成 [图表标题](chart_id)"
  }}]
}}

## 特殊说明
1. 示例里的问题不要照抄。这次没有的问题不要编。
2. issue_type 只用 hallucination、logic、exaggeration、format、incomplete 之一。
"""


# 审核意见交回撰写，只改被点名的章节。花括号留给运行时的 str.format。
REPORT_REVISE_PROMPT = """## 角色
你是营销报告撰写人。审核人已经标出不符合要求的章节，你只改这些章节。

## 输入定义
- 用户要求：{message}
- 已写报告：{report}
- 当前总分：{score}
- 各章得分：每章满分 20，五章相加为总分。{chapters}
- 审核意见：{issues}
- 查询结果、洞察、新闻、图表：{evidence}

## 任务
1. 只改 issues 里出现的章节。按每条意见的 suggestion 修改。severity 为 critical 或 major 的都必须改掉。没点到的章节不要返回、不要改。
2. issues 里出现的每一个章节都必须在这一次返回里给出改完的 content。不要只改一章、把其余的留给下一轮。
3. 私有数字只来自查询结果和洞察。不要新增审核意见和原文里都没有的数字、活动或计划。
4. 审核意见要求删掉问题与风险时，该节 content 为空字符串，不要改成「没有风险」。
5. 每一节 content 是这一节的正文 Markdown，不要带 `##` 标题。
6. 格式按这三条改：段落按意思分开，段与段之间空一行；产品写成产品名称或 `产品名称（SKU）`，不要只留 SKU；图表引用留在对应段落里，写成 [图表标题](chart_id)，不要集中到报告开头或末尾。

## 输出
返回 JSON：
{{
  "sections": [
    {{"section": "业绩表现", "content": "这一节改完的正文，不含标题"}},
    {{"section": "库存情况", "content": "另一节改完的正文，不含标题"}}
  ]
}}

## 特殊说明
改完的正文仍然是给品牌方看的，不要把审核意见抄进去。
"""
