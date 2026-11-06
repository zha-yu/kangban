> 状态：草稿　｜　版本：v1.0　｜　日期：2026-11-06
> 关联：docs/team_tasks/day04/task-01-声纹身份闭环.md（任务卡）｜docs/team_tasks/day04/README.md §3（四人分工）｜docs/team_tasks/day04/contracts.md C4/C5（契约）｜docs/plans/day-04-3D与身份闭环.md v2.4（后端方案）
> 上游依据：PRD v2.6 FR-VPR-01/02/03（验收：长者 ID + 声纹双条件、低置信主动确认、通用模式隔离）｜技术设计文档 v1.5 §3.4 voiceprints 表 + §3.6 Day 4 批次（实现：裁决 B2/C1/checkpoint）｜AGENTS §1（技术栈）/ §2（分层）/ §4（禁区）/ §5（高风险）/ §7（硬性数值）
> 变更记录见文末

---

## 0. 一句话定位

Day 4 A1 单元：**独立容器部署的声纹身份闭环**——CAM++ 推理容器（端口 7003，只做 /extract，主服务不装 torch）+ voiceprints / conversation_threads 两张新表迁移 + voiceprint_service / identity_service 双服务 + REST 三接口（samples / status / delete）+ 与 A2 信令 identity_ask/confirm 的契约对接，候选集恒为设备绑定长者 1v1，每会话首轮强制校验、cosine ≥0.85 命中。

---

## 1. 任务拆解（T1–T10，按依赖拓扑排序）

### T0 · 晨会契约冻结签字（09:00–09:30）

- **动作**：确认 C4（voiceprint REST 三接口）、C5（identity 级联 WS 消息）契约与 contracts.md v1.0 完全一致；将 C4/C5 原样拷入计划对应子任务顶部 dataclass/TypedDict 位置
- **产出**：契约冻结确认（在 contracts.md 定稿确认记录表签到）
- **优先级**：阻断所有后续，必须完成

### T1 · Spike — CAM++ 环境验证与 cosine 分布实测（09:30–11:00）

> 这是 W2 的关键路径前置（day-04 v2.4 T3）。**12:30 午站会必须通报结论**，否则 0005 迁移写不了。

- **写作用域**：`spikes/voiceprint/`
- **动作清单**：
  1. 新建 `spikes/voiceprint/` 目录
  2. 参考 `spikes/reference/asr/` 下已有的 `3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx` 模型文件
  3. 写 `spike_extract.py`：用 sherpa-onnx 加载 CAM++ 模型、从参考项目的 `examples/` 目录取 speaker1_a / speaker1_b / speaker2_a / speaker2_b 四段 WAV、调 embedding 提取
  4. 跑通后输出维度确认 + 同/异人 cosine 分布矩阵（同 = speaker1_a vs speaker1_b、speaker2_a vs speaker2_b；异 = speaker1_a vs speaker2_a 等 4 对）
  5. 实测阈值 0.85 的可分性（同对应 ≥0.85，异对应 <0.85）
  6. 写 README.md 记录：模型路径、运行命令、实测数据、cosine 矩阵
- **阻塞预案**：CAM++ 环境不通 → 立即启 resemblyzer 兜底（256 维），迁移 0005 维度参数随之改；仍不可行 → identity 走纯主动确认兜底，声纹比对降级到 Day 5，不阻塞 A2（C5 Mock 比对已解耦）
- **产出**：spikes/voiceprint/README.md（含实测 cosine 分布）
- **优先级**：P0（关键路径）

### T2 · 迁移 0004 — voiceprints + conversation_threads 表（10:00–11:30）

> **与 T1 并行**：0004 不含 embedding 列（裁决 B2：维度留 0005），可以先写、先合入。

