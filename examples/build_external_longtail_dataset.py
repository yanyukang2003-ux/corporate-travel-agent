#!/usr/bin/env python3
"""构建外部真话长尾集 external-longtail-v1：三个公开数据集里的**真实用户问法**。

## 为什么要这个集

仓库现有的长尾用例（§41）是自己写的 28 条；D2 的 480 条来自两个数据源。这里再补
约 300 条**别人家真实用户**的原话——中文自由行（ChinaTravel human split，1154 名
真人参与者写的）、中文口语对话首句（CrossWOZ）、英文订票对话首句（AirDialogue）。
它们大多不是标准企业差旅：带孩子旅游、找美食街、退改签……**这正是价值所在**：
量的是系统在门外汉问法面前守不守得住红线（不崩、不下单、不编库存、不声称已订），
以及落在哪个终态（澄清/越界/搜索）——终态分布是测量，不是门禁。

## 许可与署名（详见生成的 NOTICE.md）

- ChinaTravel（LAMDA-NeSy）：CC BY-NC-SA 4.0 —— 仅评测使用，衍生文件同许可标注。
- CrossWOZ（thu-coai）：Apache-2.0。
- AirDialogue（Google）：Apache-2.0。

原始回答、系统侧对话、参考方案一概不用作标准答案——期望只有机器可校验的红线。

```bash
.venv/bin/python examples/build_external_longtail_dataset.py \\
  --raw-dir /tmp/external-raw --output data/evaluation/external-longtail-v1
```

raw-dir 里缺哪个文件就现场下载哪个；已存在的用现成的（内容哈希会记进 manifest）。
输出目录已存在时必须带 --force 才覆盖（重建会改哈希，等于换了一版数据集）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

DATASET_ID = "external-longtail-v1"
DATASET_VERSION = "2"
HF_ROWS = "https://datasets-server.huggingface.co/rows"
SOURCES = {
    "chinatravel_human_0.json": (
        f"{HF_ROWS}?dataset=LAMDA-NeSy%2FChinaTravel&config=default&split=human"
        "&offset=0&length=100"
    ),
    "chinatravel_human_1.json": (
        f"{HF_ROWS}?dataset=LAMDA-NeSy%2FChinaTravel&config=default&split=human"
        "&offset=100&length=100"
    ),
    "crosswoz_test.json.zip": (
        "https://raw.githubusercontent.com/thu-coai/CrossWOZ/master/data/crosswoz/test.json.zip"
    ),
    "airdialogue_val_0.json": (
        f"{HF_ROWS}?dataset=google%2Fair_dialogue&config=air_dialogue_data&split=validation"
        "&offset=0&length=100"
    ),
}
CHINATRAVEL_LICENSE = "CC BY-NC-SA 4.0"
CROSSWOZ_LICENSE = "Apache-2.0"
AIRDIALOGUE_LICENSE = "Apache-2.0"
GREETINGS = {"hello.", "hello", "hi.", "hi", "hello there.", "hi there."}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fetch(raw_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name, url in SOURCES.items():
        target = raw_dir / name
        if not target.exists():
            print(f"downloading {name} …")
            with urllib.request.urlopen(url, timeout=120) as response:
                target.write_bytes(response.read())
        hashes[name] = _sha256(target)
    return hashes


def _chinatravel_cases(raw_dir: Path) -> list[dict]:
    cases = []
    for name in ("chinatravel_human_0.json", "chinatravel_human_1.json"):
        payload = json.loads((raw_dir / name).read_text())
        for item in payload["rows"]:
            row = item["row"]
            message = str(row.get("nature_language") or "").strip()
            if not message:
                continue
            cases.append(
                {
                    "case_id": f"ct-human-{row['uid']}",
                    "language": "zh",
                    "message": message,
                    "source": {
                        "dataset": "LAMDA-NeSy/ChinaTravel",
                        "config_split": "default/human",
                        "record_id": row["uid"],
                        "license": CHINATRAVEL_LICENSE,
                        "url": "https://huggingface.co/datasets/LAMDA-NeSy/ChinaTravel",
                    },
                    # 弱真值：来源对**输入本身**的结构化描述（不是答案）。
                    # 口径与本项目一致：若系统去搜了，城市必须落在声明集合里。
                    "weak_truth": {
                        "cities": [
                            str(row.get("start_city") or ""),
                            str(row.get("target_city") or ""),
                        ],
                        "days": str(row.get("days") or ""),
                        "people_number": str(row.get("people_number") or ""),
                        "dates_declared": [],
                    },
                    "notes": [
                        "真人写的中文自由行需求（多天/多人/景点餐馆约束），多数超出企业差旅范围",
                        "方括号前缀是数据集原文的一部分，逐字保留",
                    ],
                }
            )
    return cases


def _crosswoz_cases(raw_dir: Path, limit: int = 100) -> list[dict]:
    with zipfile.ZipFile(raw_dir / "crosswoz_test.json.zip") as archive:
        with archive.open("test.json") as handle:
            data = json.load(handle)
    picked: list[dict] = []
    # 先挑首个目标域是酒店/地铁/出租的（贴近本项目），再用其余域补满；键排序保证确定性。
    def first_domain(dialogue: dict) -> str:
        goal = dialogue.get("goal") or []
        return str(goal[0][1]) if goal and len(goal[0]) > 1 else ""

    ordered = sorted(data.items())
    preferred = [(k, d) for k, d in ordered if first_domain(d) in {"酒店", "地铁", "出租"}]
    rest = [(k, d) for k, d in ordered if first_domain(d) not in {"酒店", "地铁", "出租"}]
    for key, dialogue in [*preferred, *rest]:
        if len(picked) >= limit:
            break
        message = next(
            (
                str(m.get("content") or "").strip()
                for m in dialogue.get("messages", [])
                if m.get("role") == "usr" and len(str(m.get("content") or "").strip()) >= 8
            ),
            "",
        )
        if not message:
            continue
        picked.append(
            {
                "case_id": f"cw-{key}",
                "language": "zh",
                "message": message,
                "source": {
                    "dataset": "thu-coai/CrossWOZ",
                    "config_split": "test.json",
                    "record_id": key,
                    "license": CROSSWOZ_LICENSE,
                    "url": "https://github.com/thu-coai/CrossWOZ",
                    "first_goal_domain": first_domain(dialogue),
                },
                "weak_truth": {
                    "cities": ["北京"],
                    "dates_declared": [],
                    "note": "CrossWOZ 全部发生在北京；站点/景点名以原话为准",
                },
                "notes": ["北京旅游场景的口语对话首句（餐馆/景点/酒店/地铁/出租）"],
            }
        )
    return picked


def _airdialogue_cases(raw_dir: Path, limit: int = 46) -> list[dict]:
    payload = json.loads((raw_dir / "airdialogue_val_0.json").read_text())
    picked: list[dict] = []
    for index, item in enumerate(payload["rows"]):
        if len(picked) >= limit:
            break
        row = item["row"]
        message = ""
        for turn in row.get("dialogue") or []:
            text = str(turn)
            if not text.startswith("customer: "):
                continue
            content = text[len("customer: ") :].strip()
            if content.lower() in GREETINGS or len(content) < 12:
                continue
            message = content
            break
        if not message:
            continue
        picked.append(
            {
                "case_id": f"ad-val-{index:04d}",
                "language": "en",
                "message": message,
                "source": {
                    "dataset": "google/air_dialogue",
                    "config_split": "air_dialogue_data/validation",
                    "record_id": str(index),
                    "license": AIRDIALOGUE_LICENSE,
                    "url": "https://huggingface.co/datasets/google/air_dialogue",
                },
                "weak_truth": {
                    "cities": [
                        str(row["intent"].get("departure_airport") or ""),
                        str(row["intent"].get("return_airport") or ""),
                    ],
                    "dates_declared": [
                        {
                            "month": str(row["intent"].get("departure_month") or ""),
                            "day": str(row["intent"].get("departure_day") or ""),
                        },
                        {
                            "month": str(row["intent"].get("return_month") or ""),
                            "day": str(row["intent"].get("return_day") or ""),
                        },
                    ],
                    "source_goal": str(row["intent"].get("goal") or ""),
                },
                "notes": [
                    "英文订票对话里顾客的第一句实质请求（订/改/退机票）",
                    "人名是数据集生成的合成人物，不是真实个人",
                    "首句常不含机场/日期——该澄清是对的；弱真值只在真的搜索时生效",
                ],
            }
        )
    return picked


NOTICE = """# External Long-Tail Probe Dataset v1

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
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.output.exists() and not args.force:
        raise SystemExit(f"输出目录已存在（重建会换哈希）：{args.output}；确认要重建加 --force")
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    raw_hashes = _fetch(args.raw_dir)

    cases = [
        *_chinatravel_cases(args.raw_dir),
        *_crosswoz_cases(args.raw_dir),
        *_airdialogue_cases(args.raw_dir),
    ]
    for case in cases:
        case["probe"] = "host_invariants_only"

    args.output.mkdir(parents=True, exist_ok=True)
    lines = "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases)
    (args.output / "cases.jsonl").write_text(lines, encoding="utf-8")
    by_source: dict[str, int] = {}
    by_language: dict[str, int] = {}
    for case in cases:
        by_source[case["source"]["dataset"]] = by_source.get(case["source"]["dataset"], 0) + 1
        by_language[case["language"]] = by_language.get(case["language"], 0) + 1
    manifest = {
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "classification": "EXTERNAL_DERIVED",
        "created_at": datetime.now(UTC).isoformat(),
        "records": len(cases),
        "cases_sha256": hashlib.sha256(lines.encode("utf-8")).hexdigest(),
        "by_source": by_source,
        "by_language": by_language,
        "raw_source_sha256": raw_hashes,
        "expectation": "host_invariants_only",
        "license_note": "含 CC BY-NC-SA 4.0 派生内容，仅评测使用；详见 NOTICE.md",
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    (args.output / "NOTICE.md").write_text(NOTICE, encoding="utf-8")
    print(f"{len(cases)} cases -> {args.output}  {by_source}  {by_language}")


if __name__ == "__main__":
    main()
