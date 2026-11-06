import { useState } from "react";
import { ApiError, login, type Principal } from "../api";

/**
 * 家属端登录页（task-05 F2，PRD §3.1 账号密码登录）。
 *
 * 375px 移动优先单列布局（前端页面方案 §3.3：无横向滚动、正文 ≥16px、触控 ≥48px）。
 * 校验规则与员工端一致（卡 §3 要点 4）：手机号 11 位且 1 开头、密码 ≥6 位，
 * 失焦触发校验；提交中按钮 loading + disabled 防重复提交。
 * 错误文案固定（401「手机号或密码不正确」/ 网络「网络异常，请稍后重试」），
 * 禁止回显 trace_id 与内部细节。
 * 本模块与 console/pages/LoginPage.tsx 为独立同构实现（卡 §0 允许重复，禁止互相 import）。
 *
 * 2026-11-06 主理人裁决：console/family 不用 MUI（原选型与技术文档 §1、
 * 前端页面方案 §5 的出入由主理人走基线文档修订），本页为纯 React + 原生 CSS 实现。
 */

const PHONE_PATTERN = /^1\d{10}$/;

/** 密码可见性切换图标（内联 SVG） */
function EyeIcon({ off }: { off?: boolean }) {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"
        stroke="currentColor"
        strokeWidth="1.6"
      />
      <circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.6" />
      {off && <path d="M4 20 20 4" stroke="currentColor" strokeWidth="1.6" />}
    </svg>
  );
}

interface LoginPageProps {
  onSuccess: (principal: Principal) => void;
}

export function LoginPage({ onSuccess }: LoginPageProps) {
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [phoneTouched, setPhoneTouched] = useState(false);
  const [passwordTouched, setPasswordTouched] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);

  const phoneValid = PHONE_PATTERN.test(phone);
  const passwordValid = password.length >= 6;
  const phoneError = phoneTouched && !phoneValid;
  const passwordError = passwordTouched && !passwordValid;
  const canSubmit = phoneValid && passwordValid && !loading;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setLoading(true);
    setErrorText(null);
    try {
      const principal = await login(phone, password);
      onSuccess(principal);
    } catch (err) {
      setErrorText(err instanceof ApiError ? err.message : "网络异常，请稍后重试");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="auth-page">
      <div className="auth-card">
        <h1 className="auth-title">康伴 · 家属端</h1>
        <p className="auth-subtitle">请使用注册手机号登录，查看家人的每日照护日报</p>

        <form className="auth-form" onSubmit={handleSubmit} noValidate>
          <div className="field">
            <label htmlFor="family-phone">手机号</label>
            <input
              id="family-phone"
              className="input"
              type="tel"
              autoComplete="tel"
              inputMode="numeric"
              value={phone}
              onChange={(e) => setPhone(e.target.value.trim())}
              onBlur={() => setPhoneTouched(true)}
              aria-invalid={phoneError || undefined}
              aria-describedby={phoneError ? "family-phone-error" : undefined}
            />
            {phoneError && (
              <p className="helper-error" id="family-phone-error">
                请输入 11 位、以 1 开头的手机号
              </p>
            )}
          </div>

          <div className="field">
            <label htmlFor="family-password">密码</label>
            <div className="input-wrap">
              <input
                id="family-password"
                className="input"
                type={showPassword ? "text" : "password"}
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                onBlur={() => setPasswordTouched(true)}
                aria-invalid={passwordError || undefined}
                aria-describedby={passwordError ? "family-password-error" : undefined}
              />
              <button
                type="button"
                className="icon-btn"
                aria-label={showPassword ? "隐藏密码" : "显示密码"}
                onClick={() => setShowPassword((v) => !v)}
              >
                <EyeIcon off={showPassword} />
              </button>
            </div>
            {passwordError && (
              <p className="helper-error" id="family-password-error">
                密码至少 6 位
              </p>
            )}
          </div>

          {errorText && (
            <div className="alert-danger" role="alert" aria-live="polite">
              {errorText}
            </div>
          )}

          <button
            type="submit"
            className="btn-primary"
            disabled={!canSubmit}
            aria-busy={loading || undefined}
          >
            {loading && <span className="spinner spinner-inverse" aria-hidden="true" />}
            {loading ? "正在登录…" : "登录"}
          </button>
        </form>
      </div>
    </div>
  );
}
