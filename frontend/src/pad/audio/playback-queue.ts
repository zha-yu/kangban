/**
 * Day 6 · N16 分段播放队列与送达确认（契约来源：docs/team_tasks/day06/contracts.md v1.2 §4）。
 *
 * 口径（主计划 Q-2 裁决「下一段到达即结算」）：
 *  - TTS 音频走 WebRTC 单轨连续流，服务端按段顺序下发段音频，并在每段音频开始
 *    流送时发对应 tts_text。前端无法从单轨流上判定单段 onended，因此：
 *      收到段 N 的 tts_text → 段 N 上报 playing、段 N-1 上报 played（此时段 N-1 的
 *      音频已被服务端播完）；
 *      末段 played 由轨结束（audioEl ended）触发（markTrackEnded）；
 *      打断 → 未结算段上报 canceled，带最后已确认 played 的 segment_id。
 *  - epoch 是服务端下发的会话代次，客户端原样回传、不得自增；epoch < 已见最大值
 *    的段直接丢弃（stale_segment_dropped）——「不串音」的前端最后一道闸。
 *  - ack 尽力而为：发送失败最多重发 1 次，失败只打 client_diag，绝不阻塞播放。
 *
 * J3 已对齐口径（dev-C 确认，主计划 §11.4 转达件③ 2026-11-09）：
 *  ① played = 该段真正播完再报，是唯一推进水位的信号；playing 只用于首段开播延迟
 *    指标，服务端不推进水位（本实现每段都发 playing，兼容）；
 *  ② canceled：打断时上报，segment_id 填「最后一个已播完的段」，一段都没播完填 -1；
 *  唯一纪律：不得把 played 提前到「开始播」就报——本实现仅在「下一段到达（上一段
 *  音频已被服务端顺序播完）」或「轨结束」时报 played，符合纪律。
 *
 * J3 末段 played（contracts v1.2 §4.2 补充口径，2026-11-09 主理人确认）：
 *  以客户端 played ack 为准，response_completed 只由末段 played 触发。轨 ended 不可靠
 *  时 v1.2 允许「该段合成音频时长 + at_ms 计时」兜底——实测（2026-11-09 S4 试跑）
 *  WebRTC 单轨连续流上 audioEl ended 永不触发，客户端无真实时长来源（tts_text v2
 *  字段零增删），故按段文本长度估算时长起兜底计时器（350ms/字 + 1.5s 底数，宁晚勿早：
 *  早报会让麦克风在音频播完前放开引发回声抢话），到点仍未被新段/打断/轨结束收口时
 *  结算末段 played 并复位响应。dev-C 给出真实时长口径后可替换估算系数。
 *
 * Day 7 D1 可靠性修复（docs/plans/day07/d-推进计划-pad可靠性改绑联调.md S1.1）：
 *  ① isSegmentedTts 增加 epoch 类型校验（P3-36）：epoch 缺失/非法不进分段路径，
 *    不污染 maxEpoch；② 暴露 isStale() 供调用方在 unmute/play/置 speaking **之前**
 *    做淘汰判定（P2-22，被淘汰段不播不 ack 不改 ttsPlaying）；③ total 缺失按单段
 *    收口（P2-24）：totalSegments = total ?? segment_id+1，起 tail 兜底并发
 *    total_missing_single_segment 诊断；④ flushCanceled/markTrackEnded 内
 *    cancelRetry()，防打断/收口后旧 playing ack 乱序补发（P3-36）；⑤ playing ack
 *    延后到实际起播（P3-39）：段到达只记 pending，调用方在音频真正起播后调
 *    notifyPlaybackStarted() 补发（J3：playing 只作首段开播延迟指标，不推水位）；
 *  ⑥ tail 兜底结算时打 tail_fallback_estimated 诊断，估算口径不静默。
 */

import type { SegmentAckMsg, TtsTextMsg } from "../protocol";

/** 分段 tts_text 的收窄类型：三字段齐备且类型合法 */
export type SegmentedTtsTextMsg = TtsTextMsg & {
  response_id: string;
  segment_id: number;
  epoch: number;
};

/** 段消息判定（Day 7 D1 关 P3-36）：response_id / segment_id / epoch 任一缺失或
 *  类型非法 → 整段一播（旧服务端/回滚路径）；epoch 非法不进分段路径、不污染 maxEpoch */
export function isSegmentedTts(msg: TtsTextMsg): msg is SegmentedTtsTextMsg {
  return (
    typeof msg.response_id === "string" &&
    typeof msg.segment_id === "number" &&
    typeof msg.epoch === "number"
  );
}

export interface PlaybackQueueDeps {
  /** 发送一条 ack；返回 false 表示当前未能发出（WS 未就绪），队列会安排 1 次重发 */
  sendAck: (msg: SegmentAckMsg) => boolean;
  /** 客户端诊断上行（ack 发送失败等，仅日志） */
  sendDiag: (ok: boolean, error: string) => void;
  /** 响应收口（末段 played 结算/轨结束）回调：调用方据此复位播放态与状态条 */
  onResponseSettled?: () => void;
}

