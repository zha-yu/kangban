# X8 媒体域证据（CP4 · AC-37 media_service 侧）

> 2026-11-11 · dev-C · 本地 PostgreSQL（docker compose postgres）· alembic head
> 方法：一次性脚本直调 `app/services/media_service.py`，返回值原样记录，无手工修饰。
> 口径：契约 §6.1 X8——media_url 只产稳定路径 `/static/media/opera/<media_id>.wav`。


## 1. 种子写入 seed_media_library（5 行，media_id 服务端生成）

```json
[
  {
    "media_id": "c95898c3-3474-43cd-b8d9-326b66730dbb",
    "type": "opera",
    "title": "红楼梦·葬花吟选段",
    "genre": "越剧",
    "era": "经典",
    "artist": "教师提供素材",
    "duration": 300,
    "copyright_status": "teacher_authorized",
    "media_url": "/static/media/opera/c95898c3-3474-43cd-b8d9-326b66730dbb.wav"
  },
  {
    "media_id": "d36ea442-b15d-41f3-b241-cedd5d7c17ac",
    "type": "opera",
    "title": "红楼梦·读西厢选段",
    "genre": "越剧",
    "era": "经典",
    "artist": "教师提供素材",
    "duration": 260,
    "copyright_status": "teacher_authorized",
    "media_url": "/static/media/opera/d36ea442-b15d-41f3-b241-cedd5d7c17ac.wav"
  },
  {
    "media_id": "005d1655-9f15-48ee-a389-dc9a2a123583",
    "type": "opera",
    "title": "梁祝·十八相送",
    "genre": "越剧",
    "era": "经典",
    "artist": "教师提供素材",
    "duration": 420,
    "copyright_status": "public_domain",
    "media_url": "/static/media/opera/005d1655-9f15-48ee-a389-dc9a2a123583.wav"
  },
  {
    "media_id": "7f6b89af-c722-4b25-87e1-6077cdcb5c69",
    "type": "opera",
    "title": "锁麟囊·春秋亭",
    "genre": "京剧",
    "era": "经典",
    "artist": "教师提供素材",
    "duration": 380,
    "copyright_status": "teacher_authorized",
    "media_url": "/static/media/opera/7f6b89af-c722-4b25-87e1-6077cdcb5c69.wav"
  },
  {
    "media_id": "5136997c-414c-4aba-8b6e-c6478d290101",
    "type": "movie",
    "title": "黑猫警长（电影版）",
    "genre": "动画",
    "era": "怀旧",
    "artist": "教师提供素材",
    "duration": 1500,
    "copyright_status": "teacher_authorized",
    "media_url": "/static/media/opera/5136997c-414c-4aba-8b6e-c6478d290101.wav"
  }
]
```

## 2. 列表 list_media(media_type='opera')

```json
[
  {
    "media_id": "005d1655-9f15-48ee-a389-dc9a2a123583",
    "type": "opera",
    "title": "梁祝·十八相送",
    "genre": "越剧",
    "era": "经典",
    "artist": "教师提供素材",
    "duration": 420,
    "copyright_status": "public_domain",
    "media_url": "/static/media/opera/005d1655-9f15-48ee-a389-dc9a2a123583.wav"
  },
  {
    "media_id": "7f6b89af-c722-4b25-87e1-6077cdcb5c69",
    "type": "opera",
    "title": "锁麟囊·春秋亭",
    "genre": "京剧",
    "era": "经典",
    "artist": "教师提供素材",
    "duration": 380,
    "copyright_status": "teacher_authorized",
    "media_url": "/static/media/opera/7f6b89af-c722-4b25-87e1-6077cdcb5c69.wav"
  },
  {
    "media_id": "c95898c3-3474-43cd-b8d9-326b66730dbb",
    "type": "opera",
    "title": "红楼梦·葬花吟选段",
    "genre": "越剧",
    "era": "经典",
    "artist": "教师提供素材",
    "duration": 300,
    "copyright_status": "teacher_authorized",
    "media_url": "/static/media/opera/c95898c3-3474-43cd-b8d9-326b66730dbb.wav"
  },
  {
    "media_id": "d36ea442-b15d-41f3-b241-cedd5d7c17ac",
    "type": "opera",
    "title": "红楼梦·读西厢选段",
    "genre": "越剧",
    "era": "经典",
    "artist": "教师提供素材",
    "duration": 260,
    "copyright_status": "teacher_authorized",
    "media_url": "/static/media/opera/d36ea442-b15d-41f3-b241-cedd5d7c17ac.wav"
  }
]
```

## 3a. 检索 search_media('红楼梦')——匹配 title，X8 口径②

```json
[
  {
    "media_id": "c95898c3-3474-43cd-b8d9-326b66730dbb",
    "type": "opera",
    "title": "红楼梦·葬花吟选段",
    "genre": "越剧",
    "era": "经典",
    "artist": "教师提供素材",
    "duration": 300,
    "copyright_status": "teacher_authorized",
    "media_url": "/static/media/opera/c95898c3-3474-43cd-b8d9-326b66730dbb.wav"
  },
  {
    "media_id": "d36ea442-b15d-41f3-b241-cedd5d7c17ac",
    "type": "opera",
    "title": "红楼梦·读西厢选段",
    "genre": "越剧",
    "era": "经典",
    "artist": "教师提供素材",
    "duration": 260,
    "copyright_status": "teacher_authorized",
    "media_url": "/static/media/opera/d36ea442-b15d-41f3-b241-cedd5d7c17ac.wav"
  }
]
```

## 3b. 检索 search_media('越剧')——匹配 genre（「换一段」取数路径，X8 口径③）

```json
[
  {
    "media_id": "005d1655-9f15-48ee-a389-dc9a2a123583",
    "type": "opera",
    "title": "梁祝·十八相送",
    "genre": "越剧",
    "era": "经典",
    "artist": "教师提供素材",
    "duration": 420,
    "copyright_status": "public_domain",
    "media_url": "/static/media/opera/005d1655-9f15-48ee-a389-dc9a2a123583.wav"
  },
  {
    "media_id": "c95898c3-3474-43cd-b8d9-326b66730dbb",
    "type": "opera",
    "title": "红楼梦·葬花吟选段",
    "genre": "越剧",
    "era": "经典",
    "artist": "教师提供素材",
    "duration": 300,
    "copyright_status": "teacher_authorized",
    "media_url": "/static/media/opera/c95898c3-3474-43cd-b8d9-326b66730dbb.wav"
  },
  {
    "media_id": "d36ea442-b15d-41f3-b241-cedd5d7c17ac",
    "type": "opera",
    "title": "红楼梦·读西厢选段",
    "genre": "越剧",
    "era": "经典",
    "artist": "教师提供素材",
    "duration": 260,
    "copyright_status": "teacher_authorized",
    "media_url": "/static/media/opera/d36ea442-b15d-41f3-b241-cedd5d7c17ac.wav"
  }
]
```

## 3c. 检索 search_media('不存在的剧目')——无命中返回空，不编造

```json
[]
```

## 4. 取播放 URL get_playback_url(media_id=c95898c3-3474-43cd-b8d9-326b66730dbb)

- 返回值：`/static/media/opera/c95898c3-3474-43cd-b8d9-326b66730dbb.wav`
- 期望值：`/static/media/opera/c95898c3-3474-43cd-b8d9-326b66730dbb.wav`（模板 `/static/media/opera/{media_id}.wav`）
- 逐字断言：**一致** ✅

---

四步全部通过：种子 5 行落库、opera 列表 4 行、检索命中/剧种命中/空命中如实、播放 URL 与冻结模板逐字一致。
