/**
 * 形象选择页（Day05 A1 S5，方案 docs/plans/day05/a1-本地VRM与形象选择页.md §4.4）
 *
 * - 入口仅在 MainScreen standby 期暴露（会话中禁止换装，计划口径 §3.2）
 * - 一触即选：点卡片写 localStorage 并回调 onDone(true)，返回主屏后由父组件重挂
 *   AvatarStage 显式换装（D-3：不做热切换，重挂载读最新 localStorage）
 * - 角色卡结构参考离线形象总览：3:4 透明底全身立绘 + 底部信息区；
 *   浅底低饱和（P2 藤紫）只负责相框与文字，彩度全部留给立绘本身
 * - 立绘来自 public/assets/avatars/{id}.png（Q2 落地）；加载失败回落渐变占位，不白屏
 * - lipsync="none" 模型卡片标注「不支持口型」（R8-b），不隐藏、由长者自主选择
 * - pad 端规范：大字、≥48px 触控区、配色全部走 tokens 变量（前端页面方案）
 */
import { useState, type ReactNode } from "react";
import {
  AVATAR_CATALOG,
  avatarThumbUrl,
  readStoredAvatarId,
  resolveSelectedAvatar,
  writeStoredAvatarId,
  type AvatarCatalogItem,
} from "./avatar-catalog";

/** 立绘缺失时的兜底：与舞台降级层同一套渐变，保持形象位不空 */
const PLACEHOLDER_BG =
  "radial-gradient(circle at 50% 38%, var(--color-accent-soft) 0%, var(--color-brand-soft) 58%, var(--color-brand-line) 100%)";

function AvatarCard({
  item,
  selected,
  onPick,
}: {
  item: AvatarCatalogItem;
  selected: boolean;
  onPick: () => void;
}) {
  const [imgFailed, setImgFailed] = useState(false);

  return (
    <li>
      <button
        type="button"
        aria-disabled={selected}
        onClick={onPick}
        className={`flex h-full w-full flex-col overflow-hidden rounded-2xl border-2 text-left transition-transform duration-150 ${
          selected
            ? "cursor-default border-brand bg-surface"
            : "border-line bg-surface hover:-translate-y-1 active:scale-[0.99] active:bg-surface-sunken"
        }`}
      >
        {/* 3:4 立绘相框：统一浅灰紫底，14 张高彩度立绘不互相打架 */}
        <div className="relative flex aspect-[3/4] w-full items-center justify-center overflow-hidden bg-surface-sunken">
          {imgFailed ? (
            <span
              className="block h-3/4 w-3/4 rounded-full"
              style={{ background: PLACEHOLDER_BG }}
            />
          ) : (
            <img
              src={avatarThumbUrl(item.id)}
              alt={`${item.displayName} 的形象`}
              className="h-full w-full object-contain p-3"
              onError={() => setImgFailed(true)}
            />
          )}
          {selected && (
            <span className="absolute right-4 top-4 rounded-xl bg-brand px-4 py-2 text-xl font-semibold text-ink-inverse">
              使用中
            </span>
          )}
        </div>

        {/* 信息区：名字 → 体积 / 口型 → 可点提示，三行固定，卡片高度齐平 */}
        <div className="flex flex-1 flex-col gap-2 px-5 py-4">
          <span className="text-3xl font-bold leading-tight text-ink">
            {item.displayName}
          </span>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xl text-ink-subtle">约 {item.sizeMB} MB</span>
            {item.lipsync === "none" && (
              <span className="rounded-lg border border-line bg-surface-sunken px-2 py-1 text-lg text-ink-muted">
                不支持口型
              </span>
            )}
          </div>
          <span
            className={`mt-auto pt-1 text-xl font-semibold ${
              selected ? "text-ink-subtle" : "text-brand"
            }`}
          >
            {selected ? "已经是当前形象" : "点一下换上 →"}
          </span>
        </div>
      </button>
    </li>
  );
}

/** headerExtra：由主屏注入的常显操作（SOS），放在「返回」右侧，与主屏顶栏的最右位置一致。
 *  topBar：主屏注入的开发态任务切换（固定浮层，脱离文档流），保证换形象时入口不消失。 */
export function AvatarPicker({
  onDone,
  headerExtra,
  topBar,
}: {
  onDone: (changed: boolean) => void;
  headerExtra?: ReactNode;
  topBar?: ReactNode;
}) {
  // 进入页面时锁定「使用中」基准：点返回未换装则不触发重挂载
  const initialId = resolveSelectedAvatar(readStoredAvatarId()).id;

  return (
    <div className="flex h-full flex-col">
      {topBar}
      <header className="flex shrink-0 items-center justify-between px-12 pt-8">
        <div>
          <h1 className="text-5xl font-bold text-ink">换个形象</h1>
          <p className="mt-2 text-2xl text-ink-muted">
            点一下喜欢的形象，马上换好 · 共 {AVATAR_CATALOG.length} 位
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-5">
          <button
            type="button"
            onClick={() => onDone(false)}
            className="rounded-2xl border border-line bg-surface px-8 py-5 text-2xl text-ink-muted active:bg-surface-sunken"
          >
            返回
          </button>
          {headerExtra}
        </div>
      </header>

      <main className="min-h-0 flex-1 overflow-y-auto px-12 py-8">
        <ul className="grid grid-cols-2 gap-8 sm:grid-cols-3 lg:grid-cols-5">
          {AVATAR_CATALOG.map((item) => (
            <AvatarCard
              key={item.id}
              item={item}
              selected={item.id === initialId}
              onPick={() => {
                if (item.id === initialId) return; // 点「使用中」无动作
                writeStoredAvatarId(item.id);
                onDone(true);
              }}
            />
          ))}
        </ul>
      </main>
    </div>
  );
}
