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

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根：app/core/config.py -> app/core -> app -> 根
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _REPO_ROOT / ".env"


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

    # ---------------------------- System One · Laya ----------------------------
    laya_enabled: bool = True
    laya_base_url: str = "http://localhost:7001"
    laya_api_key: str = ""
    laya_timeout_ms: int = 200
    laya_confidence_threshold: float = 0.85
    laya_fallback_to_llm: bool = True

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
    mcp_enable_audit: bool = True

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
    scheduler_timezone: str = "Asia/Shanghai"
    health_checkup_start: str = "19:00"
    health_checkup_end: str = "20:00"

    # ---------------------------- 业务阈值（改动需评审，见 AGENTS.md §7） ----------------------------
    max_tool_calls_per_turn: int = 8
    tool_repeat_window_seconds: int = 60
    tool_repeat_break_threshold: int = 3
    medication_reminder_attempts: int = 4
    medication_attempt_wait_seconds: int = 60
    # 主理人裁决 2026-11-07：0.85 → 0.55（CAM++ 教师口径）。依据 spikes/voiceprint/ 实测
    # 同人 cosine 0.517–0.568，0.85 阈值必误拒；PRD §3.2 已随 v2.8 修订。运行时经 Settings 可调。
    voiceprint_similarity_threshold: float = 0.55

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
    voiceprint_fernet_key: str = (
        "ZGV2LXZvaWNlcHJpbnQtZmVybmV0LWtleS1sb2NhbDE="
    )

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