- **写作用域**：`alembic/versions/0004_day4_voice_threads.py`
- **前置依赖**：`alembic/versions/0003_fix_day1_ck_names.py`（down_revision = "0003_fix_day1"）
- **动作清单**：
  1. 创建 0004 迁移文件（模板复制 0002 的风格：显式约束命名、CHECK 短名、UUID gen_random_uuid、comment）
  2. 建 voiceprints 表（除 embedding 外的全部字段）：
     - id uuid PK gen_random_uuid
     - elder_id uuid FK→elders
     - version int DEFAULT 1
     - sample_index int DEFAULT 1
     - status text CHECK(status IN ('active','retired')) DEFAULT 'active'
     - created_at timestamptz DEFAULT now()
     - **唯一约束** UNIQUE(elder_id, version, sample_index) 裁决 C1
  3. 建 conversation_threads 表（闸门①）：
     - thread_id text PK（服务端生成，不 auto gen）
     - elder_id uuid FK→elders NULL（generic 模式可空）
     - device_id uuid FK→devices NOT NULL
     - started_at timestamptz NOT NULL DEFAULT now()
     - ended_at timestamptz NULL
     - identity_source text CHECK(identity_source IN ('device','voice','confirm','generic')) NOT NULL
     - identity_confidence numeric(4,3) NULL
     - **跨列 CHECK**：`(identity_source='generic' AND elder_id IS NULL) OR (identity_source IN ('device','voice','confirm') AND elder_id IS NOT NULL)`
  4. 写 downgrade（DROP TABLE conversation_threads, voiceprints）
  5. 本地验证：`uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` 往返一次
- **验收**：迁移可回滚、唯一约束生效、CHECK 约束生效、表 comment 对齐技术文档
- **产出**：迁移文件
- **优先级**：P0

### T3 · ORM 模型 — voiceprint + conversation（11:00–12:00）

- **写作用域**：`app/models/voiceprint.py`、`app/models/conversation.py`、`app/models/__init__.py`
- **前置依赖**：T2 迁移文件写完（但不依赖执行）
- **动作清单**：
  1. 新建 `app/models/voiceprint.py`：Voiceprint 类，列注释逐字对齐 T2 迁移
  2. 新建 `app/models/conversation.py`：ConversationThread 类
  3. 更新 `app/models/__init__.py`：import 新模型 + __all__ 追加
- **验收**：`alembic check` 无漂移；`uv run pytest tests/test_*.py` 既有用例全绿
- **产出**：两个 ORM 模型文件 + __init__ 更新
- **优先级**：P0

### T4 · 推理容器 — deploy/voiceprint/（11:30–13:00）

> 依赖 T1 spike 结论，**但骨架可先搭**（Dockerfile + FastAPI app + requirements，模型文件路径占位）。

- **写作用域**：`deploy/voiceprint/` + `docker-compose.yml`（唯一合法改动）
- **容器职责**：只做 `POST /extract`，输入 multipart audio WAV bytes → 输出 192 维 float list（或 spike 结论的维度）。**比对在主服务进程内 numpy 完成**，容器不装业务依赖。
- **动作清单**：
  1. `deploy/voiceprint/Dockerfile`：python:3.12-slim + sherpa-onnx + numpy（**不装 torch**）+ FastAPI + uvicorn；模型文件 COPY 进 `/app/models/`
  2. `deploy/voiceprint/app.py`：FastAPI 单端点 `/extract`，加载 CAM++ 模型（sherpa-onnx.SpeakerEmbeddingExtractor）、接受 WAV bytes、提取 embedding、normalize 后返回 JSON 数组；加 5s 超时保护、推理失败返回 503
  3. `deploy/voiceprint/requirements.txt`：仅 sherpa-onnx + numpy + fastapi + uvicorn + python-multipart（容器内最小依赖集）
  4. `deploy/voiceprint/models/.gitkeep` + README：说明模型放置步骤（从 spikes/reference/asr/ 复制）
  5. **docker-compose.yml 追加 voiceprint 服务**（唯一合法改动）：
     - service name: voiceprint
     - image: 本地 build 或预置镜像
     - port: 7003（内部，compose 网络内）
     - 挂载: `deploy/voiceprint/models/` 到容器 `/app/models/`（模型文件不在 repo，需用户手动放置）
     - CPU 限制（与 funasr 同款模式）
     - healthcheck: curl /health 端点
  6. 本地起容器验证：`docker compose up -d voiceprint`，健康检查通过，POST /extract 返回 192 维
- **主服务 Settings 字段**：A3 统一录入（VOICEPRINT_BASE_URL、VOICEPRINT_EXTRACT_TIMEOUT_SECONDS、VOICEPRINT_EMBEDDING_DIMENSION、VOICEPRINT_FERNET_KEY 等），本卡**不直接改 config.py**
- **产出**：容器三文件 + compose 追加段 + 健康检查
- **验收**：`docker compose up -d voiceprint` → health=healthy → `curl -X POST localhost:7003/extract -F "audio=@test.wav"` 返回 192 维
- **优先级**：P0

