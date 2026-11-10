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
 */

import type { SegmentAckMsg, TtsTextMsg } from "../protocol";

/** 段消息判定：response_id 与 segment_id 任一缺失 → 整段一播（旧服务端/回滚路径） */
export function isSegmentedTts(msg: TtsTextMsg): boolean {
  return msg.response_id !== undefined && msg.segment_id !== undefined;
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

  constructor(private deps: PlaybackQueueDeps) {}

  /**
   * 收到一条分段 tts_text：结算上一段 played、本段上报 playing。
   * 返回 false 表示该段因旧 epoch 被丢弃（调用方无需再处理字幕以外的逻辑，字幕照常渲染）。
   */
  handleSegment(msg: TtsTextMsg): boolean {
    if (!isSegmentedTts(msg)) return false;
    const epoch = msg.epoch as number;
    const segmentId = msg.segment_id as number;

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
      this.active = {
        responseId: msg.response_id as string,
        epoch,
        playingSegment: segmentId,
        totalSegments: msg.total ?? null,
        lastConfirmedPlayed: null,
      };
    } else {
      // 同一 response 的下一段：上一段结算为 played（Q-2 口径）
      this.settlePlayed(this.active.playingSegment);
      this.active.playingSegment = segmentId;
      if (msg.total !== undefined) this.active.totalSegments = msg.total;
    }
    this.sendAckFor(segmentId, "playing");
    // 末段（total 已知且本段是最后一段）：起兜底计时，轨 ended 不触发也能收口
    if (this.active.totalSegments !== null && segmentId >= this.active.totalSegments - 1) {
      this.scheduleTailFallback(msg.text.length);
    }
    return true;
  }

  /**
   * 轨结束（audioEl ended）：末段结算为 played 并复位响应状态。
   * 无活跃响应（整段模式或已收口）时为 no-op。
   */
  markTrackEnded(): void {
    this.cancelTail();
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
    this.cancelRetry();
  }

  /**
   * 末段兜底计时（J3：轨 ended 在 WebRTC 单轨连续流上不可靠，v1.2 允许计时兜底）。
   * 到点时若响应未被新段/打断/轨结束收口，按轨结束同路径结算。
   */
  private scheduleTailFallback(textLength: number): void {
    this.cancelTail();
    this.tailTimer = setTimeout(() => {
      this.tailTimer = null;
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
