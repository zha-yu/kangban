import { useCallback, useEffect, useRef, useState } from "react";
import type { PadStatus } from "./types";
import type { SubtitleSource } from "./components/BigSubtitle";
import type { DeviceSession } from "./session";
import { sessionValid, loadSession, clearSession } from "./session";
import { StatusBar } from "./components/StatusBar";
import { BigSubtitle } from "./components/BigSubtitle";
import { StatusBadge } from "./components/StatusBadge";
import { SosButton } from "./components/SosButton";
import { HoldToTalk } from "./components/HoldToTalk";
import { TaskPanel } from "./components/TaskPanel";
import { ChatTranscript, type ChatMessage } from "./components/ChatTranscript";
import { LoginPage } from "./pages/LoginPage";
import { SosResult } from "./pages/SosResult";
import { DemoSwitcher } from "./pages/DemoSwitcher";
import type { TaskKind } from "./tasks";
import {
  TASK_HINTS,
  emitPadTask,
  makeDevTask,
  readDemoTask,
  subscribePadTasks,
  type PadTaskCommand,
  type PadTaskEvent,
} from "./task-channel";
import { RealtimeClient } from "./realtime";
import { MicCapture } from "./audio/capture";
import { loadKwsDetector, type KwsDetector } from "./kws/kws-detector";
import { AvatarStage } from "./avatar/AvatarStage";
import { AvatarPicker } from "./avatar/AvatarPicker";
import { getVoiceprintStatus } from "./voiceprint/voiceprint-client";
import { VoiceprintEnroll } from "./voiceprint/VoiceprintEnroll";
import "./index.css";

interface SubtitleState {
  text: string;
  source: SubtitleSource;
}

/** 开发验收直达：模块加载时读一次 ?demo=，仅 DEV 生效，不进真实演示链路 */
const INITIAL_DEMO_TASK = readDemoTask();

/** 主界面：两相轮转（Day 4 W2）——
 *  standby：待机唤醒期，KWS 端侧监听「小伴」（音频不出端）+ 呼叫按钮正交入口（FR-UI-09）；
 *  inSession：命中或点按钮后起 RealtimeClient 走既有链路；断连/连续重连失败自动回 standby。
 *  initialNotice：声纹状态查询失败但放行进对话时，大字幕先出的一行系统提示（C4 口径：查询失败不把长者挡在门外）。 */