### T5 · 迁移 0005 — voiceprints.embedding 列（12:30–13:30）

> **严格等 T1 spike 结论后写**。12:30 午站会通报维度参数（CAM++ 192 / resemblyzer 256）。

- **写作用域**：`alembic/versions/0005_voiceprint_embedding.py`
- **前置依赖**：T2 已合入（down_revision = "0004_day4_voice"）
- **动作清单**：
  1. `ALTER TABLE voiceprints ADD COLUMN embedding vector(N)` —— N 由 spike 结论定
  2. 写 downgrade（DROP COLUMN embedding）
- **验收**：与 T2 同迁往返可逆；ORM Voiceprint.embedding 列类型与 vector(N) 一致
- **产出**：迁移文件
- **优先级**：P0（embedding 加密落库的前提）

### T6 · voiceprint_service — 注册/状态/删除服务（13:00–14:00）

- **写作用域**：`app/services/voiceprint_service.py`
- **前置依赖**：T4 推理容器可起、T5 迁移写好（embedding 列存在）、A3 录入 Settings 新字段
- **动作清单**：
  1. **推理容器 HTTP 客户端**：httpx.AsyncClient 调 `VOICEPRINT_BASE_URL/extract`，设 `VOICEPRINT_EXTRACT_TIMEOUT_SECONDS`（默认 5s）；超时 → 抛 `VoiceprintExtractUnavailable`（映射 REST 503）
  2. **三段注册编排**：
     - 入参：`elder_id, sample_index(1-3), audio_bytes(WAV 16k ≤10s)`
     - 调推理容器拿 embedding（192 维 float list）
     - Fernet 加密：`Fernet(VOICEPRINT_FERNET_KEY).encrypt(embedding.tobytes())`
     - 写 voiceprints 表：INSERT，若 `(elder_id, version, sample_index)` 唯一约束冲突 → 抛 `VoiceprintDuplicateSample`（映射 REST 409）
     - **原始音频 bytes 即弃**（不写入任何临时文件/日志，AC-36）
     - 3 段齐（sample_index 1/2/3 都有）→ UPDATE status='active'
  3. **状态查询**：按 elder_id 查 voiceprints，返回 `{registered: [已注册的 sample_index], version, status: none|partial|active}`
  4. **删除**：DELETE 该 elder 全部 voiceprints；写 audit_logs（DELETE + actor + trace_id）
  5. **进程内 cosine 比对工具**：解密 embedding → numpy array → normalize → dot product；阈值 `Settings.voiceprint_similarity_threshold`（已在 config.py 0.85，AGENTS §7 硬性数值）
  6. **单元测试**：覆盖注册三段、重复 409、推理容器不可用 → 503 映射、加密落库、原始音频即弃（断言无临时文件）、cosine ≥0.85 命中、<0.85 不命中、audit_logs 写入
- **验收**：新增测试全绿；库内 embedding 列为密文；推理容器停掉 → 503；同人样本比对 cosine ≥0.85
- **产出**：voiceprint_service.py + tests/test_voiceprint_service.py
- **优先级**：P0

### T7 · REST 三接口（14:00–14:30）

- **写作用域**：`app/api/voiceprints.py`
- **前置依赖**：T6 voiceprint_service 完成
- **契约**：C4（原样拷入文件顶部 dataclass/TypedDict）
- **动作清单**：
  1. `POST /api/voiceprints/samples`：multipart audio + int sample_index（1-3）；鉴权用设备 JWT 依赖；elder_id 从 device 解出，**不接受前端传入**；调 voiceprint_service.register_sample()；错误映射：重复 409、推理失败 503、WAV 非法 422
  2. `GET /api/voiceprints/status`：设备未绑定长者 → 409 device_unbound
  3. `DELETE /api/voiceprints`：204 无 body，调 service.delete() 写审计
  4. main.py 注册路由（一行：`app.include_router(voiceprints.router)`）
- **验收**：curl/前端真链路通过；统一错误体 {detail, code, trace_id}；契约 C4 全场景通过
- **产出**：voiceprints.py 路由文件 + main.py 注册行
- **优先级**：P0

### T8 · identity_service — 身份级联编排（13:30–14:30）

> 与 T6-T7 并行写（接口契约 C5 已定，可先按 Mock 比对结果开发，A2 消费方也按 Mock 开发解耦）。

