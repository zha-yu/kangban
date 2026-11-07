/**
 * 3D 数字人舞台（Day 4 W1 建，Day 5 A1 S4 去 C7 改造）：圆框内的 VRM 渲染与多级降级
 * （PRD FR-3D-01/02）。模型来源为本地资产目录 avatar-catalog（localStorage 选择、
 * 出厂默认回落），不再消费 C7 `GET /api/pad/avatar-config`（留痕见
 * docs/team_tasks/day05/a1-本地VRM与形象选择页.md；接口本体保留但已停用）。
 * 降级链：VRM 加载失败/超 5s → 内置渐变占位（Q2 模型截图后补为静态头像层）→ 不白屏。
 * 诊断上行（C5）：vrm_load 失败经 sendDiag 发一次 client_diag（ok:false），成功不发。
 */
import { useEffect, useRef, useState } from "react";
import type { ClientDiagStage } from "../protocol";
import {
  IDLE_ANIMATION_URL,
  readStoredAvatarId,
  resolveSelectedAvatar,
  type AvatarCatalogItem,
} from "./avatar-catalog";
import { fetchVrmBytes, mountVrmScene, type VrmSceneHandle } from "./vrm-scene";

/** 舞台三相：模型加载中 ｜ 3D 就绪 ｜ 降级展示（渐变占位，Q2 后补静态头像层） */
type StagePhase = { kind: "loading" } | { kind: "ready" } | { kind: "fallback" };

export interface AvatarStageProps {
  /**
   * 形象注入（D-1：S5 选择页换装用）；不传则读 localStorage（pad.avatar.selected，
   * id 不合法回落出厂默认）。选择页未接入前父组件无需传。
   */
  avatar?: AvatarCatalogItem;
  /** C5 client_diag 上行；socket 未就绪时由 RealtimeClient 静默丢弃 */
  sendDiag: (stage: ClientDiagStage, ok: boolean, error: string) => void;
  /**
   * TTS 下行播放元素（口型 RMS 能量源，W1 第 5 条 + R-6）。
   * 可选：不传则无口型、其余功能不受影响；lipsync="none" 模型（R8-b）本组件内部跳过。
   */
  audioEl?: HTMLAudioElement | null;
  /**
   * 指针跟随开关（F-2a 决议：仅待机期启用，会话中恢复看镜头）。开启后在圆形舞台内
   * 移动鼠标/触摸拖动（F-3 调整为触摸也跟随）时眼睛+头部跟随。
   */
  pointerFollow?: boolean;
}

