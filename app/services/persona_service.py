# owner=A3 · 依据 C7 v1.0 · PRD FR-3D-01/02（前端渲染所需的前置数据）
"""数字人形象配置领域服务（C7 v1.0，消费方 = 长者端 pad）。

本文件只回答一件事：**这台平板该渲染哪个数字人**。答案固定是 C7 的四个键，
取值链按契约「persona → Settings → 前端内置」三层降级，且降级点在**后端**：

1. `elders.persona_id` → `personas.avatar_url`（人设自己配了形象）；
2. `settings.default_avatar_url`（人设没配形象的统一默认）；
3. `None` —— 接口仍然 200，前端直接加载内置兜底模型（FR-3D-02 的最后一层）。

为什么是纯读服务、不写库：换形象只 UPDATE `personas.avatar_url`（C7 语义约定，零代码改动），
那是照护台的人设维护接口；本接口只被平板高频调用，写库在这里没有落点。
放在服务层而不是 API 层，是为了让 `app/api/pad.py` 只做鉴权与序列化（同 `elder_service.py`）。

`version` 为什么用内容指纹而不是计数器：C7 要求「`persona.avatar_url` 变更即递增」，
但库里没有这一列，本卡也不许改模型与迁移。指纹把「响应里那几个值的组合」映射成整数 ——
换形象、换默认、换补偿角、改 salt 都会得到新值，效果等价于递增且没有写库副作用；
crc32 截到 31 位是为了恒非负（前端拿它当缓存键的一部分，负数只会无谓地多一种形态）。

依赖方向：`app/api/pad.py → 本文件 → models`（AGENTS §2）。这里刻意不碰
`voiceprint_service` / `identity_service` / `llm_service` / `dialogue_service`：
形象配置与这三件事无关，串进来只会让平板首屏多背一串不必要的导入。
"""

from __future__ import annotations

import logging
import uuid
import zlib
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import get_trace_id
from app.models import Elder, Persona

logger = logging.getLogger(__name__)


class AvatarConfig(TypedDict):
    """C7 的 200 响应体：**严格四个键**，增删任何一个都是契约变更（需改 contracts.md）。"""

    avatar_url: str | None
    fallback_image_url: str | None
    rotation_y_offset: float
    version: int


def _absolutize(value: str | None, *, base_url: str) -> str | None:
    """相对路径 → 绝对 URL（C7 第 6 条）；任何「空」都收敛成 `None`。

    三条取值规则，每条都对应一个真实会出现的输入：

    1. `None` / 空串 / 纯空白 → `None`：`default_avatar_url` 的默认值就是空串，
       空串表达「未配置」；若直接拼接会得到 `https://localhost/` 这种「看起来像资源」
       的地址，前端会拿它去发请求再 404，比直接给 `None` 更糟。
    2. 已以 `http` 开头（`http://` 与 `https://`）→ 原样返回：允许运维把形象放到 CDN，
       绝对地址不该被再拼一层 `public_base_url`。
    3. 其余按 `base_url.rstrip("/") + "/" + path.lstrip("/")` 拼接：两侧都归一，
       避免 `https://localhost//avatars/x.vrm` 这类双斜杠（nginx 不会替你纠正）。

    `strip()` 只用于判定是否为空与清洗路径两端，不改变路径中间的内容 —— 文件名里
    带空格是合法的，不能被顺手改掉。

    实现约定：`base_url` 走参数而不是在这里读 Settings。本函数是纯函数，单测可以直接断言
    拼接规则，不必去动环境变量（这也是把它单独抽出来、「方便后续单测」的落点）。
    """

    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith("http"):
        return candidate
    return f"{base_url.rstrip('/')}/{candidate.lstrip('/')}"