- **写作用域**：`app/services/identity_service.py`
- **前置依赖**：T3 ORM 模型、voiceprint_service 接口契约（比对输入/输出形状）
- **动作清单**：
  1. **候选集获取**：`device_id → devices.elder_id → elders.persona_id`（**1v1，恒 1 位**，技术文档 v1.5 + day-04 v2.3 裁决）
  2. **每会话首轮强制校验**：调 voiceprint_service.cosine_compare(音频, candidate_embeddings)
     - cosine ≥ 0.85 → `identity_source=voice`，elder_id 设为 candidate
     - cosine < 0.85 → 进入主动确认分支（reason=low_confidence）
     - 推理容器不可用 → 进入主动确认分支（reason=vp_unavailable）
  3. **主动确认分支**（需 A2 signaling 对接，C5 契约）：
     - 下行 identity_ask（candidates 恒 1 位）
     - 30s 内收到 identity_confirm（elder_id 必须等于 candidates[0].elder_id，否则按未确认处理）
     - 超时 30s → `identity_source=generic`，elder_id=NULL
  4. **静默复检**（会话内后续轮次）：
     - 每 utterance 后复检一次
     - 命中当前身份 → 不打断继续对话
     - 身份变化（换人 / 访客插话）→ 结束旧 thread（ended_at 落库）→ 重走首轮校验开新 thread
  5. **conversation_threads 闸门①**：首轮身份确定后事务内 `session_seq + 1`、INSERT conversation_threads（thread_id 服务端按 `(elder_id, session_seq)` 生成，不采信前端传入）
  6. **单元测试矩阵**：
     - 同人样本首轮 → voice 命中
     - 异人样本首轮 → low_confidence → identity_ask → identity_confirm → confirm
     - 异人样本 + 30s 超时 → generic（elder_id=NULL）
     - 推理容器不可用 → vp_unavailable → 同主动确认分支
     - 候选集外 elder_id confirm → 按未确认处理
     - 会话内换人 → 旧 thread ended_at + 新 thread
     - thread_id 伪造 → 403（AC-39）
     - generic 模式下不得读记忆 / 不得写健康 / 不得确认用药（FR-VPR-03）
- **验收**：单/双人间统一三态（命中/确认/通用）；每会话首轮强制 + 静默复检；候选集恒 1 位；所有分支单测全绿
- **产出**：identity_service.py + tests/test_identity_service.py
- **优先级**：P0

### T9 · pytest 全量回归 + ruff 门禁（14:30–15:30）

- **动作**：
  1. `uv run pytest -q tests/test_voiceprint_service.py tests/test_identity_service.py tests/test_voiceprints_api.py -v` —— 新增测试全绿
  2. `uv run pytest -q` —— 全量回归（已有 411+ 用例不得红）
  3. `uv run ruff check app tests` —— lint 通过，无新增 warning
  4. `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` —— 迁移可回滚
  5. 主服务 venv 内无 torch：`uv run python -c "import torch"` → ModuleNotFoundError
- **产出**：pytest 通过确认 + ruff 通过 + alembic 可回滚
- **验收**：所有门禁通过后进 14:30 联调窗口
- **优先级**：P0（联调准入门槛）

### T10 · 14:30–17:00 联调窗口 + 收口

- **0004 与 0005 迁移入 gitignore 冲突检查**：embedding 加密列不应被 autogenerate 误生成明文列
- **与 A2 对接**：identity_service 消费 voiceprint_service 的比对接口形状确认；signaling.py 的 identity_ask/confirm 消息格式与 C5 一致
- **与前端联调**：C4 REST 三接口在 pad 声纹引导页通过；3 段注册 → status=active
- **端到端场景**：说话 → 身份级联 → 开 thread → LLM → TTS（A2 侧）
- **异常场景**：停掉 voiceprint 容器 → 503 + 主动确认兜底；换人 → 旧 thread 结束 + 新 thread 开启
- **README 登记**：voiceprint 端口 7003、VOICEPRINT_* 环境变量、模型放置步骤、推理容器起服命令
- **一次性提交**：`git add .` → commit message 含 Conventional Commits 格式（feat: voiceprint identity closed loop）
- **产出**：联调通过 + README 回填 + 提交
- **优先级**：P0

---

## 2. 时间分配（精确到小时，对齐 Day 4 时间表）