const ACK_RETRY_DELAY_MS = 500;
/** 末段兜底计时：350ms/字 + 1.5s 底数（估算宁晚勿早，见文件头 J3 说明） */
const TAIL_FALLBACK_MS_PER_CHAR = 350;
const TAIL_FALLBACK_BASE_MS = 1500;

/** 响应内已结算/待结算状态（一次回复 = 一个 response_id + 多段） */
interface ActiveResponse {
  responseId: string;
  epoch: number;
  /** 最近一次上报 playing 的段号（当前在播段） */
  playingSegment: number;
  /** 最后一段号（total 缺失时为 null，末段结算只能靠轨结束） */
  totalSegments: number | null;
  /** 最后已上报 played 的段号；null = 尚无已确认段 */
  lastConfirmedPlayed: number | null;
}

export class PlaybackQueue {
  private active: ActiveResponse | null = null;
  /** 已见最大 epoch（跨代淘汰依据） */
  private maxEpoch: number | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  /** 重发等待中的 ack（每时刻至多一条：ack 按段单调，失败重发期间新 ack 不产生——新段未到） */
  private pendingAck: SegmentAckMsg | null = null;
  /** 末段兜底计时器（轨 ended 不可靠时的结算兜底，见文件头 J3 说明） */
  private tailTimer: ReturnType<typeof setTimeout> | null = null;
  /** 待补发的 playing ack 段号（P3-39：段到达不立即发，实际起播后由
   *  notifyPlaybackStarted() 补发；至多保留最新一条——中间段的 playing 仅作
   *  兼容指标，被新段覆盖可接受，played 才是唯一推水位的信号） */
  private pendingPlayingSegment: number | null = null;

  constructor(private deps: PlaybackQueueDeps) {}

  /**
   * 收到一条分段 tts_text：结算上一段 played、本段记 pending playing
   * （实际起播后由 notifyPlaybackStarted() 补发，P3-39）。
   * 返回 false 表示该段因旧 epoch 被丢弃（调用方无需再处理字幕以外的逻辑，字幕照常渲染）。
   */
  handleSegment(msg: TtsTextMsg): boolean {
    if (!isSegmentedTts(msg)) return false;
    const { epoch, segment_id: segmentId } = msg;

    // 旧 epoch：直接丢弃，不播不 ack（字幕由调用方决定是否渲染）
    if (this.maxEpoch !== null && epoch < this.maxEpoch) {
      this.deps.sendDiag(false, `stale_segment_dropped epoch=${epoch} segment=${segmentId}`);
      return false;
    }
    this.maxEpoch = epoch;
    this.cancelTail(); // 新段到达：旧兜底计时一律作废，按最新状态重估

    // 同/新代次内出现另一条 response：旧回复视为被替代，按打断收口
    if (this.active && this.active.responseId !== msg.response_id) {
      this.flushCanceled();
    }

    if (!this.active) {
      // Day 7 D1 关 P2-24：total 缺失按单段收口（total = segment_id+1），
      // 起本段兜底计时并打诊断——不再出现「末段判定恒跳过、无兜底」的悬挂态
      const totalFallback = msg.total === undefined;
      if (totalFallback) {
        this.deps.sendDiag(false, `total_missing_single_segment segment=${segmentId}`);
      }
      this.active = {
        responseId: msg.response_id,
        epoch,
        playingSegment: segmentId,
        totalSegments: msg.total ?? segmentId + 1,
        lastConfirmedPlayed: null,
      };
    } else {
      // 同一 response 的下一段：上一段结算为 played（Q-2 口径）
      this.settlePlayed(this.active.playingSegment);
      this.active.playingSegment = segmentId;
      if (msg.total !== undefined) this.active.totalSegments = msg.total;
    }
    // Day 7 D1 关 P3-39：段到达不立即发 playing（早于实际起播），只记 pending，
    // 调用方在音频真正起播后调 notifyPlaybackStarted() 补发
    this.pendingPlayingSegment = segmentId;
    // 末段（total 已知且本段是最后一段）：起兜底计时，轨 ended 不触发也能收口
    if (this.active.totalSegments !== null && segmentId >= this.active.totalSegments - 1) {
      this.scheduleTailFallback(msg.text.length);
    }
    return true;
  }

  /**
   * Day 7 D1 关 P2-22：淘汰判定（纯查询，无副作用）。
   * 调用方须在 unmute / play() / 置 speaking **之前**调用——被淘汰段
   * （旧 epoch）不播、不 ack、不改 ttsPlaying。
   */
  isStale(msg: TtsTextMsg): boolean {
    if (!isSegmentedTts(msg)) return false;
    return this.maxEpoch !== null && msg.epoch < this.maxEpoch;
  }

