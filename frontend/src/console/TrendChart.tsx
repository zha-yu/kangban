/**
 * 长者 14 天健康趋势图（O1 / PRD FR-HLT-09 P1，console 医生复核辅助）。
 *
 * 口径：近 14 天收缩压/舒张压折线（左轴 mmHg）+ 睡眠时长（右轴 h），
 * Chart.js 按 PRD 指定；数据来自 C1③ `GET /api/health-records/trend/{elder_id}`
 * （Mock 模式由 health-api 生成稳定演示序列）。
 *
 * 仅在医生复核队列按需展开（useEffect 建图 / 卸毁），不进首屏 bundle 逻辑。
 */

import { useEffect, useRef, useState } from "react";
import { Chart } from "chart.js/auto";
import { getHealthTrend, type HealthTrend } from "./health-api";

export function TrendChart({ elderId, elderName }: { elderId: string; elderName: string }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [trend, setTrend] = useState<HealthTrend | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setTrend(null);
    setError(null);
    getHealthTrend(elderId)
      .then((t) => {
        if (alive) setTrend(t);
      })
      .catch(() => {
        if (alive) setError("趋势数据加载失败，请稍后重试");
      });
    return () => {
      alive = false;
    };
  }, [elderId]);

  useEffect(() => {
    if (!trend || !canvasRef.current) return;
    const labels = trend.points.map((p) => {
      const d = new Date(p.recorded_at);
      return `${d.getMonth() + 1}/${d.getDate()}`;
    });
    const chart = new Chart(canvasRef.current, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "收缩压 mmHg",
            data: trend.points.map((p) => p.systolic),
            borderColor: "#c0392b",
            backgroundColor: "transparent",
            tension: 0.3,
            pointRadius: 2,
          },
          {
            label: "舒张压 mmHg",
            data: trend.points.map((p) => p.diastolic),
            borderColor: "#e67e22",
            backgroundColor: "transparent",
            tension: 0.3,
            pointRadius: 2,
          },
          {
            label: "睡眠 h",
            data: trend.points.map((p) => p.sleep_hours),
            borderColor: "#2980b9",
            backgroundColor: "transparent",
            borderDash: [5, 4],
            tension: 0.3,
            pointRadius: 2,
            yAxisID: "y1",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          y: {
            position: "left",
            title: { display: true, text: "mmHg" },
            suggestedMin: 60,
            suggestedMax: 170,
          },
          y1: {
            position: "right",
            title: { display: true, text: "h" },
            suggestedMin: 0,
            suggestedMax: 12,
            grid: { drawOnChartArea: false },
          },
        },
      },
    });
    return () => chart.destroy();
  }, [trend]);

  if (error) return <p className="trend-error">{error}</p>;
  if (!trend) return <p className="trend-loading">正在加载近 14 天趋势…</p>;

  return (
    <div className="trend-box">
      <p className="trend-title">
        {elderName} · 近 {trend.window_days} 天血压 / 睡眠趋势
      </p>
      <div className="trend-canvas-wrap">
        <canvas ref={canvasRef} />
      </div>
    </div>
  );
}
