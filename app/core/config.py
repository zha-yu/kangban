"""集中配置：全部环境变量只在这里声明，禁止在业务代码里 `os.environ.get` 散读。

对齐仓库根目录 `.env.example` 的 17 组变量（技术文档未定义变量名，
本项目自定义口径见 README §6）。三条硬要求：

1. 每个字段都有默认值 —— 没有 `.env` 也能启动（大纲 1.4「Pydantic Settings 加载 .env」，
   而不是「没有 .env 就崩」）；
2. `extra="ignore"` —— `.env` 里多出不认识的键不报错，避免加变量时全组翻车；
3. 变量名与字段名大小写不敏感映射（`APP_NAME` -> `app_name`），保持 `.env.example`
   全大写、代码全小写的既有约定。
"""

from __future__ import annotations

import warnings
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根：app/core/config.py -> app/core -> app -> 根
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _REPO_ROOT / ".env"

#: 旧变量（LAYA_*）弃用告警的进程级去重集合。放模块层而不是 Settings 上，
#: 避免每实例重复刷屏；同时保证 A1 验收要求的"有且仅有一次"。
_WARNED_LEGACY: set[str] = set()


def _warn_once(message: str) -> None:
    if message in _WARNED_LEGACY:
        return
    _WARNED_LEGACY.add(message)
    warnings.warn(message, DeprecationWarning, stacklevel=3)


