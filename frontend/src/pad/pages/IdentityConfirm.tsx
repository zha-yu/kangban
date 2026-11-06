import { useState } from "react";
import { StatusBadge } from "../components/StatusBadge";
import { TaskShell } from "../components/TaskShell";
import { MOCK_CANDIDATES, type IdentityCandidate } from "../tasks";

/**
 * 身份主动确认与通用模式（PRD §3.2、FR-VPR-02/03）。
 *
 * 声纹低置信度或声纹服务不可用时，页面主动询问「请问是哪位在说话？」，
 * 由长者点选或语音回答；候选集只含设备绑定的房间长者。
 * 仍无法确定 → 通用模式：不读个人记忆、不写健康记录、不确认用药。
 */
type Mode = "ask" | "confirmed" | "general";

interface IdentityConfirmProps {
  onExit: () => void;
  onSubtitle?: (text: string) => void;
}

const GENERAL_LIMITS = [
  "不会读取您的个人记忆",
  "不会写入健康记录",
  "不会替您确认用药",
];

export function IdentityConfirm({ onExit, onSubtitle }: IdentityConfirmProps) {
  const [mode, setMode] = useState<Mode>("ask");
  const [selected, setSelected] = useState<IdentityCandidate | null>(null);

  function handleSelect(person: IdentityCandidate) {
    setSelected(person);
    setMode("confirmed");
    onSubtitle?.(`好的，${person.name}阿姨，我认出您了`);
  }

  if (mode === "general") {
    return (
      <TaskShell
        eyebrow="身份确认"
        title="已进入通用模式"
        badge={
          <StatusBadge tone="neutral" size="md">
            通用模式
          </StatusBadge>
        }
        footer={
          <button
            type="button"
            onClick={() => setMode("ask")}
            className="h-24 w-full rounded-2xl bg-brand text-3xl font-bold text-ink-inverse active:bg-brand-strong"
          >
            重新确认身份
          </button>
        }
      >
        <p className="text-2xl leading-relaxed text-ink-muted">
          还没有确定是谁在说话。为了不弄错您的信息，现在先按通用方式回答：
        </p>
        <ul className="mt-8 flex flex-col gap-4">
          {GENERAL_LIMITS.map((text, i) => (
            <li
              key={text}
              className="flex items-center gap-5 rounded-2xl border border-line bg-surface-sunken px-7 py-6"
            >
              <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full border border-brand-line bg-brand-soft text-2xl font-semibold text-brand">
                {i + 1}
              </span>
              <span className="text-3xl text-ink">{text}</span>
            </li>
          ))}
        </ul>
        <p className="mt-8 text-xl text-ink-muted">
          需要查用药、记健康数据时，请点下方重新确认身份。
        </p>
      </TaskShell>
    );
  }

  if (mode === "confirmed" && selected) {
    return (
      <TaskShell
        eyebrow="身份确认"
        title={`您好，${selected.name}`}
        badge={
          <StatusBadge tone="success" size="md">
            身份已确认
          </StatusBadge>
        }
        footer={
          <button
            type="button"
            onClick={onExit}
            className="h-24 w-full rounded-2xl bg-brand text-3xl font-bold text-ink-inverse active:bg-brand-strong"
          >
            开始对话
          </button>
        }
      >
        <div className="flex items-center gap-8 rounded-2xl border border-line bg-surface-sunken p-8">
          <span className="flex h-28 w-28 shrink-0 items-center justify-center rounded-full border border-brand-line bg-brand-soft text-5xl font-bold text-brand">
            {selected.name.slice(0, 1)}
          </span>
          <div className="flex flex-col gap-3">
            <span className="text-4xl font-semibold text-ink">{selected.name}</span>
            <span className="text-2xl text-ink-muted">{selected.bed}</span>
          </div>
        </div>
        <p className="mt-8 text-2xl text-ink-muted">
          接下来可以聊天气、听戏，也可以确认用药和记录健康数据。
        </p>
      </TaskShell>
    );
  }

  return (
    <TaskShell
      eyebrow="身份确认"
      title="请问是哪位在说话？"
      badge={
        <StatusBadge tone="info" size="md">
          需要您确认一下
        </StatusBadge>
      }
      onExit={onExit}
      exitLabel="稍后再说"
      footer={
        <p className="text-xl text-ink-muted">
          声纹服务暂不可用时会显示本页，系统不会替您猜是谁。
        </p>
      }
    >
      <p className="text-2xl text-ink-muted">
        请点一下您的名字，或者直接说出名字。
      </p>
      <div className="mt-8 grid grid-cols-2 gap-5">
        {MOCK_CANDIDATES.map((person) => (
          <button
            key={person.id}
            type="button"
            onClick={() => handleSelect(person)}
            className="flex h-40 items-center gap-6 rounded-2xl border border-line bg-surface p-6 text-left active:bg-surface-sunken"
          >
            <span className="flex h-24 w-24 shrink-0 items-center justify-center rounded-full border border-brand-line bg-brand-soft text-4xl font-bold text-brand">
              {person.name.slice(0, 1)}
            </span>
            <span className="flex flex-col gap-2">
              <span className="text-4xl font-bold text-ink">{person.name}</span>
              <span className="text-xl text-ink-muted">{person.bed}</span>
            </span>
          </button>
        ))}
      </div>
      <button
        type="button"
        onClick={() => setMode("general")}
        className="mt-6 h-20 w-full rounded-2xl border border-line bg-surface text-2xl font-semibold text-ink-muted active:bg-surface-sunken"
      >
        都不是 / 先用通用模式
      </button>
    </TaskShell>
  );
}
