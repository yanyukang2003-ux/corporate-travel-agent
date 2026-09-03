# 外部真话长尾集 · 多轮续问（能不能办成）

- dataset: `external-longtail-v1` v2 · sha `74f4429d92f8889c…` · mode `model_live_provider`
- reference_date: `2026-09-02` · 最多续问 3 轮
- 用例 6（可完成 5，供应商无映射 1）
- 红线 **5/6**，崩溃 0
- **端到端完成率 80.0%**（4/5），按续问轮数 {"1": 4}
- 无映射城市的诚实失败率 100.0%
- 真搜了交通的 4 条里，搜的日期与事实表一致 75.0%；弱真值门禁触发 4 次，失败 1
- 终态：{"honest_failure": 1, "completed": 4, "out_of_scope": 1}

| 来源 | 用例 | 可完成 | 办成 | 终态分布 |
|---|---|---|---|---|
| LAMDA-NeSy/ChinaTravel | 3 | 2 | 1 | {"honest_failure": 1, "completed": 1, "out_of_scope": 1} |
| google/air_dialogue | 3 | 3 | 3 | {"completed": 3} |

## 红线未通过

- ad-val-0003: {'searched_dates_match_declaration': False, 'searched_dates_match_fact_sheet': False}

## 限制

- 模拟旅行者是脚本：只交事实表里的日期和路线，不回答别的；系统问了别的就得不到答案。
- 事实表的日期是固定推出来的，不是原话里的；弱真值只核对月/日与来源声明一致。
- 完成率的分母只算两家沙箱供应商都能映射的路线；苏州这类只量诚实失败。
