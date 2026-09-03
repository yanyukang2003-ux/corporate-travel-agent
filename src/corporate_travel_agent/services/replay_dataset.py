"""回放数据集：从任务导出冻结查询/库存，供离线回放与确定性供应商模拟。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from corporate_travel_agent.domain.enums import SourceType
from corporate_travel_agent.domain.models import (
    HotelOffer,
    InventorySnapshot,
    TransportOffer,
    TripTask,
)
from corporate_travel_agent.providers.base import (
    HotelSearchQuery,
    TransportSearchQuery,
    inventory_query_hash,
)
from corporate_travel_agent.services.object_storage import (
    RawResponseObjectStore,
    RawResponseReadContext,
    RawResponseReadPurpose,
)
from corporate_travel_agent.services.redaction import (
    REDACTION_PROFILE_VERSION,
    redact_json,
)

DATASET_SCHEMA_VERSION: Final = 1
SNAPSHOT_SCHEMA_VERSION: Final = 1
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_DATA_FILE_BYTES = 20 * 1024 * 1024
SHA256_PATTERN = r"^[a-f0-9]{64}$"

_SNAPSHOT_ADAPTER = TypeAdapter(InventorySnapshot)


class ReplayDatasetError(RuntimeError):
    """回放数据集错误。"""
    pass


class DatasetModel(BaseModel):
    """回放数据集模型基类。"""
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class TransportReplayQuery(DatasetModel):
    """交通搜索回放查询。"""
    origin: str = Field(min_length=1, max_length=100)
    destination: str = Field(min_length=1, max_length=100)
    depart_after: datetime
    arrive_before: datetime | None

    @model_validator(mode="after")
    def valid_window(self) -> TransportReplayQuery:
        _require_aware(self.depart_after, "depart_after")
        if self.arrive_before is not None:
            _require_aware(self.arrive_before, "arrive_before")
            if self.arrive_before <= self.depart_after:
                raise ValueError("arrive_before must be later than depart_after")
        if self.origin.casefold() == self.destination.casefold():
            raise ValueError("transport origin and destination must differ")
        return self

    def to_domain(self) -> TransportSearchQuery:
        return TransportSearchQuery(
            origin=self.origin,
            destination=self.destination,
            depart_after=self.depart_after,
            arrive_before=self.arrive_before,
        )


class HotelReplayQuery(DatasetModel):
    """酒店搜索回放查询。"""
    city: str = Field(min_length=1, max_length=100)
    check_in: date
    check_out: date

    @model_validator(mode="after")
    def valid_window(self) -> HotelReplayQuery:
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be later than check_in")
        return self

    def to_domain(self) -> HotelSearchQuery:
        return HotelSearchQuery(
            city=self.city,
            check_in=self.check_in,
            check_out=self.check_out,
        )


class ReplayDatasetEntry(DatasetModel):
    """回放数据集条目。"""
    entry_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    category: Literal["transport", "hotel"]
    query: TransportReplayQuery | HotelReplayQuery
    query_hash: str = Field(pattern=SHA256_PATTERN)
    snapshot_id: str = Field(min_length=1, max_length=128)
    snapshot_file: str = Field(min_length=1, max_length=512)
    snapshot_sha256: str = Field(pattern=SHA256_PATTERN)
    redacted_raw_file: str = Field(min_length=1, max_length=512)
    redacted_raw_sha256: str = Field(pattern=SHA256_PATTERN)
    source_raw_sha256: str = Field(pattern=SHA256_PATTERN)
    original_provider: str = Field(min_length=1, max_length=100)
    original_source_type: SourceType
    captured_at: datetime
    valid_until: datetime
    item_count: int = Field(ge=0, le=100_000)
    redaction_count: int = Field(ge=0, le=1_000_000)
    redaction_reasons: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def entry_is_consistent(self) -> ReplayDatasetEntry:
        if self.category == "transport" and not isinstance(
            self.query, TransportReplayQuery
        ):
            raise ValueError("transport entry requires a transport query")
        if self.category == "hotel" and not isinstance(self.query, HotelReplayQuery):
            raise ValueError("hotel entry requires a hotel query")
        _require_aware(self.captured_at, "captured_at")
        _require_aware(self.valid_until, "valid_until")
        if self.valid_until <= self.captured_at:
            raise ValueError("valid_until must be later than captured_at")
        if any(value < 1 for value in self.redaction_reasons.values()):
            raise ValueError("redaction reason counts must be positive")
        if sum(self.redaction_reasons.values()) != self.redaction_count:
            raise ValueError("redaction reason counts do not match redaction_count")
        return self

    def domain_query(self) -> TransportSearchQuery | HotelSearchQuery:
        return self.query.to_domain()


class ReplayDatasetManifest(DatasetModel):
    """回放数据集清单。"""
    schema_version: Literal[1]
    dataset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    dataset_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    classification: Literal["INTERNAL_REDACTED"]
    redaction_profile_version: Literal["travel-redaction-v1"]
    created_at: datetime
    entries: tuple[ReplayDatasetEntry, ...] = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def manifest_is_consistent(self) -> ReplayDatasetManifest:
        _require_aware(self.created_at, "created_at")
        _require_unique(self.entries, "entry_id")
        _require_unique(self.entries, "query_hash")
        _require_unique(self.entries, "snapshot_file")
        _require_unique(self.entries, "redacted_raw_file")
        return self


@dataclass(frozen=True, slots=True)
class ReplayExportCase:
    """导出用例描述。"""
    category: Literal["transport", "hotel"]
    query: TransportSearchQuery | HotelSearchQuery
    snapshot: InventorySnapshot
    raw_response: bytes


@dataclass(frozen=True, slots=True)
class LoadedReplayDataset:
    """已加载的回放数据集。"""
    root: Path
    manifest: ReplayDatasetManifest
    snapshots_by_entry: dict[str, InventorySnapshot]
    manifest_sha256: str

    @property
    def snapshots(self) -> tuple[InventorySnapshot, ...]:
        return tuple(
            self.snapshots_by_entry[entry.entry_id]
            for entry in self.manifest.entries
        )


@dataclass(frozen=True, slots=True)
class LoadedReplayLibrary:
    """已加载的回放库（多数据集）。"""
    root: Path
    datasets: tuple[LoadedReplayDataset, ...]

    @property
    def entry_count(self) -> int:
        return sum(len(dataset.manifest.entries) for dataset in self.datasets)

    @property
    def real_snapshot_count(self) -> int:
        real_sources = {SourceType.AUTHORIZED_API, SourceType.BROWSER_ASSISTED}
        return sum(
            1
            for dataset in self.datasets
            for entry in dataset.manifest.entries
            if entry.original_source_type in real_sources
        )


def build_task_replay_cases(
    task: TripTask,
    snapshots: tuple[InventorySnapshot, ...],
    raw_response_store: RawResponseObjectStore,
) -> tuple[ReplayExportCase, ...]:
    """从任务构建回放用例。"""
    if task.request is None:
        raise ReplayDatasetError("Task does not have a complete trip request")
    request = task.request
    expected: list[
        tuple[Literal["transport", "hotel"], TransportSearchQuery | HotelSearchQuery]
    ] = [
        (
            "transport",
            TransportSearchQuery(
                origin=leg.origin,
                destination=leg.destination,
                depart_after=leg.depart_after,
                arrive_before=leg.arrive_before,
            ),
        )
        for leg in request.transport_legs()
    ]
    if request.hotel_check_in is not None and request.hotel_check_out is not None:
        expected.append(
            (
                "hotel",
                HotelSearchQuery(
                    city=request.destination,
                    check_in=request.hotel_check_in,
                    check_out=request.hotel_check_out,
                ),
            )
        )

    active_snapshot_ids = {
        snapshot_id
        for option in task.options
        for snapshot_id in option.inventory_snapshot_ids
    }
    candidates = [
        snapshot
        for snapshot in snapshots
        if not active_snapshot_ids or snapshot.snapshot_id in active_snapshot_ids
    ]
    cases: list[ReplayExportCase] = []
    for category, query in expected:
        query_hash = inventory_query_hash(query)
        matches = [item for item in candidates if item.query_hash == query_hash]
        if len(matches) != 1:
            raise ReplayDatasetError(
                f"Expected one active snapshot for query {query_hash[:12]}, found {len(matches)}"
            )
        snapshot = matches[0]
        if snapshot.raw_response is None:
            raise ReplayDatasetError(f"Snapshot {snapshot.snapshot_id} has no raw response")
        raw_response = raw_response_store.get_bytes(
            snapshot.raw_response,
            context=RawResponseReadContext(
                actor_id="replay-dataset-exporter",
                roles=frozenset({"system_replay"}),
                purpose=RawResponseReadPurpose.SYSTEM_REPLAY,
            ),
        )
        if hashlib.sha256(raw_response).hexdigest() != snapshot.raw_payload_hash:
            raise ReplayDatasetError(
                f"Snapshot {snapshot.snapshot_id} raw-response hash is inconsistent"
            )
        cases.append(
            ReplayExportCase(
                category=category,
                query=query,
                snapshot=snapshot,
                raw_response=raw_response,
            )
        )
    return tuple(cases)


def export_replay_dataset(
    cases: tuple[ReplayExportCase, ...],
    output_directory: str | os.PathLike[str],
    *,
    dataset_id: str,
    dataset_version: str,
    created_at: datetime | None = None,
) -> ReplayDatasetManifest:
    """导出回放数据集到目录。"""
    if not cases:
        raise ReplayDatasetError("At least one replay case is required")
    output = Path(output_directory).expanduser().resolve()
    if output.exists():
        raise ReplayDatasetError(f"Refusing to overwrite dataset directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix=f".{dataset_id}.", dir=output.parent))
    try:
        entries = tuple(_export_case(case, temporary) for case in cases)
        manifest = ReplayDatasetManifest(
            schema_version=DATASET_SCHEMA_VERSION,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            classification="INTERNAL_REDACTED",
            redaction_profile_version=REDACTION_PROFILE_VERSION,
            created_at=created_at or datetime.now(UTC),
            entries=entries,
        )
        _write_new_private_file(
            temporary / "manifest.json",
            _canonical_json_bytes(manifest.model_dump(mode="json")),
        )
        os.rename(temporary, output)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def load_replay_dataset(
    directory: str | os.PathLike[str],
) -> LoadedReplayDataset:
    """加载单个回放数据集。"""
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise ReplayDatasetError(f"Replay dataset directory does not exist: {root}")
    manifest_path = _safe_dataset_file(root, "manifest.json")
    manifest_bytes = _read_limited(manifest_path, MAX_MANIFEST_BYTES)
    try:
        manifest = ReplayDatasetManifest.model_validate_json(manifest_bytes)
    except ValidationError as exc:
        raise ReplayDatasetError(f"Invalid replay manifest: {exc}") from exc

    snapshots: dict[str, InventorySnapshot] = {}
    for entry in manifest.entries:
        snapshot = _load_entry(root, entry)
        snapshots[entry.entry_id] = snapshot
    return LoadedReplayDataset(
        root=root,
        manifest=manifest,
        snapshots_by_entry=snapshots,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def load_replay_library(
    directory: str | os.PathLike[str],
) -> LoadedReplayLibrary:
    """加载回放库。"""
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise ReplayDatasetError(f"Replay library directory does not exist: {root}")
    dataset_directories = sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and not path.is_symlink() and (path / "manifest.json").is_file()
    )
    if not dataset_directories:
        raise ReplayDatasetError(f"Replay library contains no datasets: {root}")
    datasets = tuple(load_replay_dataset(path) for path in dataset_directories)
    dataset_ids = [dataset.manifest.dataset_id for dataset in datasets]
    if len(dataset_ids) != len(set(dataset_ids)):
        raise ReplayDatasetError("Replay library contains duplicate dataset IDs")
    source_hashes = [
        entry.source_raw_sha256
        for dataset in datasets
        for entry in dataset.manifest.entries
    ]
    if len(source_hashes) != len(set(source_hashes)):
        raise ReplayDatasetError("Replay library contains duplicate source snapshots")
    return LoadedReplayLibrary(root=root, datasets=datasets)


def _export_case(case: ReplayExportCase, root: Path) -> ReplayDatasetEntry:
    expected_query_hash = inventory_query_hash(case.query)
    if expected_query_hash != case.snapshot.query_hash:
        raise ReplayDatasetError(
            f"Snapshot {case.snapshot.snapshot_id} does not match its replay query"
        )
    expected_query_type = (
        TransportSearchQuery if case.category == "transport" else HotelSearchQuery
    )
    if not isinstance(case.query, expected_query_type):
        raise ReplayDatasetError(f"{case.category} case has the wrong query type")
    source_raw_hash = hashlib.sha256(case.raw_response).hexdigest()
    if source_raw_hash != case.snapshot.raw_payload_hash:
        raise ReplayDatasetError(
            f"Snapshot {case.snapshot.snapshot_id} raw payload hash does not match"
        )
    try:
        raw_payload = json.loads(case.raw_response)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayDatasetError("Provider raw response must be valid UTF-8 JSON") from exc
    redacted = redact_json(raw_payload)
    redacted_raw_bytes = _canonical_json_bytes(redacted.payload)

    scrubbed_snapshot = replace(case.snapshot, raw_response=None)
    snapshot_document = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot": _SNAPSHOT_ADAPTER.dump_python(scrubbed_snapshot, mode="json"),
    }
    snapshot_redaction_check = redact_json(snapshot_document)
    if snapshot_redaction_check.redaction_count:
        reasons = ", ".join(sorted(snapshot_redaction_check.reasons))
        raise ReplayDatasetError(
            f"Normalized snapshot {case.snapshot.snapshot_id} still contains "
            f"sensitive data ({reasons})"
        )
    snapshot_bytes = _canonical_json_bytes(snapshot_document)
    entry_id = f"{case.category}-{expected_query_hash[:16]}"
    snapshot_file = f"snapshots/{entry_id}.json"
    raw_file = f"raw/{entry_id}.redacted.json"
    _write_new_private_file(root / snapshot_file, snapshot_bytes)
    _write_new_private_file(root / raw_file, redacted_raw_bytes)
    query_model: TransportReplayQuery | HotelReplayQuery
    if isinstance(case.query, TransportSearchQuery):
        query_model = TransportReplayQuery(
            origin=case.query.origin,
            destination=case.query.destination,
            depart_after=case.query.depart_after,
            arrive_before=case.query.arrive_before,
        )
    else:
        query_model = HotelReplayQuery(
            city=case.query.city,
            check_in=case.query.check_in,
            check_out=case.query.check_out,
        )
    return ReplayDatasetEntry(
        entry_id=entry_id,
        category=case.category,
        query=query_model,
        query_hash=expected_query_hash,
        snapshot_id=case.snapshot.snapshot_id,
        snapshot_file=snapshot_file,
        snapshot_sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
        redacted_raw_file=raw_file,
        redacted_raw_sha256=hashlib.sha256(redacted_raw_bytes).hexdigest(),
        source_raw_sha256=source_raw_hash,
        original_provider=case.snapshot.provider,
        original_source_type=case.snapshot.source_type,
        captured_at=case.snapshot.captured_at,
        valid_until=case.snapshot.valid_until,
        item_count=len(case.snapshot.items),
        redaction_count=redacted.redaction_count,
        redaction_reasons=redacted.reasons,
    )


def _load_entry(root: Path, entry: ReplayDatasetEntry) -> InventorySnapshot:
    if inventory_query_hash(entry.domain_query()) != entry.query_hash:
        raise ReplayDatasetError(f"Entry {entry.entry_id} has an invalid query hash")
    snapshot_path = _safe_dataset_file(root, entry.snapshot_file)
    snapshot_bytes = _read_limited(snapshot_path, MAX_DATA_FILE_BYTES)
    if hashlib.sha256(snapshot_bytes).hexdigest() != entry.snapshot_sha256:
        raise ReplayDatasetError(f"Entry {entry.entry_id} snapshot hash mismatch")
    try:
        document = json.loads(snapshot_bytes)
        if set(document) != {"schema_version", "snapshot"}:
            raise ReplayDatasetError(
                f"Entry {entry.entry_id} snapshot envelope has unexpected fields"
            )
        if document["schema_version"] != SNAPSHOT_SCHEMA_VERSION:
            raise ReplayDatasetError(
                f"Entry {entry.entry_id} snapshot schema is unsupported"
            )
        snapshot = _SNAPSHOT_ADAPTER.validate_python(document["snapshot"])
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise ReplayDatasetError(f"Entry {entry.entry_id} snapshot is invalid") from exc
    if redact_json(document).redaction_count:
        raise ReplayDatasetError(
            f"Entry {entry.entry_id} normalized snapshot contains sensitive data"
        )

    raw_path = _safe_dataset_file(root, entry.redacted_raw_file)
    redacted_raw_bytes = _read_limited(raw_path, MAX_DATA_FILE_BYTES)
    if hashlib.sha256(redacted_raw_bytes).hexdigest() != entry.redacted_raw_sha256:
        raise ReplayDatasetError(f"Entry {entry.entry_id} redacted raw hash mismatch")
    try:
        redacted_raw = json.loads(redacted_raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayDatasetError(
            f"Entry {entry.entry_id} redacted raw response is invalid"
        ) from exc
    if redact_json(redacted_raw).redaction_count:
        raise ReplayDatasetError(
            f"Entry {entry.entry_id} redacted raw response still contains sensitive data"
        )

    _validate_snapshot_against_entry(snapshot, entry)
    return snapshot


def _validate_snapshot_against_entry(
    snapshot: InventorySnapshot,
    entry: ReplayDatasetEntry,
) -> None:
    expected = {
        "snapshot_id": entry.snapshot_id,
        "query_hash": entry.query_hash,
        "provider": entry.original_provider,
        "source_type": entry.original_source_type,
        "captured_at": entry.captured_at,
        "valid_until": entry.valid_until,
        "raw_payload_hash": entry.source_raw_sha256,
    }
    for attribute, value in expected.items():
        if getattr(snapshot, attribute) != value:
            raise ReplayDatasetError(
                f"Entry {entry.entry_id} does not match snapshot {attribute}"
            )
    if snapshot.raw_response is not None:
        raise ReplayDatasetError(f"Entry {entry.entry_id} leaked a raw object reference")
    if len(snapshot.items) != entry.item_count:
        raise ReplayDatasetError(f"Entry {entry.entry_id} item count mismatch")
    if any(item.snapshot_id != snapshot.snapshot_id for item in snapshot.items):
        raise ReplayDatasetError(f"Entry {entry.entry_id} contains cross-snapshot items")
    if entry.category == "transport" and any(
        not isinstance(item, TransportOffer) for item in snapshot.items
    ):
        raise ReplayDatasetError(f"Entry {entry.entry_id} contains non-transport items")
    if entry.category == "hotel" and any(
        not isinstance(item, HotelOffer) for item in snapshot.items
    ):
        raise ReplayDatasetError(f"Entry {entry.entry_id} contains non-hotel items")


def _safe_dataset_file(root: Path, relative_name: str) -> Path:
    relative = Path(relative_name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ReplayDatasetError(f"Unsafe dataset path: {relative_name}")
    path = root.joinpath(relative)
    if path.is_symlink():
        raise ReplayDatasetError(f"Dataset file cannot be a symlink: {relative_name}")
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ReplayDatasetError(f"Dataset path escapes root: {relative_name}")
    if not resolved.is_file():
        raise ReplayDatasetError(f"Dataset file is missing: {relative_name}")
    return resolved


def _read_limited(path: Path, limit: int) -> bytes:
    size = path.stat().st_size
    if size > limit:
        raise ReplayDatasetError(f"Dataset file exceeds {limit} bytes: {path.name}")
    return path.read_bytes()


def _write_new_private_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ReplayDatasetError(f"Refusing to overwrite dataset file: {path}") from None
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_unique(entries: tuple[ReplayDatasetEntry, ...], attribute: str) -> None:
    values = [getattr(entry, attribute) for entry in entries]
    if len(values) != len(set(values)):
        raise ValueError(f"Replay entries must have unique {attribute}")
