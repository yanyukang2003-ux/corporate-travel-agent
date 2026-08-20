from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = PROJECT_ROOT / "evals/subsets/model-duffel-test-order-e2e-v1.json"
CATEGORIES = (
    "flight",
    "revalidation",
    "order-create",
    "order-read",
    "cancellation-quote",
    "cancellation-confirm",
    "order-final",
)
_SECRET_MARKERS = (
    re.compile(rb"duffel_test_[A-Za-z0-9_-]+"),
    re.compile(rb"Authorization", re.IGNORECASE),
    re.compile(rb"Bearer\s+", re.IGNORECASE),
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Recover and cross-check archived Duffel Test Order evidence without "
            "making model or provider calls."
        )
    )
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    output = Path(args.output).resolve()
    if not output.is_dir():
        parser.error("--output must be the existing failed Test Order run directory")
    if (output / "evaluation-result.json").exists():
        parser.error("recovery refuses to overwrite an existing formal evaluation result")
    dataset_path = Path(args.dataset).resolve()
    dataset_bytes = dataset_path.read_bytes()
    dataset = json.loads(dataset_bytes)
    if dataset.get("dataset_id") != "model-duffel-test-order-e2e-v1":
        parser.error("recovery requires the D14 dataset")

    raw_root = output / "raw-provider-responses"
    evidence = {category: _one_payload(raw_root, category) for category in CATEGORIES}
    result = recover_evidence(
        run_id=output.name,
        dataset=dataset,
        dataset_sha256=hashlib.sha256(dataset_bytes).hexdigest(),
        evidence=evidence,
        raw_root=raw_root,
    )
    _write_private(
        output / "recovery-result.json",
        json.dumps(result, ensure_ascii=False, indent=2),
    )
    _write_private(output / "evaluation-report.recovered.md", _render_report(result))
    _write_private(
        output / "INVALID.md",
        """# Original formal run invalid

The external Duffel Test Mode transaction completed and its Order was confirmed
cancelled. The original runner then failed while validating an unregistered
evaluation-mode enum, before writing its model/workflow trace and formal result.

`recovery-result.json` validates the seven archived Duffel responses and cleanup
state without making another model or provider call. It does not reconstruct the
missing model usage or workflow trace, so this directory must not be claimed as a
fully passing formal evaluation.
""",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["external_transaction_passed"]:
        raise SystemExit(1)


def recover_evidence(
    *,
    run_id: str,
    dataset: dict[str, Any],
    dataset_sha256: str,
    evidence: dict[str, tuple[dict[str, Any], Path]],
    raw_root: Path,
) -> dict[str, Any]:
    flight = _data(evidence["flight"][0])
    offer = _data(evidence["revalidation"][0])
    created = _data(evidence["order-create"][0])
    read_back = _data(evidence["order-read"][0])
    quote = _data(evidence["cancellation-quote"][0])
    confirmed = _data(evidence["cancellation-confirm"][0])
    final = _data(evidence["order-final"][0])
    order_id = created.get("id")
    offer_id = offer.get("id")
    cancellation_id = confirmed.get("id")
    final_cancellation = final.get("cancellation")
    offers = flight.get("offers")
    selected_search_offer = (
        next(
            (item for item in offers if isinstance(item, Mapping) and item.get("id") == offer_id),
            None,
        )
        if isinstance(offers, list)
        else None
    )
    payment_status = final.get("payment_status")
    raw_files = [path for _, path in evidence.values()]
    metadata_files = [path.with_name(f"{path.name}.metadata.json") for path in raw_files]
    raw_hashes = {
        category: hashlib.sha256(path.read_bytes()).hexdigest()
        for category, (_, path) in evidence.items()
    }
    permission_paths = [*raw_files, *metadata_files]
    checks = {
        "seven_response_categories_present": set(evidence) == set(CATEGORIES),
        "all_responses_test_mode": all(
            value.get("live_mode") is False
            for value in (flight, offer, created, read_back, quote, confirmed, final)
        ),
        "duffel_airways_offer_selected": isinstance(selected_search_offer, Mapping)
        and isinstance(selected_search_offer.get("owner"), Mapping)
        and selected_search_offer["owner"].get("name") == dataset["required_provider_owner"],
        "revalidated_offer_matches_order": created.get("offer_id") == offer_id,
        "order_readback_matches_create": read_back.get("id") == order_id,
        "cancellation_quote_matches_order": quote.get("order_id") == order_id
        and quote.get("confirmed_at") is None,
        "cancellation_confirmation_matches_quote": confirmed.get("id") == quote.get("id")
        and confirmed.get("order_id") == order_id
        and confirmed.get("confirmed_at") is not None,
        "final_order_matches_create": final.get("id") == order_id,
        "final_order_contains_confirmed_cancellation": isinstance(final_cancellation, Mapping)
        and final_cancellation.get("id") == cancellation_id
        and final_cancellation.get("confirmed_at") == confirmed.get("confirmed_at"),
        "final_order_no_longer_cancellable": isinstance(final.get("available_actions"), list)
        and "cancel" not in final["available_actions"],
        "sandbox_balance_payment_completed": isinstance(payment_status, Mapping)
        and payment_status.get("awaiting_payment") is False
        and payment_status.get("paid_at") is not None,
        "refund_returned_to_test_balance": confirmed.get("refund_to") == "balance",
        "raw_hashes_match_metadata": all(
            _metadata_matches(path, raw_hashes[category])
            for category, (_, path) in evidence.items()
        ),
        "raw_evidence_permissions_private": all(
            path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600
            for path in permission_paths
        ),
        "no_secret_markers": not _secret_marker_paths(raw_root),
    }
    external_transaction_passed = all(checks.values())
    return {
        "schema_version": 1,
        "run_id": run_id,
        "recovery_mode": "archived_external_evidence_only",
        "recovered_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "id": dataset["dataset_id"],
            "version": dataset["dataset_version"],
            "sha256": dataset_sha256,
        },
        "external_transaction": {
            "duffel_http_responses": len(evidence),
            "offer_id": offer_id,
            "order_id": order_id,
            "order_live_mode": created.get("live_mode"),
            "cancellation_id": cancellation_id,
            "cancellation_confirmed_at": confirmed.get("confirmed_at"),
            "refund_amount": confirmed.get("refund_amount"),
            "refund_currency": confirmed.get("refund_currency"),
            "refund_to": confirmed.get("refund_to"),
            "final_order_cancelled": checks["final_order_contains_confirmed_cancellation"],
        },
        "evidence": {
            "raw_response_sha256": raw_hashes,
            "raw_and_metadata_files": len(permission_paths),
            "secret_marker_paths": _secret_marker_paths(raw_root),
        },
        "checks": checks,
        "external_transaction_passed": external_transaction_passed,
        "formal_evaluation_passed": False,
        "formal_evaluation_failure": (
            "The evaluation-mode enum rejected model_live_provider_test_order after "
            "the external transaction, before model usage and workflow trace were persisted."
        ),
        "additional_external_calls": 0,
    }


