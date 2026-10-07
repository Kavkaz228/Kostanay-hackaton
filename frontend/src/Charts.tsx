import { useId } from 'react';
import { finite, number } from './api';

export interface ChartSeries {name: string; color: string; values: number[]; dashed?: boolean}
/** Lines draw themselves in when the chart appears; `live` adds a pulsing marker at the newest point. */
export function LineChart({labels, series, unit = 'ед.', emptyText = 'История появится по мере работы линии.', live = false}: {labels: string[]; series: ChartSeries[]; unit?: string; emptyText?: string; live?: boolean}) {
  const id = useId().replace(/:/g, '');
  const max = Math.max(1, ...series.flatMap(item => item.values.map(value => finite(value))));
  const top = Math.ceil(max / 4) * 4;
  const width = 740, height = 235, left = 46, right = 16, bottom = 32, padding = 16;
  const x = (i: number) => left + i / Math.max(labels.length - 1, 1) * (width - left - right);
  const y = (value: number) => height - bottom - finite(value) / top * (height - bottom - padding);
  if (!labels.length) return <div className="chart-empty"><span className="empty-chart-line"/><p>{emptyText}</p></div>;
  const ticks = [...new Set([0, Math.floor((labels.length - 1) / 4), Math.floor((labels.length - 1) / 2), Math.floor((labels.length - 1) * 3 / 4), labels.length - 1])];
  return <div className="chart-wrap"><svg className="line-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={series.map(item => `${item.name}: ${number(item.values.at(-1))} ${unit}`).join('; ')}>
    <defs>{series.map((item, i) => <linearGradient key={item.name} id={`${id}-${i}`} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor={item.color} stopOpacity=".18"/><stop offset="100%" stopColor={item.color} stopOpacity="0"/></linearGradient>)}</defs>
    {[0, 1, 2, 3, 4].map(i => <g key={i}><line className="chart-grid-line" style={{animationDelay: `${i * 60}ms`}} x1={left} x2={width - right} y1={y(top * i / 4)} y2={y(top * i / 4)} stroke="var(--chart-grid)" strokeDasharray="3 5"/><text x={left - 10} y={y(top * i / 4) + 4} textAnchor="end" fill="var(--chart-label)" fontSize="10">{number(top * i / 4)}</text></g>)}
    {ticks.map(i => <text key={i} x={x(i)} y={height - 8} textAnchor="middle" fill="var(--chart-label)" fontSize="10">{labels[i]}</text>)}
    {series.map((item, index) => {
      const points = item.values.map((v, i) => `${x(i)},${y(v)}`).join(' ');
      const last = item.values.length - 1;
      return <g key={item.name}>{!item.dashed && item.values.length > 1 && <polygon className="chart-area" points={`${left},${height-bottom} ${points} ${x(last)},${height-bottom}`} fill={`url(#${id}-${index})`}/>}<polyline className={`chart-line${item.dashed ? ' dashed' : ''}`} pathLength={item.dashed ? undefined : 1} style={{animationDelay: `${150 + index * 180}ms`}} points={points} fill="none" stroke={item.color} strokeWidth="2.3" strokeLinejoin="round" strokeLinecap="round" strokeDasharray={item.dashed ? '6 5' : undefined}/>{item.values.length === 1 && <circle cx={x(0)} cy={y(item.values[0])} r="3" fill={item.color}/>}{live && !item.dashed && item.values.length > 1 && <g transform={`translate(${x(last)} ${y(item.values[last])})`} aria-hidden="true"><circle className="chart-head-ring" r="4" fill={item.color}/><circle className="chart-head" r="3.6" fill={item.color} stroke="var(--panel)" strokeWidth="2"/></g>}</g>;
    })}
  </svg><div className="chart-legend">{series.map(item => <span key={item.name}><i style={{background: item.color}}/>{item.name}</span>)}<span className="chart-unit">{unit}</span></div></div>;
}