class Settings(BaseSettings):
    """全量配置。分组顺序与 `.env.example` 一一对应，便于双向核对。"""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------- 应用 ----------------------------
    app_name: str = "silvermate"
    app_env: str = "local"
    debug: bool = False
    api_host: str = "0.0.0.0"  # noqa: S104 - 容器内需监听全部网卡，对外暴露由 nginx+TLS 收口
    api_port: int = 8000
    public_base_url: str = "https://localhost"
    frontend_origin: str = "https://localhost:5173"
    log_level: str = "INFO"

    # ---------------------------- 鉴权 / JWT（Day 2 起使用） ----------------------------
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 120
    refresh_token_expire_days: int = 7
    elder_session_expire_hours: int = 24

    # ---------------------------- 3D 数字人资产（Day 4 · A3） ----------------------------
    # 三层取值链的第二层：personas.avatar_url 为空时才用这里的默认值。
    # 库里与这里都存「相对路径」（如 /avatars/xxx.vrm）；响应层统一拼 public_base_url 成绝对 URL。
    default_avatar_url: str = ""
    default_fallback_avatar_url: str = ""  # 静态头像 PNG：3D 加载失败或超 5s 时前端使用（AC-15）
    avatar_rotation_y_offset: float = 0.0  # 仅 VRM0 需要 π 补偿；VRM1 保持 0.0（见计划 S-02）
    avatar_cache_version_salt: str = ""  # 想让前端强制失效缓存时改这个值，version 指纹会随之变化

    # ---------------------------- 数据库（业务表） ----------------------------
    database_url: str = "postgresql+asyncpg://silvermate:silvermate@localhost:5432/silvermate"
    database_pool_size: int = 10
    database_pool_max_overflow: int = 20
    # 测试专用库（阶段 C 根治）：pytest 走这里，与开发库物理隔离，并行才不会互相踩数据。
    # None 时由 tests/conftest.py 从 database_url 推导 <库名>_test，绝不回退到开发库本身。
    database_url_test: str | None = None

    # ---------------------------- LangGraph Checkpoint（独立 schema + 独立连接池） ----------------------------
    langgraph_checkpoint_database_url: str = (
        "postgresql://silvermate:silvermate@localhost:5432/silvermate"
    )
    langgraph_checkpoint_schema: str = "langgraph"
    langgraph_checkpoint_pool_min_size: int = 1
    langgraph_checkpoint_pool_max_size: int = 5

    # ---------------------------- 缓存 / 限流 ----------------------------
    redis_url: str = "redis://localhost:6379/0"
    redis_key_prefix: str = "silvermate"
    rate_limit_per_minute: int = 60

    # ---------------------------- System Two · 大模型 ----------------------------
    llm_provider: str = "deepseek"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: int = 20
    llm_max_retries: int = 2
    llm_temperature: float = 0.6

    # ---------------------------- 长期记忆 · Embedding（Day 9 B2 / 裁决 D3） ----------------------------
    # 口径：OpenAI 兼容 `POST {base_url}/embeddings`，维度固定 1536（与
    # `models/memory.py:42` 的 vector(1536) 和 TD line 717 一致，免迁移）。
    # base_url / model 任一为空 → 视为未配置：写入降级为无向量行、召回本轮不加载记忆。
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dim: int = 1536
    embedding_timeout_seconds: float = 5.0
    #: 单次请求文本条数上限。代码侧另有硬上限 16（`_EMBEDDING_MAX_BATCH`），配置放大不生效。
    embedding_batch_size: int = 16

    # ---------------------------- System One · Laya 本地推理（Day 7 A1） ----------------------------
    # 口径来源：docs/plans/day07/dev-a-三级路由与决策日志.md §5.2.0（实测证据）
    #   laya-multilingual（322M/F16/615MB/apache-2.0），推理入口是仓库自带 rl_agent_api.RLAgent。
    # 端口 7001 不变（sidecar 仍在该端口），避免 nginx / 防火墙 / 前端配置连带改动。
    system_one_enabled: bool = True
    system_one_provider: str = "laya"  # laya | mock；生产禁 mock（启动时校验）
    system_one_base_url: str = "http://127.0.0.1:7001"
    system_one_api_key: str = ""  # 空 → sidecar 请求不带 Authorization
    system_one_timeout_ms: int = 200  # PRD §5.3 固定意图路由 ≤200ms，不得为"跑通"调大
    system_one_confidence_threshold: float = 0.85  # 低于此值只澄清一次（PRD FR-DLG-03）
    system_one_fallback_to_agent: bool = True  # 失败/低置信下沉 Agent，不调第二个分类器
    system_one_model_id: str = "laya-multilingual"
    system_one_model_path: str = ""  # 空 → sidecar /health 报 model_missing（必须显式）
    system_one_model_version: str = "laya:multilingual:dev"  # 写入 decision_logs.model_version
    system_one_device: str = "cpu"  # TD §1.4 口径：CPU 推理
    # CPU 实测（16 逻辑核 / torch 2.14.0+cpu）：单问 167ms、两问 285ms、四问 668ms。
    # 并发 >1 会让单请求互相拖慢，直接顶破 200ms 预算 → 默认 1，需要吞吐时按机型再评估。
    system_one_max_concurrency: int = 1
    system_one_warmup: bool = True
    # 快路径只问 intent（实测 p95 167ms 达标）；is_sensitive 走 shadow，不阻塞路由，
    # 急症仍由 G0 规则硬兜底（2026-11-10 主理人裁决，见计划 §10-10）。
    system_one_fast_question: str = "intent"
    system_one_shadow_question: str = "is_sensitive"
    system_one_shadow_timeout_ms: int = 400

    # ---- 以下 6 个 laya_* 为 Day 6 及以前的旧字段，仅做兼容映射（新代码禁止再读） ----
    # 下个版本删除；删除时同步 .env.example 与 README 配置表。
    laya_enabled: bool | None = None
    laya_base_url: str | None = None
    laya_api_key: str | None = None
    laya_timeout_ms: int | None = None
    laya_confidence_threshold: float | None = None
    laya_fallback_to_llm: bool | None = None

    # ---------------------------- 语音识别 ASR ----------------------------
    asr_provider: str = ""
    asr_base_url: str = ""
    asr_app_id: str = ""
    asr_api_key: str = ""
    asr_language: str = "zh-CN"
    asr_timeout_ms: int = 300
    asr_emit_confidence: bool = True
    # FunASR 热词（"词 权重" 空格分隔，权重 1~100）：空 → 握手不带 hotwords 字段，回落原行为
    # （adhoc-20261106 收音与识别优化）。镜像模型是否启用 seaco 解码未确认，字段不生效也无害。
    asr_hotwords: str = ""
    # FunASR runtime 镜像默认开 TLS 且内置自签证书（容器日志 "SSL is opened!"）。
    # 置 true 才跳过证书校验；公网/正式环境应换真证书并把这里关掉。
    asr_insecure: bool = False

    # ---------------------------- 语音合成 TTS ----------------------------
    tts_provider: str = ""
    tts_base_url: str = ""
    tts_app_id: str = ""
    tts_api_key: str = ""
    tts_voice_id: str = ""
    tts_speed: float = 1.0
    tts_timeout_ms: int = 300
    # edge 兜底链路用的 ffmpeg 可执行文件（默认走 PATH；Windows 装了 WinGet 包
    # 但 IDE 进程 PATH 未刷新时可填绝对路径）
    tts_ffmpeg_bin: str = "ffmpeg"

    # ---------------------------- WebRTC / TURN ----------------------------
    stun_url: str = "stun:localhost:3478"
    turn_url: str = "turn:localhost:3478"
    turn_username: str = "silvermate"
    turn_credential: str = ""
    turn_credential_ttl_seconds: int = 3600
    webrtc_ice_timeout_seconds: int = 5
    webrtc_audio_bitrate_kbps: int = 32
    # adhoc-20261106：2→3（0~3 档里最强的抗噪判定），配合端侧 TEN-VAD threshold 0.6
    # 收窄收音范围，减少远处他人语音被误切段送进 ASR。这是运行时唯一生效的口径——
    # webrtc.py 的判定会话与 VadSession 无参兜底路径都以此为准（VadSession 数据类
    # 默认值与之保持一致）。
    webrtc_vad_aggressiveness: int = 3

    # ---------------------------- 天气（唯一真实外部接口） ----------------------------
    weather_api_provider: str = ""
    weather_api_base_url: str = ""
    weather_api_key: str = ""
    weather_cache_ttl_seconds: int = 600
    weather_default_city: str = ""

    # ---------------------------- 通知 ----------------------------
    notify_channel: str = "inapp"
    webpush_vapid_public_key: str = ""
    webpush_vapid_private_key: str = ""
    webpush_subject: str = "mailto:admin@example.com"
    notify_retry_interval_seconds: int = 30
    notify_max_retries: int = 3

    # ---------------------------- MCP 客户端 ----------------------------
    mcp_weather_server_url: str = "http://localhost:7101"
    mcp_operations_server_url: str = "http://localhost:7102"
    mcp_call_timeout_seconds: int = 5

    # ---------------------------- 观测 ----------------------------
    langfuse_enabled: bool = False
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_sample_rate: float = 1.0
    otel_enabled: bool = True
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_service_name: str = "silvermate-backend"
    metrics_enabled: bool = True

    # ---------------------------- 数据保留 / 调度 ----------------------------
    audio_retention_hours: int = 72
    audio_debug_save: bool = False
    # F27 原始音频清理扫描目录（audio_retention_jobs.sweep 的扫描根）；
    # 落盘生产方（谁往这个目录写）登记 Day 10。
    audio_debug_dir: str = "var/audio_debug"
    scheduler_timezone: str = "Asia/Shanghai"
    health_checkup_start: str = "19:00"
    health_checkup_end: str = "20:00"
    # Day 10 A5 设备掉线检测（FR-ADM-10）：Device.last_heartbeat 注释口径
    # 「超 2 个心跳周期判掉线」。周期默认 60s（模拟设备语义），阈值=2*周期。
    device_heartbeat_period_seconds: int = 60
    # 掉线扫描 job 的运行间隔（半个心跳周期，保证至多迟到 1 次扫描的检测延迟）。
    device_offline_scan_interval_seconds: int = 30

    # ---------------------------- 业务阈值（改动需评审，见 AGENTS.md §7） ----------------------------
    max_tool_calls_per_turn: int = 8
    tool_repeat_window_seconds: int = 60
    tool_repeat_break_threshold: int = 3
    medication_reminder_attempts: int = 4
    medication_attempt_wait_seconds: int = 60
    # 主理人裁决 2026-11-11：0.55 → 0.62（Day 8 Step 9 首轮真人互测实测定稿，PRD v3.1）。
    # 实测（3 人 9 段，eval/reports/声纹调优报告.md）：同人 0.6664–0.8399、异人 0.2165–0.6512，
    # 0.55 档误识 5/27（FAR 18.5%）；0.62 档 FAR 3.7% / FRR 0，误识由三档级联主动确认兜底。
    # 沿革：0.85 → 0.55（v2.8，2026-11-07 CAM++ 教师口径）→ 0.62（v3.1）。运行时经 Settings 可调。
    voiceprint_similarity_threshold: float = 0.62

    # ---------------------------- Day 5 · 三档慢响应阈值（真正并行 watchdog，见 dev-b 计划 v3.1） ----------------------------
    # 语义：provider **还在跑但超过这个时间** 就先发 slow 提示，不等 provider 返回；
    #      provider 最终成功 → 正常继续；最终失败 → slow + unavailable/timeout 两条都发（后端两条都发、前端负责状态升级合并）。
    # 两套超时不混：provider 层断网兜底（FUNASR_RECEIVE_TIMEOUT_S=45s / llm_timeout_seconds=20）走 ASRUnavailable/LLMTimeout 异常 →
    #              signaling.deliver(code="asr_unavailable"/"llm_timeout")；这三档是「有结果但太慢」的分段耗时。
    degrade_asr_slow_ms: int = 2000
    degrade_llm_slow_ms: int = 3000
    degrade_tts_slow_ms: int = 1000

    # ---- 声纹推理容器（Day 4 · A1） ----
    # voiceprint_service 调这个容器的 POST /extract 端点，主服务不装 torch（容器隔离重依赖）
    voiceprint_base_url: str = "http://localhost:7003"
    voiceprint_extract_timeout_seconds: float = 5.0
    voiceprint_embedding_dimension: int = 192  # CAM++ 固定 192 维（spikes/voiceprint/ 验证）
    # Fernet 密钥：embedding 加密落库用。必须 32 字节 base64 编码；生产经 .env 注入，
    # .env.example 只放占位符（AGENTS §4.2 红线）。默认值是合法 32 字节 key、仅供
    # 本地开发兜底（此前默认串解码后 37 字节，Fernet 初始化必炸 500）。
    voiceprint_fernet_key: str = "ZGV2LXZvaWNlcHJpbnQtZmVybmV0LWtleS1sb2NhbDE="

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_laya(cls, data: Any) -> Any:
        """把 Day 6 及以前的 `LAYA_*` 映射到 `system_one_*`，每个旧变量只告警一次。

        pydantic-settings 会先把 `.env` 与环境变量合并成 dict 再交给 before-validator，
        所以这里既能接住 `.env`，也能接住 `Settings(LAYA_BASE_URL=...)` 这类显式入参。
        新旧同时给时**新值优先**，并把"旧变量被忽略"写进告警——不做静默二选一。
        """
        if not isinstance(data, dict):
            return data
        legacy = {
            "laya_enabled": "system_one_enabled",
            "laya_base_url": "system_one_base_url",
            "laya_api_key": "system_one_api_key",
            "laya_timeout_ms": "system_one_timeout_ms",
            "laya_confidence_threshold": "system_one_confidence_threshold",
            "laya_fallback_to_llm": "system_one_fallback_to_agent",
        }
        for old, new in legacy.items():
            if old not in data:
                continue
            value = data[old]
            if value is None:
                # 字段默认值是 None，代表"没配过"，不当成用户显式设置
                data.pop(old, None)
                continue
            if data.get(new) is not None:
                _warn_once(f"{old.upper()} 已弃用且被 {new.upper()} 覆盖（新值优先，旧变量忽略）")
                continue
            data[new] = value
            _warn_once(f"{old.upper()} 已弃用，请改用 {new.upper()}")
        return data

    @model_validator(mode="after")
    def _reject_mock_in_production(self) -> Settings:
        """生产环境禁用 MockBackend：契约测试可以 mock，交付环境不行。"""
        if self.is_production and self.system_one_provider.strip().lower() == "mock":
            raise ValueError("system_one_provider=mock 仅限测试；生产必须接本地真实权重")
        return self

    @property
    def cors_origins(self) -> list[str]:
        """`FRONTEND_ORIGIN` 支持逗号分隔多来源；空则不开 CORS 通配。"""
        return [item.strip() for item in self.frontend_origin.split(",") if item.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"prod", "production"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试可用 `get_settings.cache_clear()` 重置。"""
    return Settings()