def _fingerprint(
    *,
    avatar_url: str | None,
    fallback_image_url: str | None,
    rotation_y_offset: float,
    salt: str,
) -> int:
    """响应四值（除 `version` 外）的内容指纹，充当前端缓存失效键（C7 第 7 条）。

    两个刻意的口径：

    - 指纹打在**响应值**（已绝对化的 URL）上，而不是库里的原始相对路径。前端缓存的键
      就是这两个绝对 URL，改 `PUBLIC_BASE_URL`（换域名、加路径前缀）同样必须让旧缓存
      失效；若只哈希库里的相对路径，换域名后 `version` 不变，平板会继续用旧域名下的缓存。
    - `None` 交给 f-string 写成字面量 `"None"`，与任何真实文件名都不可能相同，
      不需要额外编码；`|` 是分隔符，URL 里出现 `|` 的概率可以忽略，且这里只要求
      「同一内容同值、不同内容大概率不同值」，不承担防碰撞职责。
    """

    material = f"{avatar_url}|{fallback_image_url}|{rotation_y_offset}|{salt}"
    return zlib.crc32(material.encode("utf-8")) & 0x7FFFFFFF


async def _persona_avatar_url(session: AsyncSession, *, elder_id: uuid.UUID) -> str | None:
    """第①层取值：`elders.persona_id → personas.avatar_url`；任一环缺失都返回 `None`。

    分两次查询而不是一条 JOIN，是为了区分两种「查不到」：

    - 长者行不存在：设备绑了一个 `elders` 里没有的 id，属数据缺口，要留一条带
      `trace_id` 的告警日志才查得出来；
    - 人设没配形象（`persona_id IS NULL` 或 `avatar_url` 为空）：正常配置状态，走 Settings 默认。

    一条 JOIN 会把两者压成同一个 `NULL`，日志里就再也分不开了。两张表在本系统的规模都是
    个位数行，多一次往返对平板首屏可以忽略。

    **长者行不存在时不抛 404**：C7 只定义了 200 与 401/403，且「形象查不到」不该让整页白屏
    （FR-3D-02 的降级要求）。缺行按「没配形象」处理，落到 Settings 那层再落到前端内置兜底；
    这也不泄露任何信息 —— 设备本来就知道自己绑的是哪个 id，响应里没有任何长者数据。
    """

    elder = await session.get(Elder, elder_id)
    if elder is None:
        logger.warning(
            "avatar_config_elder_missing",
            extra={"trace_id": get_trace_id() or "-", "elder_id": str(elder_id)},
        )
        return None
    if elder.persona_id is None:
        return None

    avatar_url = (
        await session.execute(
            select(Persona.avatar_url).where(Persona.persona_id == elder.persona_id)
        )
    ).scalar_one_or_none()
    # FK 保证人设存在；真查不到（行被绕过外键删掉）也按「没配形象」降级，不 500。
    if avatar_url is None or not avatar_url.strip():
        return None
    return avatar_url.strip()


async def get_avatar_config(session: AsyncSession, *, elder_id: uuid.UUID) -> AvatarConfig:
    """按 C7 的三层取值链产出平板渲染数字人所需的四个值。

    `elder_id` 必须由调用方从 `Identity` 取（`app/api/pad.py` 用 `current_device`），
    **不接受请求参数或模型传入**：AGENTS §4.9 要求长者身份只从服务端上下文注入，
    否则平板一说「我是别人」，形象就跟着换了。
    """

    settings = get_settings()

    source = await _persona_avatar_url(session, elder_id=elder_id)
    if source is None:
        # 第②层：人设没配形象时用统一默认。可能是空串，交给 `_absolutize` 再收敛成 None。
        source = settings.default_avatar_url

    avatar_url = _absolutize(source, base_url=settings.public_base_url)
    fallback_image_url = _absolutize(
        settings.default_fallback_avatar_url, base_url=settings.public_base_url
    )
    rotation_y_offset = settings.avatar_rotation_y_offset

    return AvatarConfig(
        # 第③层：连 Settings 默认都没有 → None，接口仍 200，前端用内置兜底。
        avatar_url=avatar_url,
        fallback_image_url=fallback_image_url,
        # VRM0 需要 π 补偿、VRM1 恒 0.0（见计划 S-02）。这里原样透传，不猜模型版本：
        # 猜错会把数字人整体转向 180°，而配置项本来就是让人改的。
        rotation_y_offset=rotation_y_offset,
        version=_fingerprint(
            avatar_url=avatar_url,
            fallback_image_url=fallback_image_url,
            rotation_y_offset=rotation_y_offset,
            salt=settings.avatar_cache_version_salt,
        ),
    )


__all__ = [
    "AvatarConfig",
    "get_avatar_config",
]
