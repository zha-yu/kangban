```mermaid
flowchart TD
    subgraph 会话生命周期
        A[Pad/Console 新会话] --> B{identity 决策}
        B -->|generic| C[generic companion]
        B -->|elder_id 已确认| D[personal companion]
        D --> E[StateCenter 初始化 version=0]
    end

    subgraph 单轮对话（重复）
        E --> F[收到 user_text]
        F --> G[build_turn_context 九层装配]
        G --> G1[L0 safety_and_policy<br/>写：缓存]
        G --> G2[L1 identity_and_scope<br/>写：身份锁定]
        G --> G3[L2 runtime_info<br/>写：时间/设备]
        G --> G4[L3 active_task<br/>写：StateSnapshot.task]
        G --> G5[L4 authoritative_facts<br/>写：memory_chunks 查库]
        G --> G6[L5 relevant_memories<br/>写：memory_chunks 召回]
        G --> G7[L6 older_summary<br/>写：HistoryStore 摘要]
        G --> G8[waiting_for 绑定<br/>写：StateSnapshot.task.waiting_for]
        G --> H[模型生成 response]
        H --> I[响应流 TTS+下发]
        I --> J[StateCenter apply 事件 CAS]
    end

    subgraph 状态变更节点（每事件写哪里）
        J --> K1[turn_started<br/>写：StateSnapshot.version+1<br/>写：conversation_events]
        J --> K2[asr_final_committed<br/>写：StateSnapshot<br/>写：conversation_events]
        J --> K3[response_started<br/>写：StateSnapshot<br/>写：conversation_events<br/>写：tool_executions]
        J --> K4[response_segment_delivered<br/>写：StateSnapshot.last_acked_segment]
        J --> K5[response_interrupted / completed<br/>写：StateSnapshot<br/>写：conversation_turns]
        J --> K6[task_waiting / resumed<br/>写：StateSnapshot.task<br/>写：conversation_events]
        J --> K7[turn_completed<br/>写：StateSnapshot 重置<br/>写：conversation_turns]
    end

    subgraph 对话结束后（异步，不阻塞主链）
        K7 --> M[extract_preference_candidates 异步]
        M --> N1[insert_candidate 入库 memory_chunks<br/>写：memory_chunks 新行<br/>写：audit_logs]
        N1 --> N2[recall_active_chunks 闸门②过滤<br/>写：只读不写]
    end

    subgraph 会话终止
        K7 --> O[StateCenter clear<br/>写：删 Redis session key]
        O --> P[conversation_turns 最终落库<br/>写：conversation_turns]
        P --> Q[APScheduler 触发<br/>写：memory_chunks 衰减/归并]
    end
```

---

### 节点→存储映射表（讲义 §6 验收）

| 节点 | 历史流水 `conversation_events/turns` | StateCenter (Redis) | 业务表 | 备注 |
|------|------|------|------|------|
| 新会话 | — | ✅ version=0 初始化 | — | generic 跳过 |
| turn_started | ✅ event | ✅ input.status=active | — | |
| asr_final_committed | ✅ event | ✅ asr_partial 清空 | — | |
| response_started | ✅ event + tool_executions | ✅ output.status=playing | — | |
| response_segment_delivered | — | ✅ last_acked_segment | — | 高频事件，不写历史流水 |
| response_interrupted | ✅ event + turn | ✅ output.status=interrupted | — | 讲义 §7.4 打断标记 |
| response_completed | ✅ event + turn | ✅ output.status=completed | — | |
| tool_started/succeeded/failed | ✅ event | — | — | 工具事件不改 StateSnapshot（§5.2 显式跳过） |
| task_waiting | ✅ event | ✅ task.kind/waiting_for | — | 讲义 §7.2 任务绑定 |
| task_resumed | ✅ event | ✅ task 清空 | — | |
| turn_completed | ✅ event + turn | ✅ input 重置 | — | |
| extract → insert_candidate | — | — | ✅ memory_chunks + audit_logs | 异步不阻塞主链；闸门② elder_id |
| recall_active_chunks | — | — | 只读 memory_chunks | 闸门② + 有效期过滤 |
| StateCenter clear | — | ✅ 删 Redis key | — | 不删业务表 |

### CAS 冲突处理

```
caller → SessionCoordinator.submit(thread_id, events)
         ↓ asyncio.Lock per thread
         ↓ snapshot → expected_version=3
         ↓ apply with events → CAS Lua
         ↓ expected=3 ≠ actual=4 → StateVersionConflict
         ↓ 自动重试（最多 3 次）
         ↓ 超过重试 → 抛给上层（降级话术 + 日志）
```
