# 派生企业差旅评测集

## V2 范围

`data/evaluation/derived-v2` 是离线、确定性、可审计的测试数据集，共 540 条：

- PreferTripPlan 派生工作流案例 60 条；
- 意图案例 480 条：英文 225 条、中文 255 条；
- 真实 Provider 快照 0 条。

V1 的 30 条意图案例存在明显的选择偏差：比较类由“高铁+飞机”关键词筛选后取前
15 条，其他类别也主要采用关键词或前 N 条。V2 不再按解析器能力挑题：

- 纳入 PreferTripPlan 数据卡推荐的完整 225 条 curated `test`；
- 纳入 Open-Travel 完整 250 条分类 `test`；
- 另保留 V1 的 5 条 Open-Travel `train` 缺字段案例，用于历史连续对比。

30 条旧案例标记为 `LEGACY_TARGETED`，其余 450 条标记为
`CURATED_FULL_SPLIT`，验证器会分别输出指标，避免总体均值掩盖选择偏差。

## 意图覆盖

| 场景 | 数量 | 来源 | 金标依据 |
|---|---:|---|---|
| 偏好丰富的多日行程 | 225 | PreferTripPlan | 结构化城市、日期、城市数、约束和偏好谓词 |
| 交通方式比较 | 50 | Open-Travel | `compare_itinerary` 子任务及显式交通词 |
| 多日旅游 | 50 | Open-Travel | `multi_day_travel` 子任务 |
| 多点路线规划 | 50 | Open-Travel | `direction` 子任务；当前企业差旅边界外 |
| 单日城市游 | 50 | Open-Travel | `one_day_travel` 子任务；当前企业差旅边界外 |
| 周边 POI 搜索 | 50 | Open-Travel | `search_around` 子任务；当前企业差旅边界外 |
| 旧版缺字段 | 5 | Open-Travel train | V1 人工复核标签，仅作连续对比 |

其中 305 条明确包含当前领域不支持的要求，例如完整景点/餐饮行程、多城市行程、
自驾模式或未建模的复合偏好；这些要求必须被显式拒绝或报告冲突，不能静默忽略。

## 指标口径

所有 480 条均统计分类、澄清、提前 Provider 调用和库存幻觉。维度型指标只在有可靠
金标时计分：

- 缺失字段：250 条；
- 交通偏好：27 条；
- 不支持约束拒绝：305 条；
- 越界识别：150 条。

“不适用”不再作为空集合命中。例如没有交通信号的案例不会进入交通偏好准确率的
分母。无适用样本的切片输出 `null`，不会显示成误导性的 100%。

## 当前离线基线

确定性解析器的 V2 结果证明旧数据确实过于迎合：

| 指标 | 旧 30 条 | 新增 450 条 | 全部 480 条 |
|---|---:|---:|---:|
| 分类准确率 | 100.0% | 6.9% | 12.7% |
| 缺失字段精确集合匹配 | 80.0% | 0.0% | 8.0% |
| 缺失字段精确率 | 87.0% | 25.0% | 28.5% |
| 缺失字段召回率 | 100.0% | 33.3% | 37.7% |
| 澄清准确率 | 100.0% | 70.0% | 71.9% |
| 越界识别率 | 100.0% | 6.9% | 10.0% |
| 交通偏好识别 | 93.8% | 90.9% | 92.6% |
| 不支持约束拒绝率 | 0.0% | 0.0% | 0.0% |
| 提前 Provider 调用 | 0.0% | 0.0% | 0.0% |
| 库存幻觉 | 0.0% | 0.0% | 0.0% |

这组结果是安全离线基线，不是生产模型成绩。它暴露了英文解析、路线/单日游范围识别、
自驾与复杂偏好拒绝等明显缺口；安全闸门只要求提前 Provider 调用和库存幻觉保持为零。

## 数据边界与许可

- 工作流库存、员工、政策、会议窗口、审批和故障均由固定规则生成，库存类型为
  `MOCK`，不能计入真实 Provider 覆盖。
- 意图案例保留原始 query；源数据中的模型答案、思维过程、工具轨迹和空工具结果不
  作为金标。
- PreferTripPlan：数据卡声明 Apache-2.0，并提示继承的 TravelPlanner 内容继续遵守
  上游条款。
- Open-Travel：CC BY-NC 4.0，仅用于非商业评测，商业使用前必须另行确认许可。

精确来源版本、源文件 SHA-256、转换说明和逐条记录 ID 位于 manifest、case 和
`data/evaluation/derived-v2/NOTICE.md`。

## 校验

```bash
.venv/bin/python examples/validate_evaluation_dataset.py \
  data/evaluation/derived-v2
```

该命令校验 manifest、JSONL schema、文件哈希、来源和案例唯一性，运行 60 条工作流
与 480 条意图，并按 cohort、场景和来源输出指标及失败案例 ID。

配置 `OPENAI_API_KEY` 后可用同一套案例运行真实模型适配器：

```bash
.venv/bin/python examples/validate_evaluation_dataset.py \
  data/evaluation/derived-v2 --intent-runner openai
```

## 重建

转换器只接受固定提交和源文件 SHA-256，且拒绝覆盖已有输出目录：

```bash
.venv/bin/python examples/build_derived_evaluation_dataset.py \
  --prefertripplan-file /path/to/prefertripplan_test.jsonl \
  --open-travel-directory /path/to/open-travel-files \
  --output-directory data/evaluation/derived-v2-new
```

转换器不调用 LLM、Provider、预订或支付接口。
