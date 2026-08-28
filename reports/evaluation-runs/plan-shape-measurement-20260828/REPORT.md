# 走法枚举规模测量

HANDOFF §30.6 要求的「开工前先量一次」。不花钱，不调模型。

## 结论（三句话）

1. **走法数很小。** 最坏情况（6 段，每段飞机高铁都有货，住宿可选可不选）也只有 **128 种走法**，穷举毫无压力。
2. **枚举走法不多花一次库存查询。** 一段查一次就把这段所有交通方式都拿回来了；最坏情况仍然是 **7 次查询**（每段一次 + 住宿一次），和今天完全一样。
3. **要评的报价组合一个不少，也一个不多。** 走法是对同一批组合的**分组**，不是筛选：冻结评测的 60 个世界里今天平均评 3.67 格、最多 8 格，按走法分组之后总数不变。所以第 05 步改的是**摆出来的是什么**，不是快多少——别把它当性能优化。

## 详细数字

### 冻结评测数据的 60 个世界

| 指标 | 最小 | 平均 | 最大 |
|---|---:|---:|---:|
| 今天的组合数（笛卡尔积格子） | 1 | 3.67 | 8 |
| 走法数 | 1 | 6.52 | 8 |

每段实际拿得到几种交通方式（段数计数）：{1: 16, 2: 104}。

> 读这一行要小心：真实 Provider（Duffel）**只返回航班**，高铁库存今天只存在于Mock 与评测夹具里。所以「按交通方式枚举走法」在真实链路上现在只有一种取值，价值要等接入铁路库存才兑现。

### 按段数扫一遍（最坏情况）

| 航段数 | 每段可选方式 | 住宿 | 走法数 | 库存查询次数 |
|---:|---:|---|---:|---:|
| 1 | 1 | no_lodging | 1 | 1 |
| 1 | 1 | lodging_required | 1 | 2 |
| 1 | 1 | lodging_optional | 2 | 2 |
| 1 | 2 | no_lodging | 2 | 1 |
| 1 | 2 | lodging_required | 2 | 2 |
| 1 | 2 | lodging_optional | 4 | 2 |
| 2 | 1 | no_lodging | 1 | 2 |
| 2 | 1 | lodging_required | 1 | 3 |
| 2 | 1 | lodging_optional | 2 | 3 |
| 2 | 2 | no_lodging | 4 | 2 |
| 2 | 2 | lodging_required | 4 | 3 |
| 2 | 2 | lodging_optional | 8 | 3 |
| 3 | 1 | no_lodging | 1 | 3 |
| 3 | 1 | lodging_required | 1 | 4 |
| 3 | 1 | lodging_optional | 2 | 4 |
| 3 | 2 | no_lodging | 8 | 3 |
| 3 | 2 | lodging_required | 8 | 4 |
| 3 | 2 | lodging_optional | 16 | 4 |
| 4 | 1 | no_lodging | 1 | 4 |
| 4 | 1 | lodging_required | 1 | 5 |
| 4 | 1 | lodging_optional | 2 | 5 |
| 4 | 2 | no_lodging | 16 | 4 |
| 4 | 2 | lodging_required | 16 | 5 |
| 4 | 2 | lodging_optional | 32 | 5 |
| 5 | 1 | no_lodging | 1 | 5 |
| 5 | 1 | lodging_required | 1 | 6 |
| 5 | 1 | lodging_optional | 2 | 6 |
| 5 | 2 | no_lodging | 32 | 5 |
| 5 | 2 | lodging_required | 32 | 6 |
| 5 | 2 | lodging_optional | 64 | 6 |
| 6 | 1 | no_lodging | 1 | 6 |
| 6 | 1 | lodging_required | 1 | 7 |
| 6 | 1 | lodging_optional | 2 | 7 |
| 6 | 2 | no_lodging | 64 | 6 |
| 6 | 2 | lodging_required | 64 | 7 |
| 6 | 2 | lodging_optional | 128 | 7 |

## 直接回答 §30.6 的两个问题

- **三段行程会枚举出多少种走法？** 最多 16 种（三段各有飞机和高铁两种货，住宿可选可不选）。
- **每种走法要花多少次库存查询？** 0 次。整趟一共 4 次（每段一次 + 住宿一次），枚举走法完全发生在这些查询之后。

## 因此

第 05 步可以做：规模不是问题，也不会多花 Provider 的钱。
真正要小心的是**它现在能兑现多少**——见上面那条关于 Duffel 只有航班的提醒。
