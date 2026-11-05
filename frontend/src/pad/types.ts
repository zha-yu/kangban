/** pad 全局共享类型 */

/** 状态条状态：三态（正在听/正在想/正在说）+ 连接态（连接中/已断开），口径见 adhoc §3.3 */
export type PadStatus =
  | "listening" // 正在听
  | "thinking" // 正在想
  | "speaking" // 正在说
  | "connecting" // 连接中
  | "disconnected"; // 已断开
