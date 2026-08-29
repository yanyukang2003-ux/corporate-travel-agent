# 多城报价：分段问价 vs 一次多段请求

- measured_at: `2026-08-29T17:55:25.228349+00:00`
- runner: `multicity-pricing-measure-v1`
- 出发日: `2026-10-28`（相对今天 +60 天）
- Duffel 沙箱只读查询 **15 次**，0 次模型调用

| 行程 | 分段合计 | 一次多段 | 差价 | 谁更便宜 |
|---|---:|---:|---:|---|
| MP-01 | 477.73 USD | 114.41 USD | -363.32（-76.1%） | **一次多段** |
| MP-02 | 553.43 USD | 428.57 USD | -124.86（-22.6%） | **一次多段** |
| MP-03 | 298.75 USD | 238.47 USD | -60.28（-20.2%） | **一次多段** |
| MP-04 | 132.93 USD | 108.90 USD | -24.03（-18.1%） | **一次多段** |

## 逐条

### MP-01 · 北京 → 上海 → 杭州 → 北京（国内三段）

- 分段问价：{"legs": [{"leg": "PEK-SHA", "offers": 44, "cheapest": "67.05", "currency": "USD"}, {"leg": "SHA-HGH", "offers": 1, "cheapest": "34.58", "currency": "USD"}, {"leg": "HGH-PEK", "offers": 30, "cheapest": "376.10", "currency": "USD"}], "complete": true, "total": "477.73", "currency": "USD", "requests": 3}
- 一次多段：{"offers": 1, "offers_covering_every_leg": 1, "cheapest": "114.41", "currency": "USD", "requests": 1}

### MP-02 · 北京 → 东京 → 新加坡 → 北京（跨境三段）

- 分段问价：{"legs": [{"leg": "PEK-TYO", "offers": 44, "cheapest": "101.27", "currency": "USD"}, {"leg": "TYO-SIN", "offers": 39, "cheapest": "215.36", "currency": "USD"}, {"leg": "SIN-PEK", "offers": 34, "cheapest": "236.80", "currency": "USD"}], "complete": true, "total": "553.43", "currency": "USD", "requests": 3}
- 一次多段：{"offers": 1, "offers_covering_every_leg": 1, "cheapest": "428.57", "currency": "USD", "requests": 1}

### MP-03 · 上海 → 香港 → 曼谷 → 上海（跨境三段）

- 分段问价：{"legs": [{"leg": "SHA-HKG", "offers": 48, "cheapest": "72.32", "currency": "USD"}, {"leg": "HKG-BKK", "offers": 49, "cheapest": "88.63", "currency": "USD"}, {"leg": "BKK-SHA", "offers": 34, "cheapest": "137.80", "currency": "USD"}], "complete": true, "total": "298.75", "currency": "USD", "requests": 3}
- 一次多段：{"offers": 5796, "offers_covering_every_leg": 5796, "cheapest": "238.47", "currency": "USD", "requests": 1}

### MP-04 · **对照组：普通往返**（北京 ⇄ 上海）

- 分段问价：{"legs": [{"leg": "PEK-SHA", "offers": 44, "cheapest": "65.80", "currency": "USD"}, {"leg": "SHA-PEK", "offers": 48, "cheapest": "67.13", "currency": "USD"}], "complete": true, "total": "132.93", "currency": "USD", "requests": 2}
- 一次多段：{"offers": 1465, "offers_covering_every_leg": 1465, "cheapest": "108.90", "currency": "USD", "requests": 1}

