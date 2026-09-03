# AGENTS.md

给在这个仓库里工作的 AI 助手的说明。

## 沟通标准（最重要）

**用简单清晰的语言。遇到专业名词，先解释再用。**

具体要求：

1. **先解释，后使用。** 任何专业名词、缩写、内部概念，第一次出现时必须先用一句
   大白话说清它是什么，再往下讲。例如不要直接写「harness 的 D2 集」，要写
   「评测集 D2（480 条用户原话，用来检查系统能不能读懂需求）」。

2. **先给结论，再给细节。** 开头一两句话说清楚"结果是什么、能不能用"，
   想深入的人再往下看。不要让人读三段才知道成没成。

3. **用日常词替代行话。** 能说「系统会先问清楚再去查」就不要说
   「orchestrator 在 compile 阶段拒绝并进入 clarification 状态」。
   必须提到代码里的名字时，把它放在解释之后的括号里。

4. **不用没解释过的缩写。** D1/D15、WORM、PCI DSS、BSP、offer、handoff、
   rubric 这类词，每次在新的对话里首次出现都要解释一遍。

5. **数字要说明含义。** 不要只写「clarification_accuracy 0.719」，
   要写「该问就问、该查就查的判断准确率 71.9%」。

6. **诚实优先于漂亮。** 没做的就说没做，没验证的就说没验证，
   不确定的就说不确定。不要用模糊措辞掩盖缺口。

## 项目背景一句话

这是一个企业差旅助手：员工用大白话说出行需求，系统读懂需求 → 查航班酒店 →
按公司差旅规定检查是否合规 → 需要时走审批 → 最后把方案交给员工自己去官方平台下单。

**系统不会自动下单，也不会付钱。** 这是硬边界，不是还没做完。

## 硬性技术约束

改代码前必须知道的几条红线：

- **不许自动预订或付款。** `providers/duffel.py` 和 `providers/liteapi.py` 里有
  开关会主动拒绝启用真实预订，别绕过它们。
- **政策结论由确定性代码判定，不由 AI 判定。** AI 只负责读懂用户在说什么，
  以及解释已经查到的事实。
- **模糊就问，不要猜。** 系统宁可多问一句，也不能自己编一个日期或城市去查库存。
- **评测报告目录必须是新的。** 各个 runner 都要求输出目录事先不存在，
  这是为了不覆盖历史证据。
- **改了提示词或校准规则，旧的评测数字就不能直接对比了。** 要新开报告目录。

## 常用命令

```bash
# 全量测试
.venv/bin/python -m pytest tests -q

# 代码检查
.venv/bin/ruff check src tests examples migrations

# 静态类型检查（配置在 pyproject.toml；domain / policy / workflow / 仓储端口 / 编排器为严格模式）
.venv/bin/mypy

# 前端（连本机 API；8000 被占时换端口）
cd frontend && npm run build && npm test && npm run lint
cd frontend && VITE_API_TARGET=http://127.0.0.1:8001 npm run dev

# 启动 API（本机没开 Postgres 时必须清空 DATABASE_URL）
set -a && . ./.env && set +a && export DATABASE_URL= && \
  .venv/bin/uvicorn corporate_travel_agent.api.main:app --host 127.0.0.1 --port 8000
```

## 交接文档

- `README.md` —— 功能全貌与运行方式
- `HANDOFF.md` —— 历次开发的详细交接记录（很长，按章节查）
- `docs/evaluation-protocol.md` —— 评测怎么做、门禁是什么
- `docs/adr/` —— 重要架构决策及其理由
- `docs/product-gap-review.md` —— 对照产品设计思路的缺口盘点与改进优先级