function MainScreen({
  onLogout,
  initialNotice,
}: {
  onLogout: () => void;
  initialNotice?: string;
}) {
  const [phase, setPhase] = useState<"standby" | "inSession">("standby");
  const [status, setStatus] = useState<PadStatus>("disconnected");
  const [subtitle, setSubtitle] = useState<SubtitleState>({
    text: initialNotice ?? "",
    source: initialNotice ? "notice" : "final",
  });
  // 本次会话的完整对话：只累积整句（final / reply），前端内存保存；
  // 退出登录、刷新会随组件卸载清空，不落库、不加后端字段
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const nextMsgId = useRef(0);
  // R-5 可用性探测三态：loading（探测中）→ listening（唤醒词可用）/ unavailable（只留呼叫按钮）
  const [kwsState, setKwsState] = useState<"loading" | "listening" | "unavailable">("loading");
  const audioRef = useRef<HTMLAudioElement>(null);
  const clientRef = useRef<RealtimeClient | null>(null);
  const kwsRef = useRef<{ detector: KwsDetector; mic: MicCapture } | null>(null);
  // C-5：端侧 KWS 命中时刻（epoch 秒），随本轮 auth 首帧上报；呼叫按钮路径不填走后端兜底
  const wakeTsRef = useRef<number | undefined>(undefined);
  // S5 形象选择：pickerOpen 仅 standby 期可开（会话中禁止换装）；stageKey 变化触发
  // AvatarStage 重挂载读取新 localStorage（D-3 显式换装流程，不做热切换）
  const [pickerOpen, setPickerOpen] = useState(
    // 开发验收直达：?picker=1 跳过点「换个形象」，仅 DEV 生效，不进真实演示链路
    () =>
      import.meta.env.DEV &&
      new URLSearchParams(window.location.search).has("picker"),
  );
  const [stageKey, setStageKey] = useState(0);
  // FR-ALT-06：SOS 常显，结果态优先于形象选择页与任务面板；
  // 结果态只替换中部内容，顶栏按钮 / 大字幕 / 操作区高度都不动，位置永不移动。
  // 开发验收直达 ?sos=1（或 ?demo=sos），仅 DEV 生效，不进真实演示链路。
  const [sosOpen, setSosOpen] = useState(
    () =>
      import.meta.env.DEV &&
      (new URLSearchParams(window.location.search).has("sos") ||
        new URLSearchParams(window.location.search).get("demo") === "sos"),
  );

  // v1.5：任务命令（与传输解耦）。后端适配器落地后只需往任务总线 emit，
  // 主屏不需要知道是外呼、WS 降级帧还是调度器触发的。
  const [activeTask, setActiveTask] = useState<PadTaskCommand | null>(() =>
    INITIAL_DEMO_TASK ? makeDevTask(INITIAL_DEMO_TASK) : null,
  );
  const [taskHint, setTaskHint] = useState<string | null>(() =>
    INITIAL_DEMO_TASK ? TASK_HINTS[INITIAL_DEMO_TASK] : null,
  );

  // 唯一订阅点。open 时幂等（同 taskId 不重启面板）、close 时只对得上的任务生效；
  // 任务到达会退出形象选择页，SOS 结果态保持优先、不被任务挤掉。
  useEffect(
    () =>
      subscribePadTasks((event: PadTaskEvent) => {
        if (event.type === "open") {
          setActiveTask((prev) =>
            prev?.taskId === event.command.taskId ? prev : event.command,
          );
          setTaskHint(event.command.hint);
          setPickerOpen(false);
        } else {
          setActiveTask((prev) =>
            prev?.taskId === event.taskId ? null : prev,
          );
          setTaskHint(null);
        }
      }),
    [],
  );

  /** 真实 ASR / TTS 字幕到达时，任务提示让位，保证字幕始终与音频一致（FR-UI-02）。
   *  整句（final / reply）同时追加到对话记录；partial 中间态与 notice 系统提示
   *  只更新大字幕、不进记录，避免列表被过程文本刷屏。 */
  const pushSubtitle = useCallback((text: string, source: SubtitleSource) => {
    setTaskHint(null);
    setSubtitle({ text, source });
    if ((source === "final" || source === "reply") && text.trim()) {
      setMessages((prev) => [
        ...prev,
        {
          id: nextMsgId.current++,
          role: source === "final" ? "elder" : "companion",
          text,
        },
      ]);
    }
  }, []);

  const closeTask = useCallback(() => {
    if (!activeTask) return;
    emitPadTask({
      type: "close",
      taskId: activeTask.taskId,
      reason: "user_exit",
    });
  }, [activeTask]);

  /** 开发态切换条 = 任务下发适配器的可运行样例：点击即 emit 一条同形命令 */
  function handleSwitch(kind: TaskKind) {
    if (kind === "sos") {
      setSosOpen(true);
      return;
    }
    setSosOpen(false);
    if (kind === "none") {
      closeTask();
      return;
    }
    emitPadTask({ type: "open", command: makeDevTask(kind) });
  }

  /** 进入会话：停 KWS（关麦 + 释放回调，麦克风交给 RealtimeClient 自建实例），切换相位 */
  const beginSession = useCallback(() => {
    const k = kwsRef.current;
    kwsRef.current = null;
    if (k) {
      k.detector.close();
      void k.mic.stop();
      setKwsState("loading"); // 会话结束后回 standby 会重新探测
    }
    setPhase("inSession");
  }, []);

  /** 回到待机：停既有链路（effect 清理里 stop），重挂 KWS 监听 */
  const endSession = useCallback(() => {
    setPhase("standby");
    setStatus("disconnected");
  }, []);

  // standby 期：KWS 可用性探测 + 端侧监听（R-5：探测失败落呼叫按钮降级，不阻塞）
  useEffect(() => {
    if (phase !== "standby") return;
    let cancelled = false;
    void (async () => {
      const detector = await loadKwsDetector();
      if (cancelled) return;
      if (!detector) {
        setKwsState("unavailable");
        return;
      }
      const mic = new MicCapture(); // 独立实例：唤醒期与 RealtimeClient 的实例互不相干
      try {
        await mic.start();
      } catch {
        setKwsState("unavailable"); // 麦克风拿不到 → 按钮降级
        return;
      }
      if (cancelled) {
        void mic.stop(); // 双跑竞态守卫：cleanup 已跑则立即释放，防双麦
        return;
      }
      detector.onHit = () => {
        wakeTsRef.current = Date.now() / 1000; // C-5：命中时刻捕获（epoch 秒，N1 规范）
        beginSession();
      };
      mic.onFrame = (pcm) => detector.feed(pcm); // 仅本机推理，不出端（AGENTS 红线 10）
      kwsRef.current = { detector, mic };
      setKwsState("listening");
    })();
    return () => {
      cancelled = true;
    };
  }, [phase, beginSession]);

  // inSession 期：起 RealtimeClient（原主链路逻辑）
  useEffect(() => {
    if (phase !== "inSession") return;
    const session = loadSession();
    if (!session || !audioRef.current) {
      setPhase("standby");
      return;
    }
    const client = new RealtimeClient({
      onStatus: (s) => {
        setStatus(s);
        if (s === "disconnected") endSession(); // 会话结束/断连 → 回待机（呼叫按钮与唤醒词重新可用）
      },
      onSubtitle: pushSubtitle,
      onAuthFail: onLogout, // 会话过期/无效 → 回登录页（T-H 方案 A 定案）
      onNeedManualRetry: endSession, // 连续 5 次重连失败 → 同样回待机，按钮重新出现
      audioEl: audioRef.current,
    });
    clientRef.current = client;
    void client.start(session.access_token, wakeTsRef.current);
    wakeTsRef.current = undefined; // 消费一次即清：断线重连不重发旧 wake_ts

    // T-H 方案 A 定案（无 refresh 通道，过期回登录）：会话中途到期的主动守护——
    // 30s 周期 + 页面回前台时校验 expires_at，过期即清会话回登录页；
    // WS 4401/auth_fail 与 REST 401 由各自链路回调 onLogout 兜住
    const expiryGuard = setInterval(() => {
      if (!sessionValid()) onLogout();
    }, 30_000);
    const onVisible = () => {
      if (document.visibilityState === "visible" && !sessionValid()) onLogout();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(expiryGuard);
      document.removeEventListener("visibilitychange", onVisible);
      void client.stop();
      clientRef.current = null;
    };
    // onLogout/endSession 由调用方保证稳定；此处仅随相位启停
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase]);

  // 卸载兜底：停掉待机期的 KWS 麦克风
  useEffect(
    () => () => {
      const k = kwsRef.current;
      if (k) {
        k.detector.close();
        void k.mic.stop();
      }
    },
    [],
  );

  // S5：形象选择页（仅 standby 期渲染；此间 KWS 仍在监听，喊「小伴」会直接进会话
  // 并因 phase 切换自动离开选择页）。onDone(changed) 为真才重挂舞台，避免无谓重载。
  if (pickerOpen && phase === "standby" && !sosOpen && !activeTask) {
    return (
      <AvatarPicker
        // 与 MainScreen 顶栏同一个最右位置，换形象时 SOS 也不消失（FR-ALT-06 常显）
        headerExtra={<SosButton onClick={() => setSosOpen(true)} />}
        topBar={
          // 固定浮层，切换入口在换形象页也不消失；此分支恒为「主对话」
          <DemoSwitcher value="none" onChange={handleSwitch} />
        }
        onDone={(changed) => {
          setPickerOpen(false);
          if (changed) setStageKey((k) => k + 1);
        }}
      />
    );
  }

  const standbyHint =
    kwsState === "listening"
      ? "说“小伴”开始对话"
      : kwsState === "loading"
        ? "正在准备唤醒词…"
        : "点下方按钮开始对话"; // R-5：KWS 不可用只留按钮，不误导长者喊唤醒词

  // 大字幕：standby 默认显示唤醒引导；降级「按住说话」与声纹提示下发的文本优先显示，
  // 避免话术被引导语吞掉。两相位共用同一字幕区，位置不随对话状态跳动。
  // SOS / 任务态显示一行中性 UI 提示（notice，不是 TTS 字幕，不经过语音路由）；
  // 真实 ASR/TTS 到达后由 pushSubtitle 清掉任务提示，字幕重新与音频一致。
  const subtitleText = sosOpen
    ? "已按下求助按钮，请稍等"
    : taskHint ??
      (phase === "inSession" ? subtitle.text : subtitle.text || standbyHint);
  const subtitleSource: SubtitleSource =
    sosOpen || taskHint
      ? "notice"
      : phase === "inSession" || subtitle.text
        ? subtitle.source
        : "notice";

  const switcherValue: TaskKind = sosOpen ? "sos" : (activeTask?.kind ?? "none");
  // 中部形态优先级：SOS 结果态 > 任务面板 > 对话记录（仅 inSession）> 常态舞台
  const showTranscript = phase === "inSession" && !sosOpen && !activeTask;
  const twoColumn = !sosOpen && (!!activeTask || showTranscript);
  // 两栏态收紧横向间距、字幕降档、底部不再叠一个「呼叫小伴」大按钮
  const taskMode = !sosOpen && !!activeTask;
  const subtitleCompact = sosOpen || !!taskHint;

  // 舞台在任务 / 对话记录到达时收窄为左侧 300px 圆框；ResizeObserver 自适应，场景不重挂
  const stage = (
    <AvatarStage
      key={stageKey} // S5 显式换装：选择页确认后重挂，读最新 localStorage（D-3）
      compact={twoColumn}
      sendDiag={(s, ok, error) => clientRef.current?.sendDiag(s, ok, error)}
      audioEl={phase === "inSession" ? audioRef.current : null} // 仅会话期有 TTS 下行
      pointerFollow={phase === "standby"} // F-2a：仅待机期跟随，会话中恢复看镜头
    />
  );

  return (
    <div className="flex h-full flex-col">
      {/* 开发态任务切换：固定浮层，不占四段式高度；点击走任务总线而非直接改布局 */}
      <DemoSwitcher value={switcherValue} onChange={handleSwitch} />

      {/* TTS 下行播放（WebRTC 收轨）；传给 AvatarStage 做口型（P1） */}
      <audio ref={audioRef} />

      {/* 顶栏：状态条 + 退出 + SOS 常显（FR-ALT-06）。SOS 固定在最右 80px 高，
          相位切换、SOS 结果态、形象选择页都不改变它的位置 */}
      <header className="flex shrink-0 items-center justify-between gap-6 px-12 pt-8">
        <StatusBar status={status} />
        <div className="flex shrink-0 items-center gap-5">
          <button
            onClick={onLogout}
            className="rounded-2xl border border-line bg-surface px-5 py-4 text-xl text-ink-muted active:bg-surface-sunken"
          >
            退出登录
          </button>
          <SosButton active={sosOpen} onClick={() => setSosOpen(true)} />
        </div>
      </header>

      {/* 舞台独占剩余空间；任务到达时收为左舞台 + 右任务面板两栏，四段高度不变。
          字幕区与操作区高度固定，圆框不再被底部控件压到字幕上
          （旧版 standby 底部 4 层堆叠约 510px，挤掉 main 空间导致 60px 大字幕穿过人物）。
          两栏态顶部留 24px：右栏卡片不再从 main 顶边起、贴住页面顶栏；留白放在
          容器上而非卡片内，标题条仍从卡片圆角顶边起，不会出现切断圆角的白边 */}
      <main
        className={`flex min-h-0 flex-1 items-center justify-center overflow-hidden ${twoColumn ? "gap-8 px-8 pt-6" : "gap-16 px-12"}`}
      >
        {sosOpen ? (
          // 结果态只占中部：顶栏 SOS（active）、大字幕区、操作区高度都在原位
          <div className="flex h-full w-full items-center justify-center">
            <SosResult onExit={() => setSosOpen(false)} />
          </div>
        ) : activeTask ? (
          // 任务面板优先于对话记录；退出任务后若在会话中会自动回到记录
          <>
            {stage}
            <TaskPanel
              command={activeTask}
              onExit={closeTask}
              onHint={setTaskHint}
            />
          </>
        ) : showTranscript ? (
          // 对话中：左 3D 舞台 + 右可回看的完整对话记录
          <>
            {stage}
            <ChatTranscript messages={messages} />
          </>
        ) : (
          stage
        )}
      </main>

      {/* 大字幕固定区（FR-UI-02）：只显示当前这一句并与音频同步；固定高度 + 溢出裁切，
          长提示不再换行压到操作区。完整对话在中部右栏记录里翻看 */}
      <section className="flex h-[152px] shrink-0 items-center justify-center overflow-hidden">
        <BigSubtitle
          text={subtitleText}
          source={subtitleSource}
          compact={subtitleCompact}
        />
      </section>

      {/* 操作区高度恒定，字幕位置不跳。任务态收掉「呼叫小伴」大按钮——
          任务面板里已有「已服用 / 这次不吃」主操作，再叠一个主色大按钮既抢层级又挤占纵向空间；
          KWS 仍在监听，语音确认照常可用，降级入口「按住说话」保留。
          SOS 结果态时留空但高度不变，避免长者在求助后误触呼叫 */}
      <footer
        className={`flex min-h-[232px] shrink-0 flex-col items-center gap-6 pb-12 ${taskMode ? "justify-center" : "justify-start"}`}
      >
        {phase === "standby" && !sosOpen && taskMode && (
          <HoldToTalk
            compact
            token={loadSession()?.access_token ?? ""}
            onSubtitle={pushSubtitle}
            onAuthFail={onLogout}
          />
        )}
        {phase === "standby" && !sosOpen && !taskMode && (
          <>
            <button
              onClick={beginSession}
              className="rounded-2xl bg-brand px-20 py-6 text-4xl font-bold text-ink-inverse active:bg-brand-strong"
            >
              呼叫小伴
            </button>
            <div className="flex items-center justify-center gap-6">
              {/* T-G 降级入口：主链路未建立/已断开时按住说话走 C3 REST 整段上传 */}
              <HoldToTalk
                compact
                token={loadSession()?.access_token ?? ""}
                onSubtitle={pushSubtitle}
                onAuthFail={onLogout}
              />
              {/* S5 换装入口：仅 standby 期暴露（会话中禁止换装，计划口径 §3.2） */}
              <button
                type="button"
                onClick={() => setPickerOpen(true)}
                className="min-h-14 rounded-2xl border border-line bg-surface px-10 py-4 text-2xl font-semibold text-ink active:bg-surface-sunken"
              >
                换个形象
              </button>
            </div>
          </>
        )}
      </footer>
    </div>
  );
}