| 时段 | 子任务 | 预估耗时 | 优先级 | 关键里程碑 |
| --- | --- | --- | --- | --- |
| 09:00–09:30 | T0 契约冻结签字 | 30 min | 阻断 | C4/C5 锁定 |
| 09:30–11:00 | T1 Spike CAM++ 环境验证 | 90 min | P0 关键路径 | 维度 + cosine 分布 |
| 09:30–11:30 | T2 迁移 0004（与 T1 并行） | 120 min | P0 | voiceprints + conversation_threads 表 |
| 11:00–12:00 | T3 ORM 模型（与 T2 并行收尾） | 60 min | P0 | 两表 ORM + alembic check 无漂移 |
| 11:30–13:00 | T4 推理容器（骨架可先搭） | 90 min | P0 | 容器起服 + 健康检查 |
| **12:30–12:40** | **午站会：spike 结论通报** | **10 min** | **强制** | **维度参数定案** |
| 12:30–13:30 | T5 迁移 0005（等 spike 结论） | 60 min | P0 | embedding vector(N) 列 |
| 13:00–14:00 | T6 voiceprint_service | 60 min | P0 | 注册/加密/cosine/删除 |
| 13:30–14:30 | T8 identity_service（与 T6 并行） | 60 min | P0 | 级联编排 + 1v1 + 静默复检 |
| 14:00–14:30 | T7 REST 三接口（与 T8 并行） | 30 min | P0 | samples/status/delete + main.py 注册 |
| 14:30–15:30 | T9 pytest + ruff + alembic 门禁 | 60 min | P0 联调准入 | 全绿 + 可回滚 |
| 15:30–17:00 | T10 联调窗口 + README + 提交 | 90 min | P0 收口 | 端到端通过 + 一次性提交 |

---

## 3. 依赖说明

### 3.1 前置条件（无跨人硬依赖）

| 依赖项 | 说明 | 就绪状态 |
| --- | --- | --- |
| Day 3 音频链路（ASR/TTS/WS 信令） | A1 独立，不依赖 day03 其他分支 | ✅ 已交付 |
| Day 2 设备 JWT 鉴权 | REST 三接口与 identity_service 复用设备会话依赖 | ✅ 已交付 |
| pgvector 扩展 | voiceprints.embedding 列需要 pgvector 类型 | ✅ pyproject.toml 已声明，docker compose postgres 已启用 |
| Fernet 加密库 | embedding 加密落库需要 cryptography.fernet.Fernet | ✅ pyjwt[crypto] 已含 cryptography 依赖 |
| C4/C5 契约冻结 | 晨会 09:00–09:30 签字确认后开工 | ⏳ 待晨会 |
| spikes/reference/asr/ 下的 CAM++ 模型 | 已有 `3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx` | ✅ 仓库内已存在 |
| 参考项目 examples/ 下的声纹样本 | speaker1_a/b / speaker2_a/b 四段 WAV | ✅ 仓库内已存在 |

### 3.2 外部资源

| 资源 | 说明 | 保障措施 |
| --- | --- | --- |
| sherpa-onnx Python 包 | T1 spike + T4 推理容器都需要 | 📦 pip 包管理器已有（容器内 pip install sherpa-onnx） |
| pyjwt[crypto] → cryptography → Fernet | embedding 加密 | 📦 pyproject.toml 已有 pyjwt[crypto] |
| A3 统一录入的 Settings 字段 | VOICEPRINT_* 配置 | 📢 晨会报 A3 录入，群内通告 |
| docker + docker compose | 推理容器起服 | 🛠️ 本地环境已有 |
| postgres + pgvector | voiceprints 表迁移 | 🐳 `docker compose up -d postgres` 已起 |

### 3.3 阻塞预案

| 阻塞 | 预案 |
| --- | --- |
| CAM++ T1 spike 跑不通 | 立即启 resemblyzer 兜底（256 维），0005 迁移维度随结论改；Fernet 加密列不受维度影响 |
| resemblyzer 也不通 | identity 走纯主动确认兜底，声纹比对降级到 Day 5，不阻塞 A2（C5 Mock 比对已解耦） |
| 推理容器起不来 | A1 自起 docker compose 中 voiceprint 服务；或配 VOICEPRINT_BASE_URL 指向队友共享实例（IP 群内分发不入仓库）；不可用时 503 → 主动确认兜底 |
| A3 Settings 字段未及时录入 | voiceprint_service 内用 `get_settings()` 硬编码临时默认值（不写死生产密钥），A3 录入后自动生效 |
| pytest 红 | 严格按"改一个子任务 → 跑一组测试"节奏开发，避免堆积；发现红先定位根因再修 |