def _one_payload(root: Path, category: str) -> tuple[dict[str, Any], Path]:
    paths = sorted(
        path
        for path in root.rglob(f"{category}/*.json")
        if not path.name.endswith(".metadata.json")
    )
    if len(paths) != 1:
        raise SystemExit(f"Expected one {category} response, found {len(paths)}")
    return json.loads(paths[0].read_text(encoding="utf-8")), paths[0]


def _data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise SystemExit("Archived Duffel response is missing data")
    return data


def _metadata_matches(path: Path, sha256: str) -> bool:
    metadata_path = path.with_name(f"{path.name}.metadata.json")
    if not metadata_path.is_file():
        return False
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    return metadata.get("sha256") == sha256 and metadata.get("size_bytes") == path.stat().st_size


def _secret_marker_paths(root: Path) -> list[str]:
    matches: list[str] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if any(pattern.search(path.read_bytes()) for pattern in _SECRET_MARKERS):
            matches.append(str(path.relative_to(root)))
    return matches


def _render_report(result: dict[str, Any]) -> str:
    external_status = "PASS" if result["external_transaction_passed"] else "FAIL"
    checks = "\n".join(
        f"- {'PASS' if passed else 'FAIL'} `{name}`" for name, passed in result["checks"].items()
    )
    transaction = result["external_transaction"]
    return f"""# Recovered Duffel Test Order evidence

- External transaction: **{external_status}**
- Formal evaluation: **INVALID**
- Duffel HTTP responses: `{transaction["duffel_http_responses"]}`
- Test Order live mode: `{transaction["order_live_mode"]}`
- Cancellation confirmed at: `{transaction["cancellation_confirmed_at"]}`
- Refund: `{transaction["refund_amount"]} {transaction["refund_currency"]}`
  to `{transaction["refund_to"]}`
- Final Order cancelled: `{str(transaction["final_order_cancelled"]).lower()}`
- Additional recovery API calls: `{result["additional_external_calls"]}`

## Archived evidence checks

{checks}

The real Test Mode Order was created, paid with sandbox balance, read back,
cancelled, refunded to the sandbox balance, and read back again. The original
runner failed only while constructing its formal trace after these steps. Because
the model usage and workflow trace were not persisted, this recovery report does
not upgrade the original run to a formal evaluation pass.
"""


def _write_private(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


if __name__ == "__main__":
    main()
