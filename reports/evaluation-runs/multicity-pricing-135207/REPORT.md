# 多城报价：分段问价 vs 一次多段请求

- measured_at: `2026-08-29T17:53:36.155385+00:00`
- runner: `multicity-pricing-measure-v1`
- 出发日: `2026-10-13`（相对今天 +45 天）
- Duffel 沙箱只读查询 **15 次**，0 次模型调用

| 行程 | 分段合计 | 一次多段 | 差价 | 谁更便宜 |
|---|---:|---:|---:|---|
| MP-01 | 171.30 USD | 112.26 USD | -59.04（-34.5%） | **一次多段** |
| MP-02 | 506.14 USD | 429.91 USD | -76.23（-15.1%） | **一次多段** |
| MP-03 | 287.88 USD | 241.57 USD | -46.31（-16.1%） | **一次多段** |
| MP-04 | 136.80 USD | 105.20 USD | -31.60（-23.1%） | **一次多段** |

## 逐条

### MP-01 · 北京 → 上海 → 杭州 → 北京（国内三段）

- 分段问价：{"legs": [{"leg": "PEK-SHA", "offers": 43, "cheapest": "67.50", "currency": "USD"}, {"leg": "SHA-HGH", "offers": 2, "cheapest": "34.61", "currency": "USD"}, {"leg": "HGH-PEK", "offers": 40, "cheapest": "69.19", "currency": "USD"}], "complete": true, "total": "171.30", "currency": "USD", "requests": 3}
- 一次多段：{"offers": 1, "offers_covering_every_leg": 1, "cheapest": "112.26", "currency": "USD", "requests": 1}

### MP-02 · 北京 → 东京 → 新加坡 → 北京（跨境三段）

- 分段问价：{"legs": [{"leg": "PEK-TYO", "offers": 44, "cheapest": "101.07", "currency": "USD"}, {"leg": "TYO-SIN", "offers": 41, "cheapest": "216.29", "currency": "USD"}, {"leg": "SIN-PEK", "offers": 27, "cheapest": "188.78", "currency": "USD"}], "complete": true, "total": "506.14", "currency": "USD", "requests": 3}
- 一次多段：{"offers": 1, "offers_covering_every_leg": 1, "cheapest": "429.91", "currency": "USD", "requests": 1}

### MP-03 · 上海 → 香港 → 曼谷 → 上海（跨境三段）

- 分段问价：{"legs": [{"leg": "SHA-HKG", "offers": 46, "cheapest": "74.26", "currency": "USD"}, {"leg": "HKG-BKK", "offers": 49, "cheapest": "88.36", "currency": "USD"}, {"leg": "BKK-SHA", "offers": 32, "cheapest": "125.26", "currency": "USD"}], "complete": true, "total": "287.88", "currency": "USD", "requests": 3}
- 一次多段：{"offers": 7042, "offers_covering_every_leg": 7042, "cheapest": "241.57", "currency": "USD", "requests": 1}

### MP-04 · **对照组：普通往返**（北京 ⇄ 上海）

- 分段问价：{"legs": [{"leg": "PEK-SHA", "offers": 43, "cheapest": "69.36", "currency": "USD"}, {"leg": "SHA-PEK", "offers": 48, "cheapest": "67.44", "currency": "USD"}], "complete": true, "total": "136.80", "currency": "USD", "requests": 2}
- 一次多段：{"offers": 2074, "offers_covering_every_leg": 2074, "cheapest": "105.20", "currency": "USD", "requests": 1}