---

## 4. 验收标准（按子任务）

### 整体验收（task-01 交付物）

```
[1] spike 结论：CAM++ 192 维容器内可跑、同/异人 cosine 分布实测（spikes/voiceprint/）
[2] 推理容器 deploy/voiceprint/ + compose 服务：/extract 正常返回 192 维、健康检查通过
[3] 迁移 0004/0005：upgrade/downgrade 可逆
[4] voiceprint_service：逐段上传（重复 409）、Fernet 加密、原始音频取完即弃
[5] identity_service：每会话首轮强制校验 + 静默复检，进程内 cosine ≥0.85 命中，低置信/容器不可用 → 主动确认（候选集恒 1 位）
[6] REST 三接口：{samples, status, delete} + 统一错误体 {detail, code, trace_id}
```

### 每个子任务的完成判定

| 子任务 | 验收命令 / 判据 |
| --- | --- |
| T0 契约冻结 | contracts.md 定稿确认记录表 C4/C5 行签完 |
| T1 Spike | `spikes/voiceprint/README.md` 含 cosine 矩阵，12:30 午站会通报 |
| T2 迁移 0004 | `uv run alembic upgrade head` + `downgrade` 往返可逆；唯一约束生效（手动 INSERT 验证） |
| T3 ORM | `uv run alembic check` 无漂移；既有 pytest 全绿 |
| T4 推理容器 | `docker compose up -d voiceprint` → health=healthy → POST /extract 返回 192 维 |
| T5 迁移 0005 | embedidng 列 vector(N) 与 T1 spike 维度一致；与 T2 同迁往返可逆 |
| T6 voiceprint_service | 新增测试全绿；库内 embedding 列为密文；停容器 → 503；同人比对 cosine ≥0.85 |
| T7 REST 三接口 | curl 三接口返回符合 C4 契约；统一错误体 |
| T8 identity_service | 三态（voice/confirm/generic）单测全绿；候选集恒 1 位；thread_id 伪造 403 |
| T9 门禁 | `uv run pytest -q` 全绿；`ruff check app` 通过；`alembic upgrade head && downgrade -1 && upgrade head` 可逆；主服务 venv 内 `import torch` → ModuleNotFoundError |
| T10 联调 | A1↔A2 身份级联全链通过；异常场景（停容器 → 503 + 主动确认）通过；README 回填 |

### 技术验收对照（PRD + 技术文档）

| PRD 条款 | 技术文档条款 | 本计划覆盖 |
| --- | --- | --- |
| FR-VPR-01 长者 ID + 声纹双条件注册匹配 | §3.4 voiceprints 表 + 裁决 C1（唯一约束） | T2/T3/T6 |
| FR-VPR-02 低置信主动询问 / 服务不可用降级 | §3.0 图 ② 身份级联分支 + C5 契约 | T8 |
| FR-VPR-03 通用模式隔离（不读记忆/不写健康/不确认用药） | §3.1 要点 1 + CHECK 约束 | T2 CHECK + T8 测试矩阵 |
| AC-36 原始音频不落盘 | §3.1 要点 4 | T6 "原始音频 bytes 即弃" + 测试断言 |
| AC-39 thread_id 伪造 403 | §3.6 conversation_threads PK 口径 | T8 测试矩阵 |
| 技术文档 §1.4 选型口径（不装 torch） | Day 4 批次 2 张表 | T4 推理容器 + T9 venv 验证 |
| AGENTS §7 cosine ≥0.85 写死常量 | voiceprint_similarity_threshold 已在 config.py | T6 直接引用 settings |

---

## 5. 风险预案

