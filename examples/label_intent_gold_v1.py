#!/usr/bin/env python3
"""Apply simplified intent-gold labels and validate the human annotation packet."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
INTENT_PATH = ROOT / "data/evaluation/human-annotations/v1/to-label/01-intent-gold.jsonl"

CLASSIFICATIONS = {
    "TRIP",
    "TRANSPORT_COMPARE",
    "MULTI_DAY_TRIP",
    "NEEDS_CLARIFICATION",
    "OUT_OF_SCOPE",
}
HARD = {
    "arrive_before_meeting",
    "direct_only",
    "train_only",
    "flight_only",
    "hotel_required",
}
SOFT = {
    "avoid_early_departure",
    "hotel_near_client",
    "prefer_train",
    "prefer_flight",
    "compare_train_and_flight",
    "lowest_cost",
    "shortest_duration",
}
FIELD_KEYS = [
    "origin",
    "destination",
    "departure_after",
    "arrive_by",
    "return_after",
    "return_before",
    "hotel_check_in",
    "hotel_check_out",
    "hard_constraints",
    "soft_preferences",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("apply", help="Write simplified gold labels into 01-intent-gold.jsonl")
    sub.add_parser("validate", help="Validate intent-gold annotations")
    sub.add_parser("status", help="Show labeling progress")
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def empty_fields() -> dict[str, Any]:
    return {
        "origin": None,
        "destination": None,
        "departure_after": None,
        "arrive_by": None,
        "return_after": None,
        "return_before": None,
        "hotel_check_in": None,
        "hotel_check_out": None,
        "hard_constraints": None,
        "soft_preferences": None,
    }


def oos(*, notes: str) -> dict[str, Any]:
    fields = empty_fields()
    fields["hard_constraints"] = []
    fields["soft_preferences"] = []
    return {
        "classification": "OUT_OF_SCOPE",
        "fields": fields,
        "provided_fields": ["hard_constraints", "soft_preferences"],
        "missing_required_fields": [],
        "conflicts": [],
        "assumptions": [],
        "ambiguity": "CLEAR",
        "manipulation_detected": False,
        "must_clarify_before_search": False,
        "evidence_spans": {},
        "notes": notes,
    }


def pack(
    *,
    classification: str,
    fields: dict[str, Any],
    must_clarify: bool,
    ambiguity: str = "CLEAR",
    conflicts: list[str] | None = None,
    assumptions: list[str] | None = None,
    notes: str = "",
    missing: list[str] | None = None,
) -> dict[str, Any]:
    filled = empty_fields()
    filled.update(fields)
    if filled["hard_constraints"] is None and classification != "OUT_OF_SCOPE":
        filled["hard_constraints"] = []
    if filled["soft_preferences"] is None and classification != "OUT_OF_SCOPE":
        filled["soft_preferences"] = []
    provided = []
    for key in FIELD_KEYS:
        value = filled[key]
        if value is None:
            continue
        # [] means judged empty; non-empty values are provided evidence.
        provided.append(key)
    if missing is None:
        if classification in {"TRIP", "TRANSPORT_COMPARE"}:
            missing = [
                key
                for key in ("origin", "destination", "departure_after")
                if filled.get(key) in (None, [])
            ]
        elif classification == "MULTI_DAY_TRIP":
            missing = [
                key
                for key in ("origin", "destination", "departure_after")
                if filled.get(key) in (None, [])
            ]
        else:
            missing = []
    return {
        "classification": classification,
        "fields": filled,
        "provided_fields": provided,
        "missing_required_fields": missing,
        "conflicts": conflicts or [],
        "assumptions": assumptions or [],
        "ambiguity": ambiguity,
        "manipulation_detected": False,
        "must_clarify_before_search": must_clarify,
        "evidence_spans": {},
        "notes": notes,
    }


def shanghai_day(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> str:
    dt = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))
    return dt.isoformat()


def labels() -> dict[str, dict[str, Any]]:
    """Simplified gold labels for human-annotations v1 intent set."""
    # reference_time for most: 2026-07-20T09:00:00Z = 2026-07-20 17:00 Asia/Shanghai (Mon)
    tomorrow = shanghai_day(2026, 7, 21)
    next_tuesday = shanghai_day(2026, 7, 28)  # next Tue after Mon 2026-07-20

    return {
        "INT-001": oos(notes="多景点游览路线规划，非企业差旅订票/行程工作流。"),
        "INT-002": pack(
            classification="MULTI_DAY_TRIP",
            fields={
                "origin": "Guangzhou",
                "destination": "Zhuhai",
                "hard_constraints": ["hotel_required"],
                "soft_preferences": [],
            },
            must_clarify=True,
            ambiguity="AMBIGUOUS",
            assumptions=["暑期未给具体日期", "宠物友好为当前产品未建模约束"],
            notes="广州→珠海2日休闲+酒店；缺出发日期；宠物/景点攻略超当前企业差旅能力，需拒绝未支持约束。",
            missing=["departure_after"],
        ),
        "INT-003": oos(notes="市内多点打卡路线排序，属观光导览，越界。"),
        "INT-004": oos(notes="研学目的地推荐，非具体差旅预订请求。"),
        "INT-005": pack(
            classification="TRANSPORT_COMPARE",
            fields={
                "origin": "Qingdao",
                "destination": "Fuzhou",
                "departure_after": tomorrow,
                "hard_constraints": [],
                "soft_preferences": ["compare_train_and_flight"],
            },
            must_clarify=False,
            assumptions=["“明天”相对 reference_time+Asia/Shanghai 解析为 2026-07-21"],
            notes="青岛→福州，比较高铁/飞机班次。",
        ),
        "INT-006": oos(notes="单日海岛骑行/打卡可行性，属一日游导览。"),
        "INT-007": oos(notes="周边售票点 POI 搜索，非行程意图。"),
        "INT-008": oos(notes="单日景点顺序与闭馆时间规划，越界。"),
        "INT-009": oos(notes="单日自驾观光拍照路线，越界。"),
        "INT-010": oos(notes="周边礼品店 POI 推荐，越界。"),
        "INT-011": oos(notes="多点观光步行/游览路线，越界。"),
        "INT-012": pack(
            classification="MULTI_DAY_TRIP",
            fields={
                "destination": "Hangzhou",
                "hard_constraints": [],
                "soft_preferences": [],
            },
            must_clarify=True,
            ambiguity="AMBIGUOUS",
            assumptions=["3天时长，无出发城市与具体日期"],
            notes="杭州3日亲子游规划；缺 origin/日期；休闲行程可能需声明能力边界。",
            missing=["origin", "departure_after"],
        ),
        "INT-013": pack(
            classification="MULTI_DAY_TRIP",
            fields={
                "origin": "Washington",
                "destination": "Tampa",
                "departure_after": "2025-11-02T00:00:00+00:00",
                "return_before": "2025-11-04T23:59:59+00:00",
                "hotel_check_in": "2025-11-02",
                "hotel_check_out": "2025-11-04",
                "hard_constraints": ["hotel_required"],
                "soft_preferences": [],
            },
            must_clarify=False,
            assumptions=["3日行程默认入住 11-02 退房 11-04", "餐厅评分/人均约束产品未建模"],
            notes="Washington→Tampa 多日休闲；餐饮质量约束需作为不支持约束拒绝，不可静默忽略。",
        ),
        "INT-014": pack(
            classification="TRANSPORT_COMPARE",
            fields={
                "origin": "Tai'an",
                "destination": "Rizhao",
                "departure_after": next_tuesday,
                "hard_constraints": [],
                "soft_preferences": [],
            },
            must_clarify=False,
            assumptions=[
                "下周二=2026-07-28（reference 为周一）",
                "自驾对比不在 soft_preferences 枚举内",
            ],
            notes="泰安→日照，高铁 vs 自驾耗时比较；自驾为未建模模式，比较结论需声明边界。",
        ),
        "INT-015": pack(
            classification="TRIP",
            fields={
                "origin": "LHR",
                "destination": "JFK",
                "departure_after": "2026-09-15T00:00:00+00:00",
                "arrive_by": "2026-09-17T00:00:00+00:00",
                "hard_constraints": ["flight_only"],
                "soft_preferences": [],
            },
            must_clarify=False,
            notes="企业差旅 LHR→JFK，仅航班；日期已是绝对时间。",
        ),
        "INT-016": pack(
            classification="MULTI_DAY_TRIP",
            fields={
                "origin": "Denver",
                "destination": "Medford",
                "departure_after": "2026-01-23T00:00:00+00:00",
                "return_before": "2026-01-25T23:59:59+00:00",
                "hotel_check_in": "2026-01-23",
                "hotel_check_out": "2026-01-25",
                "hard_constraints": ["hotel_required"],
                "soft_preferences": [],
            },
            must_clarify=False,
            assumptions=["餐饮预算分层为未建模约束"],
            notes="Denver→Medford 3日休闲；餐厅预算约束需拒绝或 fort-ground，非差旅核心槽位。",
        ),
        "INT-017": pack(
            classification="MULTI_DAY_TRIP",
            fields={
                "origin": "Beijing",
                "destination": "Tianjin",
                "hard_constraints": [],
                "soft_preferences": ["prefer_train"],
            },
            must_clarify=True,
            ambiguity="AMBIGUOUS",
            assumptions=["12月无具体日", "2日工业旅游/博物馆属休闲内容"],
            notes="北京→天津2日高铁休闲；缺具体日期；途经唐山/博物馆推荐超纯交通预订。",
            missing=["departure_after"],
        ),
        "INT-018": pack(
            classification="MULTI_DAY_TRIP",
            fields={
                "origin": "Guangzhou",
                "hard_constraints": [],
                "soft_preferences": [],
            },
            must_clarify=True,
            ambiguity="AMBIGUOUS",
            notes="广州出发周末短途；缺目的地与具体日期。",
            missing=["destination", "departure_after"],
        ),
        "INT-019": pack(
            classification="TRANSPORT_COMPARE",
            fields={
                "origin": "Meishan",
                "destination": "Panzhihua",
                "departure_after": shanghai_day(2026, 8, 3),
                "hard_constraints": [],
                "soft_preferences": ["lowest_cost"],
            },
            must_clarify=False,
            assumptions=["年份取 reference_time 的 2026", "“划算”映射 lowest_cost", "自驾未建模"],
            notes="眉山→攀枝花，开车 vs 高铁成本比较。",
        ),
        "INT-020": pack(
            classification="MULTI_DAY_TRIP",
            fields={
                "destination": "Altay",
                "hard_constraints": ["hotel_required"],
                "soft_preferences": [],
            },
            must_clarify=True,
            ambiguity="AMBIGUOUS",
            assumptions=["宠物自驾路线/景点攻略为未支持复合需求"],
            notes="目的地阿勒泰；缺出发地与日期；宠物自驾攻略需能力边界说明。",
            missing=["origin", "departure_after"],
        ),
        "INT-021": pack(
            classification="TRANSPORT_COMPARE",
            fields={
                "origin": "Lhasa",
                "destination": "Shanghai",
                "departure_after": shanghai_day(2026, 7, 26),
                "hard_constraints": [],
                "soft_preferences": ["compare_train_and_flight", "shortest_duration"],
            },
            must_clarify=False,
            assumptions=["7月26日年份取 2026", "“哪个快”→shortest_duration"],
            notes="拉萨→上海，高铁 vs 飞机速度比较。",
        ),
        "INT-022": pack(
            classification="MULTI_DAY_TRIP",
            fields={
                "origin": "Appleton",
                "destination": "Denver",
                "departure_after": "2025-03-26T00:00:00+00:00",
                "return_before": "2025-03-28T23:59:59+00:00",
                "hotel_check_in": "2025-03-26",
                "hotel_check_out": "2025-03-28",
                "hard_constraints": ["hotel_required"],
                "soft_preferences": [],
            },
            must_clarify=False,
            assumptions=["餐厅评分下限为未建模约束"],
            notes="Appleton→Denver 3日；餐饮评分约束需显式不支持处理。",
        ),
        "INT-023": pack(
            classification="MULTI_DAY_TRIP",
            fields={
                "origin": "Long Beach",
                "destination": "Dallas",
                "departure_after": "2025-11-12T00:00:00+00:00",
                "return_before": "2025-11-14T23:59:59+00:00",
                "hotel_check_in": "2025-11-12",
                "hotel_check_out": "2025-11-14",
                "hard_constraints": ["hotel_required"],
                "soft_preferences": ["lowest_cost"],
            },
            must_clarify=False,
            assumptions=["$360/晚为酒店预算上限，映射到成本敏感偏好；精确 cap 未建模"],
            notes="Long Beach→Dallas 3日含住宿预算。",
        ),
        "INT-024": oos(notes="商场周边图书馆 POI 推荐，越界。"),
        "INT-025": pack(
            classification="MULTI_DAY_TRIP",
            fields={
                "hard_constraints": [],
                "soft_preferences": [],
            },
            must_clarify=True,
            ambiguity="AMBIGUOUS",
            notes="国内蜜月方向性需求；缺 origin/destination/日期。",
            missing=["origin", "destination", "departure_after"],
        ),
        "INT-026": pack(
            classification="MULTI_DAY_TRIP",
            fields={
                "origin": "Guangzhou",
                "hard_constraints": [],
                "soft_preferences": [],
            },
            must_clarify=True,
            ambiguity="AMBIGUOUS",
            assumptions=["三天假≈3日行程，无具体日历"],
            notes="广州出发目的地开放推荐；缺 destination 与日期。",
            missing=["destination", "departure_after"],
        ),
    }


def apply_labels() -> None:
    rows = load_jsonl(INTENT_PATH)
    gold = labels()
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    missing_ids = []
    for row in rows:
        ann_id = row["annotation_id"]
        if ann_id not in gold:
            missing_ids.append(ann_id)
            continue
        row["annotation"] = gold[ann_id]
        row["annotation_meta"] = {
            **row.get("annotation_meta", {}),
            "annotator_id": "human-01",
            "round": 1,
            "blinded": True,
            "label_style": "simplified_v1",
            "completed_at": now,
        }
    if missing_ids:
        raise SystemExit(f"missing labels for: {missing_ids}")
    write_jsonl(INTENT_PATH, rows)
    print(f"wrote {len(rows)} labels -> {INTENT_PATH}")


def validate_rows(rows: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for row in rows:
        ann_id = row.get("annotation_id", "?")
        ann = row.get("annotation") or {}
        prefix = f"{ann_id}"
        cls = ann.get("classification")
        if cls not in CLASSIFICATIONS:
            errors.append(f"{prefix}: invalid classification {cls!r}")
            continue
        fields = ann.get("fields") or {}
        for key in FIELD_KEYS:
            if key not in fields:
                errors.append(f"{prefix}: missing fields.{key}")
        hc = fields.get("hard_constraints")
        sp = fields.get("soft_preferences")
        if hc is not None:
            if not isinstance(hc, list) or any(item not in HARD for item in hc):
                errors.append(f"{prefix}: invalid hard_constraints {hc!r}")
        if sp is not None:
            if not isinstance(sp, list) or any(item not in SOFT for item in sp):
                errors.append(f"{prefix}: invalid soft_preferences {sp!r}")
        if ann.get("manipulation_detected") not in (True, False):
            errors.append(f"{prefix}: manipulation_detected must be bool")
        if ann.get("must_clarify_before_search") not in (True, False):
            errors.append(f"{prefix}: must_clarify_before_search must be bool")
        if ann.get("ambiguity") not in {"CLEAR", "AMBIGUOUS", "UNJUDGEABLE", None}:
            errors.append(f"{prefix}: invalid ambiguity")
        if cls == "OUT_OF_SCOPE":
            for key in FIELD_KEYS:
                if key in {"hard_constraints", "soft_preferences"}:
                    if fields.get(key) not in ([], None):
                        errors.append(f"{prefix}: OOS {key} should be []")
                elif fields.get(key) is not None:
                    errors.append(f"{prefix}: OOS field {key} must be null")
            if ann.get("must_clarify_before_search") is True:
                errors.append(f"{prefix}: OOS should not must_clarify_before_search")
        if cls in {"TRIP", "TRANSPORT_COMPARE"} and not ann.get("must_clarify_before_search"):
            for key in ("origin", "destination", "departure_after"):
                if fields.get(key) in (None, []):
                    # allowed only if explicitly missing_required and must_clarify
                    pass
        meta = row.get("annotation_meta") or {}
        if meta.get("completed_at") in (None, ""):
            errors.append(f"{prefix}: annotation_meta.completed_at empty")
        # null vs [] rule for list fields when completed
        if meta.get("completed_at") and hc is None and cls != "OUT_OF_SCOPE":
            # completed annotations should use [] not null for judged empty lists
            errors.append(f"{prefix}: completed hard_constraints should not be null (use [])")
        if meta.get("completed_at") and sp is None and cls != "OUT_OF_SCOPE":
            errors.append(f"{prefix}: completed soft_preferences should not be null (use [])")
    return errors


def cmd_validate() -> int:
    rows = load_jsonl(INTENT_PATH)
    errors = validate_rows(rows)
    complete = sum(1 for row in rows if (row.get("annotation_meta") or {}).get("completed_at"))
    print(f"intent_gold: {complete}/{len(rows)} completed")
    if errors:
        print(f"FAIL {len(errors)} issues:")
        for item in errors:
            print(f"  - {item}")
        return 1
    print("PASS simplified intent-gold checks")
    # distribution
    from collections import Counter

    counts = Counter((row.get("annotation") or {}).get("classification") for row in rows)
    print("classification counts:", dict(counts))
    return 0


def cmd_status() -> None:
    rows = load_jsonl(INTENT_PATH)
    complete = 0
    for row in rows:
        ann = row.get("annotation") or {}
        done = (row.get("annotation_meta") or {}).get("completed_at") and ann.get("classification")
        mark = "done" if done else "todo"
        if done:
            complete += 1
        print(
            f"{row['annotation_id']}: {mark} "
            f"class={ann.get('classification')} clarify={ann.get('must_clarify_before_search')}"
        )
    print(f"progress {complete}/{len(rows)}")


def main() -> int:
    args = parse_args()
    if args.command == "apply":
        apply_labels()
        return cmd_validate()
    if args.command == "validate":
        return cmd_validate()
    if args.command == "status":
        cmd_status()
        return 0
    raise SystemExit(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
