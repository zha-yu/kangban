"""X5 记忆管理三函数（Day 9 B2 · contracts X5 冻结签名）——`/api/memory` 的消费面。

**为什么这个模块只有 re-export**：X5 的实现（隔离闸门②/AC-39、审计、superseded
版本链、软删）全部落在 `memory_service`（唯一写库层，services 聚合交付）。
`app/api/memory.py` 的骨架是按「`memory_admin_service` 三函数」的契约名写的
（X5 冻结时定的消费面名字），交付时实现进了 `memory_service`——本模块把两个
名字接起来，`/api/memory` 的 503 X5_NOT_DELIVERED 骨架即可下线，**不复制任何
隔离逻辑**（契约 X5「禁止」行：一个能力只有一种形态）。

三函数签名与 `memory_service` 逐字一致：

- `list_memories(session, *, elder_id, limit, offset) -> list[MemoryView]`
- `correct_memory(session, *, elder_id, chunk_id, new_text) -> MemoryRef`
- `delete_memory(session, *, elder_id, chunk_id) -> MemoryRef`

跨长者 `chunk_id` → `MemoryAccessDenied` + 审计（`memory_cross_elder_access_denied`），
由 API 层映射 403；空文本 → `MemoryValidationError`（API 层映射 422）。
事务归调用方（只 flush，不 commit）。
"""

from __future__ import annotations

from app.services.memory_service import (
    MemoryAccessDenied,
    MemoryRef,
    MemoryValidationError,
    MemoryView,
    correct_memory,
    delete_memory,
    list_memories,
)

__all__ = [
    "MemoryAccessDenied",
    "MemoryRef",
    "MemoryValidationError",
    "MemoryView",
    "correct_memory",
    "delete_memory",
    "list_memories",
]
