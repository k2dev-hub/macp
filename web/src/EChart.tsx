import { useEffect, useRef } from "react";
import * as echarts from "echarts";
import type { EChartsOption } from "echarts";

interface Props {
  option: EChartsOption;
  height?: number;
}

export default function EChart({ option, height = 260 }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!ref.current) {
      return;
    }
    const chart = echarts.init(ref.current);
    chartRef.current = chart;
    const resize = () => chart.resize();
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    chartRef.current?.setOption(option, true);
  }, [option]);

  return <div ref={ref} style={{ height, width: "100%" }} />;
}