export function AvatarStage({ avatar, sendDiag, audioEl, pointerFollow = false }: AvatarStageProps) {
  // 模型解析：props 注入优先，否则 localStorage（D-1/D-3：进页面读一次，会话内不响应变化）
  const model = avatar ?? resolveSelectedAvatar(readStoredAvatarId());
  const [phase, setPhase] = useState<StagePhase>({ kind: "loading" });
  const canvasRef = useRef<HTMLCanvasElement>(null);
  /** 圆形舞台容器：指针跟随的命中区域（F-2 决议：仅圆形区域内生效） */
  const stageBoxRef = useRef<HTMLDivElement>(null);
  /** 本次挂载内 client_diag 只发一次（C5：仅日志；成功不发） */
  const diagSentRef = useRef(false);
  /** 口型用「最新 audioEl」引用：元素可能首帧后才挂载/换流，rAF 内每帧经访问器取最新 */
  const audioElRef = useRef<HTMLAudioElement | null>(null);
  audioElRef.current = audioEl ?? null;
  /** 场景句柄：指针跟随经它下发（模型重建后新句柄自动生效） */
  const sceneHandleRef = useRef<VrmSceneHandle | null>(null);

  useEffect(() => {
    let disposed = false;
    let handle: VrmSceneHandle | null = null;

    /** vrm_load 失败诊断：脱敏短码，整个挂载生命周期只发一次 */
    const failVrmLoad = (reason: string): void => {
      if (diagSentRef.current) return;
      diagSentRef.current = true;
      sendDiag("vrm_load", false, reason);
    };

    void (async () => {
      // 本地资产下载（多槽缓存命中则免网络）；失败/超 5s 直接降级，不白屏
      const buffer = await fetchVrmBytes(model.url);
      if (disposed) return;
      if (!buffer) {
        failVrmLoad("vrm_download_failed_or_timeout");
        setPhase({ kind: "fallback" });
        return;
      }

      const canvas = canvasRef.current;
      if (!canvas) return; // 卸载竞态防御
      try {
        handle = await mountVrmScene(
          canvas,
          buffer,
          model.rotationYOffset,
          // R8-b：无表情模型不接口型链路；其余每帧取最新元素（W1 第 5 条）
          model.lipsync === "expression" ? () => audioElRef.current : undefined,
          // Q3：待机动画循环叠加，加载失败静默回程序化待机
          { idleAnimationUrl: IDLE_ANIMATION_URL },
        );
        if (disposed) {
          handle.dispose();
          return;
        }
        sceneHandleRef.current = handle; // 指针跟随经此下发
        setPhase({ kind: "ready" }); // 3D 就绪，揭开降级层
      } catch {
        failVrmLoad("vrm_parse_or_render_failed");
        setPhase({ kind: "fallback" });
      }
    })();

    return () => {
      disposed = true;
      handle?.dispose();
      handle = null;
      sceneHandleRef.current = null;
    };
    // 仅在模型变化时重建场景（S5 换装走 props 注入触发）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [model.id]);

  // 指针跟随（F-1b/F-2a/F-3）：监听圆框容器，鼠标/触摸/触控笔均生效（触摸拖动也跟随）；
  // 圆形命中判定（容器 bounding box 四角不算），出圆即回落「看镜头」
  useEffect(() => {
    const el = stageBoxRef.current;
    if (!pointerFollow || !el) {
      sceneHandleRef.current?.setPointerTarget(null);
      return;
    }
    const applyPointer = (e: PointerEvent): void => {
      const rect = el.getBoundingClientRect();
      const nx = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      const ny = -(((e.clientY - rect.top) / rect.height) * 2 - 1);
      if (nx * nx + ny * ny > 1) {
        sceneHandleRef.current?.setPointerTarget(null); // 圆外四角：不跟随
        return;
      }
      sceneHandleRef.current?.setPointerTarget(nx, ny);
    };
    const onLeave = (): void => sceneHandleRef.current?.setPointerTarget(null);
    el.addEventListener("pointermove", applyPointer);
    el.addEventListener("pointerleave", onLeave);
    return () => {
      el.removeEventListener("pointermove", applyPointer);
      el.removeEventListener("pointerleave", onLeave);
      sceneHandleRef.current?.setPointerTarget(null);
    };
  }, [pointerFollow]);

  return (
    <div
      ref={stageBoxRef}
      className="relative flex items-center justify-center overflow-hidden rounded-full border border-brand-line bg-brand-soft"
      style={{ height: "min(560px, 48vh)", width: "min(560px, 48vh)" }}
    >
      {/* 3D 画布：就绪前被降级层覆盖，成功后揭开（透明底透出圆框底色） */}
      <canvas ref={canvasRef} className="h-full w-full" />

      {phase.kind !== "ready" && (
        <div className="absolute inset-0 flex items-center justify-center">
          {/* 内置兜底：纯样式渐变圆（Q2 截图就绪后在此之上加静态头像层） */}
          <div
            className="h-full w-full"
            style={{
              background:
                "radial-gradient(circle at 50% 38%, var(--color-accent-soft) 0%, var(--color-brand-soft) 58%, var(--color-brand-line) 100%)",
            }}
          />
        </div>
      )}

      {phase.kind === "loading" && (
        <span className="absolute bottom-10 text-xl text-ink-subtle">数字人加载中…</span>
      )}
    </div>
  );
}
