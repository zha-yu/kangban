/**
 * 3D 数字人舞台（Day 4 W1）：圆框内的 VRM 渲染与多级降级（PRD FR-3D-01/02）。
 * 降级链（C7 语义约定）：VRM 加载失败/超 5s → fallback_image_url 静态头像 →
 * 内置渐变占位；设备未绑定（403 device_not_bound）→ 大字提示。任何失败不白屏。
 * 诊断上行（C5）：vrm_load 失败经 sendDiag 发一次 client_diag（ok:false），成功不发。
 */
import { useEffect, useRef, useState } from "react";
import type { ClientDiagStage } from "../protocol";
import { AvatarConfigError, fetchAvatarConfig, type AvatarConfig } from "./avatar-config";
import { fetchVrmBytes, mountVrmScene, type VrmSceneHandle } from "./vrm-scene";

/** 舞台四相：取配置/模型中 ｜ 设备未绑定 ｜ 3D 就绪 ｜ 降级展示（静态头像或渐变占位） */
type StagePhase =
  | { kind: "loading" }
  | { kind: "unbound" }
  | { kind: "ready" }
  | { kind: "fallback" };

export interface AvatarStageProps {
  /** 设备会话 access_token（E4 签发，C7 请求头用） */
  token: string;
  /** C5 client_diag 上行；socket 未就绪时由 RealtimeClient 静默丢弃 */
  sendDiag: (stage: ClientDiagStage, ok: boolean, error: string) => void;
  /** C7 401：设备会话过期，走既有回登录逻辑（T-H 方案 A） */
  onAuthFail: () => void;
  /**
   * TTS 下行播放元素（口型 RMS 能量源，W1 第 5 条 + R-6）。
   * 可选：不传则无口型、其余功能不受影响；App.tsx 接线由主线负责。
   */
  audioEl?: HTMLAudioElement | null;
}

export function AvatarStage({ token, sendDiag, onAuthFail, audioEl }: AvatarStageProps) {
  const [phase, setPhase] = useState<StagePhase>({ kind: "loading" });
  const [fallbackUrl, setFallbackUrl] = useState<string | null>(null);
  /** 静态头像自己也加载失败时再退一层：内置渐变占位 */
  const [imageBroken, setImageBroken] = useState(false);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  /** 本次挂载内 client_diag 只发一次（C5：仅日志；成功不发） */
  const diagSentRef = useRef(false);
  /** 口型用「最新 audioEl」引用：元素可能首帧后才挂载/换流，rAF 内每帧经访问器取最新 */
  const audioElRef = useRef<HTMLAudioElement | null>(null);
  audioElRef.current = audioEl ?? null;

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
      let config: AvatarConfig | null;
      try {
        config = await fetchAvatarConfig(token, onAuthFail);
      } catch (err) {
        if (disposed) return;
        if (err instanceof AvatarConfigError) {
          failVrmLoad(err.code);
          setPhase({ kind: "unbound" });
          return;
        }
        throw err; // 未知错误不吞：交给上层错误边界，避免静默
      }
      if (disposed) return;

      setFallbackUrl(config?.fallback_image_url ?? null);
      setImageBroken(false);
      if (!config?.avatar_url) {
        // C7：avatar_url 为空 = 后端未配形象，属正常降级（内置兜底），不打失败诊断
        setPhase({ kind: "fallback" });
        return;
      }

      // 下载期间静态头像已先行展示（若有）；缓存键 = version（C7 v1.1）
      const buffer = await fetchVrmBytes(config.avatar_url, config.version);
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
          config.rotation_y_offset,
          () => audioElRef.current, // 口型音频源：每帧取最新元素（W1 第 5 条）
        );
        if (disposed) {
          handle.dispose();
          return;
        }
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
    };
    // token 在 MainScreen 生命周期内不变；仅在挂载时执行一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  return (
    <div
      className="relative flex items-center justify-center overflow-hidden rounded-full border border-brand-line bg-brand-soft"
      style={{ height: "min(560px, 48vh)", width: "min(560px, 48vh)" }}
    >
      {/* 3D 画布：就绪前被降级层覆盖，成功后揭开（透明底透出圆框底色） */}
      <canvas ref={canvasRef} className="h-full w-full" />

      {phase.kind !== "ready" && (
        <div className="absolute inset-0 flex items-center justify-center">
          {phase.kind === "unbound" ? (
            /* 403 device_not_bound：需人工到照护台重绑，与「没配形象」分开提示 */
            <span className="px-12 text-center text-4xl text-ink-muted">
              设备还没有绑定长者，请在照护台完成绑定
            </span>
          ) : fallbackUrl && !imageBroken ? (
            <img
              src={fallbackUrl}
              alt="数字人头像"
              className="h-full w-full object-contain"
              onError={() => setImageBroken(true)}
            />
          ) : (
            /* 内置兜底（C7 取值链第三层）：纯样式渐变圆 */
            <div
              className="h-full w-full"
              style={{
                background:
                  "radial-gradient(circle at 50% 38%, var(--color-accent-soft) 0%, var(--color-brand-soft) 58%, var(--color-brand-line) 100%)",
              }}
            />
          )}
        </div>
      )}

      {phase.kind === "loading" && (
        <span className="absolute bottom-10 text-xl text-ink-subtle">数字人加载中…</span>
      )}
    </div>
  );
}
