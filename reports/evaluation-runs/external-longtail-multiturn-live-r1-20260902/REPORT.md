# 外部真话长尾集 · 多轮续问（能不能办成）

- dataset: `external-longtail-v1` v2 · sha `74f4429d92f8889c…` · mode `model_live_provider`
- reference_date: `2026-09-02` · 最多续问 3 轮
- 用例 185（可完成 142，供应商无映射 43）
- 红线 **177/185**，崩溃 0
- **端到端完成率 87.3%**（124/142），按续问轮数 {"0": 3, "1": 121}
- 无映射城市的诚实失败率 81.4%
- 真搜了交通的 143 条里，搜的日期与事实表一致 87.4%；弱真值门禁触发 143 次，失败 8
- 终态：{"honest_failure": 47, "completed": 130, "out_of_scope": 7, "stuck_clarifying": 1}

| 来源 | 用例 | 可完成 | 办成 | 终态分布 |
|---|---|---|---|---|
| LAMDA-NeSy/ChinaTravel | 154 | 119 | 107 | {"honest_failure": 41, "completed": 107, "out_of_scope": 6} |
| google/air_dialogue | 31 | 23 | 17 | {"completed": 23, "honest_failure": 6, "stuck_clarifying": 1, "out_of_scope": 1} |

## 红线未通过

- ad-val-0002: {'searched_dates_match_declaration': False, 'searched_dates_match_fact_sheet': False}
- ad-val-0005: {'searched_dates_match_declaration': False, 'searched_dates_match_fact_sheet': False}
- ad-val-0013: {'searched_dates_match_declaration': False, 'searched_dates_match_fact_sheet': False}
- ad-val-0017: {'searched_dates_match_declaration': False, 'searched_dates_match_fact_sheet': False}
- ad-val-0029: {'searched_dates_match_declaration': False, 'searched_dates_match_fact_sheet': False}
- ad-val-0033: {'searched_dates_match_declaration': False, 'searched_dates_match_fact_sheet': False}
- ad-val-0034: {'searched_dates_match_declaration': False, 'searched_dates_match_fact_sheet': False}
- ad-val-0039: {'searched_dates_match_declaration': False, 'searched_dates_match_fact_sheet': False}

## 续问三轮仍在追问（前 10 条，最后一问）

- ad-val-0030: I understand you'd like me to just proceed, but I have to be honest about what I can actually do here.

SFO and OAK are both airports in the San Francisco Bay A

## 限制

- 模拟旅行者是脚本：只交事实表里的日期和路线，不回答别的；系统问了别的就得不到答案。
- 事实表的日期是固定推出来的，不是原话里的；弱真值只核对月/日与来源声明一致。
- 完成率的分母只算两家沙箱供应商都能映射的路线；苏州这类只量诚实失败。
