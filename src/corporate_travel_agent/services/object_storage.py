"""供应商原始响应的 WORM 对象存储：不可变写入、哈希校验与按角色的读授权。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from corporate_travel_agent.domain.enums import RawResponseAccessPolicy
from corporate_travel_agent.domain.models import RawResponseReference


class ObjectConflictError(RuntimeError):
    """同一 object_key 已存在且内容不一致（违反 WORM 不可变约束）。"""


class ObjectAccessDenied(PermissionError):
    """调用方角色/用途不允许读取受限原始响应。"""


class ObjectIntegrityError(RuntimeError):
    """存储元数据或内容哈希与引用不匹配。"""


class RawResponseReadPurpose(StrEnum):
    """读取原始响应的合法用途。"""

    SYSTEM_REPLAY = "SYSTEM_REPLAY"
    AUDIT = "AUDIT"


@dataclass(frozen=True, slots=True)
class RawResponseReadContext:
    """一次原始响应读取的主体、角色与用途上下文。"""

    actor_id: str
    roles: frozenset[str]
    purpose: RawResponseReadPurpose


class RawResponseObjectStore(Protocol):
    """原始响应对象存储端口：按 key 写入字节并按授权读取。"""

    backend_name: str

    def put_bytes(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
        stored_at: datetime,
        retention_until: datetime,
        access_policy: RawResponseAccessPolicy,
    ) -> RawResponseReference: ...

    def get_bytes(
        self,
        reference: RawResponseReference,
        *,
        context: RawResponseReadContext,
    ) -> bytes: ...


class InMemoryRawResponseObjectStore:
    """内存 WORM 对象存储，供单测与非持久化演示使用。"""

    backend_name = "memory"

    def __init__(self) -> None:
        self._objects: dict[str, tuple[RawResponseReference, bytes]] = {}

    def put_bytes(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
        stored_at: datetime,
        retention_until: datetime,
        access_policy: RawResponseAccessPolicy,
    ) -> RawResponseReference:
        reference = _new_reference(
            object_key=object_key,
            content=content,
            content_type=content_type,
            stored_at=stored_at,
            retention_until=retention_until,
            access_policy=access_policy,
        )
        existing = self._objects.get(object_key)
        if existing is not None:
            if existing != (reference, content):
                raise ObjectConflictError(f"Object {object_key} is immutable")
            return existing[0]
        self._objects[object_key] = (reference, bytes(content))
        return reference

    def get_bytes(
        self,
        reference: RawResponseReference,
        *,
        context: RawResponseReadContext,
    ) -> bytes:
        _authorize(reference, context)
        try:
            stored_reference, content = self._objects[reference.object_key]
        except KeyError as exc:
            raise FileNotFoundError(reference.object_key) from exc
        if stored_reference != reference:
            raise ObjectIntegrityError("Stored raw-response metadata does not match snapshot")
        _verify_hash(reference, content)
        return bytes(content)


class LocalRawResponseObjectStore:
    """本地目录 WORM 后端：私有文件权限 + 不可变 metadata sidecar。"""

    backend_name = "local-worm"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def put_bytes(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
        stored_at: datetime,
        retention_until: datetime,
        access_policy: RawResponseAccessPolicy,
    ) -> RawResponseReference:
        reference = _new_reference(
            object_key=object_key,
            content=content,
            content_type=content_type,
            stored_at=stored_at,
            retention_until=retention_until,
            access_policy=access_policy,
        )
        object_path = self._resolve(object_key)
        metadata_path = object_path.with_name(f"{object_path.name}.metadata.json")
        metadata = json.dumps(
            asdict(reference),
            default=str,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        _write_once(object_path, content)
        _write_once(metadata_path, metadata)
        return reference

    def get_bytes(
        self,
        reference: RawResponseReference,
        *,
        context: RawResponseReadContext,
    ) -> bytes:
        _authorize(reference, context)
        object_path = self._resolve(reference.object_key)
        metadata_path = object_path.with_name(f"{object_path.name}.metadata.json")
        content = object_path.read_bytes()
        expected_metadata = json.dumps(
            asdict(reference),
            default=str,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if metadata_path.read_bytes() != expected_metadata:
            raise ObjectIntegrityError("Stored raw-response metadata does not match snapshot")
        _verify_hash(reference, content)
        return content

    def _resolve(self, object_key: str) -> Path:
        relative = Path(object_key)
        if relative.is_absolute() or not object_key or ".." in relative.parts:
            raise ValueError("object_key must be a safe relative path")
        resolved = (self.root / relative).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("object_key escapes the configured store")
        return resolved


def _new_reference(
    *,
    object_key: str,
    content: bytes,
    content_type: str,
    stored_at: datetime,
    retention_until: datetime,
    access_policy: RawResponseAccessPolicy,
) -> RawResponseReference:
    if not object_key:
        raise ValueError("object_key is required")
    if not content_type:
        raise ValueError("content_type is required")
    if stored_at.tzinfo is None or stored_at.utcoffset() is None:
        raise ValueError("stored_at must be timezone-aware")
    if retention_until.tzinfo is None or retention_until.utcoffset() is None:
        raise ValueError("retention_until must be timezone-aware")
    if retention_until <= stored_at:
        raise ValueError("retention_until must be later than stored_at")
    return RawResponseReference(
        object_key=object_key,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        content_type=content_type,
        stored_at=stored_at,
        retention_until=retention_until,
        access_policy=access_policy,
    )


def _authorize(
    reference: RawResponseReference,
    context: RawResponseReadContext,
) -> None:
    if reference.access_policy is not RawResponseAccessPolicy.SYSTEM_REPLAY_OR_AUDIT_ADMIN:
        raise ObjectAccessDenied("Unsupported raw-response access policy")
    replay_allowed = (
        context.purpose is RawResponseReadPurpose.SYSTEM_REPLAY
        and "system_replay" in context.roles
    )
    audit_allowed = (
        context.purpose is RawResponseReadPurpose.AUDIT
        and "audit_admin" in context.roles
    )
    if not (replay_allowed or audit_allowed):
        raise ObjectAccessDenied(
            f"Actor {context.actor_id} cannot read restricted provider responses"
        )


def _verify_hash(reference: RawResponseReference, content: bytes) -> None:
    actual = hashlib.sha256(content).hexdigest()
    if actual != reference.sha256:
        raise ObjectIntegrityError(
            f"Raw-response hash mismatch for {reference.object_key}"
        )


def _write_once(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != content:
            raise ObjectConflictError(f"Object {path.name} is immutable") from None
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
