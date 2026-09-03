# 外部真话长尾集 · 多轮续问（能不能办成）

- dataset: `external-longtail-v1` v2 · sha `74f4429d92f8889c…` · mode `model_live_provider`
- reference_date: `2026-09-02` · 最多续问 3 轮
- 用例 185（可完成 142，供应商无映射 43）
- 红线 **185/185**，崩溃 0
- **端到端完成率 90.8%**（129/142），按续问轮数 {"0": 5, "1": 124}
- 无映射城市的诚实失败率 83.7%
- 真搜了交通的 145 条里，搜的日期与事实表一致 95.9%；弱真值门禁触发 145 次，失败 0
- 终态：{"honest_failure": 46, "completed": 135, "out_of_scope": 3, "stuck_clarifying": 1}

| 来源 | 用例 | 可完成 | 办成 | 终态分布 |
|---|---|---|---|---|
| LAMDA-NeSy/ChinaTravel | 154 | 119 | 112 | {"honest_failure": 40, "completed": 112, "out_of_scope": 2} |
| google/air_dialogue | 31 | 23 | 17 | {"completed": 23, "honest_failure": 6, "stuck_clarifying": 1, "out_of_scope": 1} |

## 续问三轮仍在追问（前 10 条，最后一问）

- ad-val-0030: SFO and OAK are both Bay Area airports about 15 miles apart, and no airline flies between them, so there's no flight inventory to search. What's the real origin

## 限制

- 模拟旅行者是脚本：只交事实表里的日期和路线，不回答别的；系统问了别的就得不到答案。
- 事实表的日期是固定推出来的，不是原话里的；弱真值只核对月/日与来源声明一致。
- 完成率的分母只算两家沙箱供应商都能映射的路线；苏州这类只量诚实失败。
