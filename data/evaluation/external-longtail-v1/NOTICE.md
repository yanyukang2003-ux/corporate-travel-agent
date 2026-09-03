# External Long-Tail Probe Dataset v1

本目录是**派生评测数据**，不用于训练，不用于生产。

## 来源与许可

- [LAMDA-NeSy/ChinaTravel](https://huggingface.co/datasets/LAMDA-NeSy/ChinaTravel)
  （default/human，CC BY-NC-SA 4.0）：`nature_language` 用户原话，逐字保留。
- [thu-coai/CrossWOZ](https://github.com/thu-coai/CrossWOZ)
  （data/crosswoz/test.json，Apache-2.0）：每段对话用户侧第一句实质发言。
- [google/air_dialogue](https://huggingface.co/datasets/google/air_dialogue)
  （validation，Apache-2.0）：顾客第一句实质请求。

来源的回答、系统侧对话、参考方案、标注**一概不用作标准答案**。
含 ChinaTravel 派生内容，本目录整体按 **CC BY-NC-SA 4.0** 对待：仅评测使用。

## 弱真值（v2 起）

每条带 `weak_truth`：来源对**输入本身**的结构化描述（出发/目标城市或机场、声明过的
日期）。它不是"标准答案"——是"用户到底说了哪些地方和日子"的第二个独立出处。
探针据此加两条门禁：**若系统执行了搜索**，(a) 搜索的地点必须落在声明集合里或原话里；
(b) 搜索的日期必须与声明日期一致（没有声明日期的来源，则每次交通搜索必须带原话出处）。
AirDialogue 的人名是数据集生成的合成人物。

## 期望是什么

每条只有机器可校验的红线（`probe: host_invariants_only`）：不崩、不产生预订、
不编造库存引用、用户可见文字不声称已下单。终态分布（澄清/越界/搜索/无方案）
是**测量**不是门禁——这些问法多数本来就不是企业差旅。