  /** 当前活跃 response_id（Day 7 D1：打断上行 interrupt 消息取值来源；无活跃回复为 null） */
  get activeResponseId(): string | null {
    return this.active?.responseId ?? null;
  }

  /**
   * Day 7 D1 关 P3-39：调用方在音频实际起播（play() 成功 / playing 事件）后调用，
   * 补发 pending 的 playing ack。无 pending / 无活跃响应时为 no-op。
   */
  notifyPlaybackStarted(): void {
    const segmentId = this.pendingPlayingSegment;
    this.pendingPlayingSegment = null;
    if (segmentId === null || !this.active) return;
    this.sendAckFor(segmentId, "playing");
  }

  /**
   * 轨结束（audioEl ended）：末段结算为 played 并复位响应状态。
   * 无活跃响应（整段模式或已收口）时为 no-op。
   */
  markTrackEnded(): void {
    this.cancelTail();
    // Day 7 D1 关 P3-36：收口即作废重发与 pending playing，防旧 ack 乱序补发
    this.cancelRetry();
    this.pendingPlayingSegment = null;
    if (!this.active) return;
    this.settlePlayed(this.active.playingSegment);
    this.active = null;
    this.deps.onResponseSettled?.();
  }

  /**
   * 打断收口：当前未结算段与队列剩余全部作废，上报 canceled。
   * segment_id 填「最后一个已播完的段」；一段都没播完填 -1（dev-C 口径确认，
   * 主计划 §11.4 转达件③ 2026-11-09——J3 开放点②就此落定）。
   */
  interrupt(): void {
    this.flushCanceled();
  }

  /** 会话拆线等场景：清空全部状态，不发任何 ack */
  reset(): void {
    this.cancelTail();
    this.active = null;
    this.maxEpoch = null;
    this.pendingPlayingSegment = null;
    this.cancelRetry();
  }

  /**
   * 末段兜底计时（J3：轨 ended 在 WebRTC 单轨连续流上不可靠，v1.2 允许计时兜底）。
   * 到点时若响应未被新段/打断/轨结束收口，按轨结束同路径结算；
   * Day 7 D1 修复⑥：结算前打 tail_fallback_estimated 诊断，估算口径不静默。
   */
  private scheduleTailFallback(textLength: number): void {
    this.cancelTail();
    this.tailTimer = setTimeout(() => {
      this.tailTimer = null;
      this.deps.sendDiag(false, "tail_fallback_estimated");
      this.markTrackEnded();
    }, TAIL_FALLBACK_BASE_MS + textLength * TAIL_FALLBACK_MS_PER_CHAR);
  }

  private cancelTail(): void {
    if (this.tailTimer !== null) {
      clearTimeout(this.tailTimer);
      this.tailTimer = null;
    }
  }

  /** 上报 played（幂等：已确认过的段不再重复上报） */
  private settlePlayed(segmentId: number): void {
    if (!this.active) return;
    if (this.active.lastConfirmedPlayed !== null && segmentId <= this.active.lastConfirmedPlayed) return;
    this.active.lastConfirmedPlayed = segmentId;
    this.sendAckFor(segmentId, "played");
  }

  private flushCanceled(): void {
    if (!this.active) return;
    this.cancelTail(); // 打断收口优先于兜底计时
    // Day 7 D1 关 P3-36：打断后旧 playing ack 不再补发（含重发计时器），防乱序
    this.cancelRetry();
    this.pendingPlayingSegment = null;
    const { responseId, epoch, lastConfirmedPlayed } = this.active;
    this.active = null;
    this.deliverAck({
      type: "segment_ack",
      response_id: responseId,
      epoch,
      // 无任何已播完段时填 -1（dev-C 口径：服务端据此识别「首段都没听完就被打断」）
      segment_id: lastConfirmedPlayed ?? -1,
      status: "canceled",
      at_ms: Math.round(performance.now()),
    });
  }

  private sendAckFor(segmentId: number, status: "playing" | "played"): void {
    if (!this.active) return;
    this.deliverAck({
      type: "segment_ack",
      response_id: this.active.responseId,
      epoch: this.active.epoch,
      segment_id: segmentId,
      status,
      at_ms: Math.round(performance.now()),
    });
  }

  /** 尽力而为：发出失败 → 安排 1 次重发；重发仍失败 → client_diag，不阻塞播放 */
  private deliverAck(msg: SegmentAckMsg): void {
    if (this.deps.sendAck(msg)) return;
    this.pendingAck = msg;
    this.cancelRetry();
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      const pending = this.pendingAck;
      this.pendingAck = null;
      if (!pending) return;
      if (!this.deps.sendAck(pending)) {
        this.deps.sendDiag(false, `segment_ack_send_failed status=${pending.status} segment=${pending.segment_id}`);
      }
    }, ACK_RETRY_DELAY_MS);
  }

  private cancelRetry(): void {
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.pendingAck = null;
  }
}