/**
 * 声纹门卫（C4 语义）：登录后、RealtimeClient 启动前先查 GET /api/voiceprints/status，
 * 三分支：409 device_unbound → 大字提示不进对话（未绑定长者不该起对话链路）；
 * none/partial → VoiceprintEnroll 录入引导，onDone 后进对话（3 段齐即 active，无需复查）；
 * active → 直接进对话。查询失败 / 网络异常不阻塞放行，大字幕出一行提示。
 */
type VoiceprintPhase = "checking" | "unbound" | "enroll" | "ready";

function VoiceprintGate({ onLogout }: { onLogout: () => void }) {
  const [phase, setPhase] = useState<VoiceprintPhase>("checking");
  const [notice, setNotice] = useState<string | null>(null);

  const check = useCallback(async () => {
    const session = loadSession();
    if (!session) {
      onLogout();
      return;
    }
    const result = await getVoiceprintStatus(session.access_token);
    if ("kind" in result) {
      if (result.kind === "auth") {
        onLogout(); // 会话失效 → 回登录
        return;
      }
      if (result.kind === "device_unbound") {
        setPhase("unbound");
        return;
      }
      // unavailable（503/网络/超时）：不把长者挡在门外，直接进对话 + 大字幕一行提示
      setNotice("声纹状态暂时没查到，先陪您聊天；需要录入时请稍后再试");
      setPhase("ready");
      return;
    }
    setNotice(null);
    setPhase(result.status === "active" ? "ready" : "enroll");
  }, [onLogout]);

  useEffect(() => {
    void check();
  }, [check]);

  if (phase === "checking") {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-6">
        <p className="text-5xl font-bold text-ink">正在准备…</p>
        <p className="text-2xl text-ink-muted">正在确认您的声纹状态</p>
      </div>
    );
  }

  if (phase === "unbound") {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-10 px-24 text-center">
        <StatusBadge tone="warning" size="lg">
          设备未绑定
        </StatusBadge>
        <p className="text-5xl font-bold leading-snug text-ink">
          设备还没有绑定长者，请在照护台完成绑定
        </p>
        <p className="text-2xl text-ink-muted">绑定完成后，点下面的按钮继续</p>
        <button
          type="button"
          onClick={() => void check()}
          className="h-24 rounded-2xl bg-brand px-16 text-3xl font-bold text-ink-inverse active:bg-brand-strong"
        >
          我已完成绑定，重新检查
        </button>
        <button
          type="button"
          onClick={onLogout}
          className="rounded-2xl border border-line bg-surface px-8 py-4 text-xl text-ink-muted active:bg-surface-sunken"
        >
          退出登录
        </button>
      </div>
    );
  }

  if (phase === "enroll") {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <VoiceprintEnroll
          token={loadSession()?.access_token ?? ""}
          onAuthFail={onLogout}
          onDone={() => setPhase("ready")}
        />
      </div>
    );
  }

  return <MainScreen onLogout={onLogout} initialNotice={notice ?? undefined} />;
}

export function App() {
  // 会话本体存 sessionStorage（session.ts）；组件只持有登录态
  const [loggedIn, setLoggedIn] = useState(() => sessionValid());

  function handleLoggedIn(_s: DeviceSession) {
    setLoggedIn(true);
  }

  function handleLogout() {
    clearSession();
    setLoggedIn(false);
  }

  if (!loggedIn) return <LoginPage onLoggedIn={handleLoggedIn} />;
  return <VoiceprintGate onLogout={handleLogout} />;
}
