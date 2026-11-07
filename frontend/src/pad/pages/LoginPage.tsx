import { useEffect, useRef, useState } from "react";
import {
  MOCK_ENABLED,
  deviceLogin,
  loginErrorText,
  mockDeviceLogin,
  LoginError,
  type LoginErrorCode,
} from "../api";
import { saveSession, type DeviceSession } from "../session";

const RATE_LIMIT_SECONDS = 60;

interface LoginPageProps {
  onLoggedIn: (session: DeviceSession) => void;
}

/** T-B 登录页：大字表单（FR-UI-02 触控区 ≥48px 正文 ≥20px），粘贴 device_id + device_secret 换证 */
export function LoginPage({ onLoggedIn }: LoginPageProps) {
  const [deviceId, setDeviceId] = useState("");
  const [deviceSecret, setDeviceSecret] = useState("");
  const [error, setError] = useState<LoginErrorCode | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [countdown, setCountdown] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 429 倒计时：到 0 解锁
  useEffect(() => {
    if (countdown <= 0) return;
    timerRef.current = setInterval(
      () => setCountdown((c) => Math.max(0, c - 1)),
      1000,
    );
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [countdown > 0]);

  const locked = countdown > 0;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || locked) return;
    if (!deviceId.trim() || !deviceSecret.trim()) {
      setError("unauthorized"); // 空输入统一按「请检查输入」话术提示
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const session = MOCK_ENABLED
        ? await mockDeviceLogin(deviceId, deviceSecret)
        : await deviceLogin(deviceId.trim(), deviceSecret);
      saveSession(session);
      onLoggedIn(session);
    } catch (err) {
      if (err instanceof LoginError) {
        setError(err.code);
        if (err.code === "rate_limited") setCountdown(RATE_LIMIT_SECONDS);
      } else {
        setError("network");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="mx-auto flex h-full w-[720px] flex-col justify-center gap-8"
    >
      <h1 className="text-center text-6xl font-bold text-brand">康伴</h1>
      <p className="text-center text-2xl text-ink-muted">
        请输入设备编号与设备密钥登录
      </p>

      <label className="flex flex-col gap-2">
        <span className="text-2xl">设备编号</span>
        <input
          value={deviceId}
          onChange={(e) => setDeviceId(e.target.value)}
          disabled={locked}
          className="h-16 rounded-2xl border border-line bg-surface px-6 text-3xl text-ink outline-none placeholder:text-ink-subtle focus:ring-2 focus:ring-brand disabled:opacity-50"
        />
      </label>

      <label className="flex flex-col gap-2">
        <span className="text-2xl">设备密钥</span>
        <input
          type="password"
          value={deviceSecret}
          onChange={(e) => setDeviceSecret(e.target.value)}
          disabled={locked}
          className="h-16 rounded-2xl border border-line bg-surface px-6 text-3xl text-ink outline-none placeholder:text-ink-subtle focus:ring-2 focus:ring-brand disabled:opacity-50"
        />
      </label>

      {error && (
        <p role="alert" className="text-2xl font-semibold text-danger-ink">
          {loginErrorText(error)}
        </p>
      )}

      <button
        type="submit"
        disabled={submitting || locked}
        className="h-20 rounded-2xl bg-brand text-3xl font-bold text-ink-inverse active:bg-brand-strong disabled:opacity-50"
      >
        {locked ? `请等待 ${countdown} 秒后重试` : submitting ? "登录中…" : "登录"}
      </button>

      {MOCK_ENABLED && (
        <p className="text-center text-xl text-ink-subtle">
          mock 模式：密钥输 401 / 429 触发对应错误，其余成功
        </p>
      )}
    </form>
  );
}