| # | 风险 | 概率 | 影响 | 缓解 / 应对 |
| --- | --- | --- | --- | --- |
| R1 | CAM++ T1 spike 环境不通（torch 重依赖、下载慢） | 中 | P0，阻塞 0005 迁移写 | **先有兜底分支（主动确认/通用模式）再接真模型**；resemblyzer 作轻量备选（256 维）；12:30 前仍不可行 → identity 走纯主动确认，声纹比对降级 Day 5 |
| R2 | embedding 维度在 spike 前误写迁移 | 低 | P0，违反裁决 B2 迁移返工 | T2 只建 0004（不含 embedding 列）；0005 严格排在 T3 spike 结论之后，12:30 午站会定案 |
| R3 | 推理容器超时/不可用 | 高 | P0，阻断注册与比对 | voiceprint_service 对 503 → 主动确认兜底（reason=vp_unavailable）；主服务自起或配共享实例 |
| R4 | embedding 加密 Fernet key 未生成 | 低 | P1，embedding 明文落库 | `.env` 本地生成不入 Git，`.env.example` 只放占位符（AGENTS §4.2 红线）；A3 录入时同步提醒 |
| R5 | 原始音频被调试开关外落盘 | 低 | P0，违反 AC-36 | voiceprint_service 用 `bytes` 参数（非 file path），提取完成后无任何持久化操作；测试断言无临时文件残留 |
| R6 | 公共文件 config.py 字段冲突 | 中 | P1，A3 汇总时覆盖 | 晨会统一报 A3，群内通告所有新增字段（VOICEPRINT_BASE_URL 等）；A3 统一录入后再提交 |
| R7 | main.py 装配冲突 | 低 | P1，后提交者 rebase | A7 REST 三接口注册只占 main.py 一行（include_router）；与 A2 路由装配行无重叠；冲突时后提交者 rebase |
| R8 | 迁移 autogenerate 漂移 | 中 | P0，embedding 加密列被误生成明文列 | 严格用 `alembic check` 验证 ORM ↔ 迁移同步；embedding.encrypted 字段名必须与 0005 ADD COLUMN 后的列类型一致 |
| R9 | pytest 堆积红灯 | 中 | P0，联调准入失败 | 严格按子任务粒度开发 → 每个子任务完成跑对应的单测 → 避免堆积；发现红先定位根因再修 |
| R10 | A1↔A2 接口形状不一致 | 低 | P1，联调时阻塞 | C5 契约已定稿（contracts.md v1.0）；identity_service 消费 voiceprint_service 的比对接口形状与 A2 按 Mock 开发一致；联调窗口 14:30 前形状应已稳定 |

---

## 6. 本卡严格禁触清单（AGENTS §2 + day04 README §3）

- ❌ `app/audio/signaling.py`（A2 写作域）
- ❌ `app/services/llm_service.py`（A2 写作域）
- ❌ `app/services/dialogue_service.py`（A2 写作域）
- ❌ `app/core/context.py`（A2 写作域）
- ❌ frontend/ 所有文件（pad/console/family）
- ❌ 已合入迁移 0001–0003（只新增 0004/0005）
- ❌ `app/core/config.py`（Settings 新字段报 A3 统一录入）
- ❌ `app/main.py` 除路由装配行外的任何改动

---

## 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.0 | 2026-11-06 | 初稿：A1 声纹身份闭环 10 子任务（T0 晨会签字 → T1 spike → T2 迁移 0004 → T3 ORM → T4 推理容器 → T5 迁移 0005 → T6 voiceprint_service → T7 REST 三接口 → T8 identity_service → T9 门禁 → T10 联调收口）；时间分配精确到小时对齐 Day 4 时间表；依赖说明含前置条件 + 外部资源 + 阻塞预案；验收标准按子任务粒度 + PRD/技术文档条款映射；风险预案 10 条 |
| v1.1 | 2026-11-06 | identity_service thread 生命周期修正：`verify_session_identity` / `open_thread_from_confirm` / `open_thread_as_generic_on_timeout` 建新 thread 前先结束该设备未结束 thread；新增 `tests/test_identity_thread_lifecycle.py`（FakeSession 顺序测试，不依赖 DB） |
| v1.2 | 2026-11-06 | 静态复检策略修正：已识别会话低于阈值 → 结束旧 thread + `confirm/low_confidence`；generic 命中 → voice 新 thread；同人/generic 未命中/vp 不可用/未绑定 → None；补 5 类 FakeSession 复检测试 |
| v1.3 | 2026-11-06 | `session_seq` 落地：personal thread 同事务 `Elders.session_seq + 1 RETURNING`，thread_id 用 `(elder_id, session_seq)`；generic session_seq=0 + UUID hex；`IdentityDecision.session_seq` 回传 A2 |
