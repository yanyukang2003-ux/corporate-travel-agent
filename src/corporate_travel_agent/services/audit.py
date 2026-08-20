"""审计事件构造：为任务状态变更生成可哈希、可追溯的 AuditEvent。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from corporate_travel_agent.domain.models import AuditEvent


def stable_hash(value: Any) -> str:
    """对任意可序列化值计算稳定 SHA-256，用作审计输入/输出指纹。"""
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    payload = json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def new_audit_event(
    task_id: str,
    event_type: str,
    *,
    actor_type: str = "SYSTEM",
    input_value: Any = None,
    output_value: Any = None,
    evidence_refs: tuple[str, ...] = (),
) -> AuditEvent:
    """创建一条审计事件，仅存储输入/输出哈希与证据引用，不落明文敏感内容。"""
    return AuditEvent(
        event_id=str(uuid4()),
        task_id=task_id,
        event_type=event_type,
        actor_type=actor_type,
        input_hash=stable_hash(input_value),
        output_hash=stable_hash(output_value),
        evidence_refs=evidence_refs,
        created_at=datetime.now(UTC),
    )
