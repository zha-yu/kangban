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
    app_name: str = "konban"
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

    # ---------------------------- 数据库（业务表） ----------------------------
    database_url: str = "postgresql+asyncpg://konban:konban@localhost:5432/konban"
    database_pool_size: int = 10
    database_pool_max_overflow: int = 20

    # ---------------------------- LangGraph Checkpoint（独立 schema + 独立连接池） ----------------------------
    langgraph_checkpoint_database_url: str = "postgresql://konban:konban@localhost:5432/konban"
    langgraph_checkpoint_schema: str = "langgraph"
    langgraph_checkpoint_pool_min_size: int = 1
    langgraph_checkpoint_pool_max_size: int = 5

    # ---------------------------- 缓存 / 限流 ----------------------------
    redis_url: str = "redis://localhost:6379/0"
    redis_key_prefix: str = "konban"
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
    turn_username: str = "konban"
    turn_credential: str = ""
    turn_credential_ttl_seconds: int = 3600
    webrtc_ice_timeout_seconds: int = 5
    webrtc_audio_bitrate_kbps: int = 32
    webrtc_vad_aggressiveness: int = 2

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
    otel_service_name: str = "konban-backend"
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
    voiceprint_similarity_threshold: float = 0.85

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
