import { useEffect, useRef, useState } from 'react';
import { number, request } from './api';
const date = (value: string | null | undefined) => value ? new Date(value).toLocaleString('ru-RU', {timeZone: 'Asia/Qyzylorda', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'}) : '—';
import type { RobotObservation, TwinState } from './types';
import { RobotSettings } from './RobotSettings';
import type { RobotConfig } from './RobotSettings';

interface SensorStats {
  count: number; missing: number; min: number | null; max: number | null; mean: number | null; stddev: number | null; last: number | null;
  trend_reason: string; trend: null | {per_minute: number; r_squared: number; samples: number; window_seconds: number; crossing: null | {limit: number; kind: string; eta_minutes: number; timestamp: string}};
}
interface Analytics {measurements: number; duration_seconds: number; unknown_seconds: number; status_seconds: Record<string, number>; interval_assumption: string; sensors: Record<string, SensorStats>}

type Sensor = 'joint_temperature_c' | 'vibration_mm_s' | 'hydraulic_pressure_bar' | 'pneumatic_pressure_bar' | 'pneumatic_pressure';
const sensors: {key: Sensor; label: string; unit: string}[] = [
  {key: 'joint_temperature_c', label: 'Температура узла', unit: '°C'},
  {key: 'vibration_mm_s', label: 'Вибрация', unit: 'мм/с'},
  {key: 'hydraulic_pressure_bar', label: 'Гидравлическое давление', unit: 'бар'},
  {key: 'pneumatic_pressure_bar', label: 'Пневматическое давление', unit: 'бар'},
  {key: 'pneumatic_pressure', label: 'Пневматическое давление', unit: 'единица не указана'},
];
const statuses: Record<string, string> = {in_progress: 'В работе', idle: 'Простой', warning: 'Предупреждение', fault: 'Неисправность', error: 'Ошибка', welding: 'Сварка', assembly: 'Сборка', painting: 'Покраска'};
const statusLabel = (v: string) => statuses[v.toLowerCase()] ?? v;
const clock = (v: string) => new Date(v).toLocaleTimeString('ru-RU', {timeZone: 'Asia/Qyzylorda'});

function SensorChart({rows, sensor, config}: {rows: RobotObservation[]; sensor: typeof sensors[number]; config: RobotConfig | null}) {
  const measured = rows.filter(r => r[sensor.key] !== null);
  if (!measured.length) return <p className="muted">Нет измерений</p>;
  const values = measured.map(r => r[sensor.key] as number);
  const limits = Object.entries(config?.limits[sensor.key] ?? {}).filter((entry): entry is [string, number] => typeof entry[1] === 'number');
  const low = Math.min(...values, ...limits.map(v => v[1])), high = Math.max(...values, ...limits.map(v => v[1]));
  const pad = Math.max((high - low) * .15, Math.abs(high) * .02, .1);
  const min = low - pad, max = high + pad;
  const begin = Date.parse(rows[0].timestamp), end = Date.parse(rows.at(-1)!.timestamp);
  const x = (r: RobotObservation) => 100 + (Date.parse(r.timestamp) - begin) / Math.max(1, end - begin) * 550;
  const y = (v: number) => 160 - (v - min) / (max - min) * 140;
  // Missing sensor values break the line instead of becoming zero or being interpolated.
  let path = '', previous = false, previousTime: number | null = null;
  for (const row of rows) {
    const value = row[sensor.key];
    if (value === null) {previous = false; continue;}
    if (previousTime !== null && Date.parse(row.timestamp) - previousTime > (config?.expected_interval_seconds ?? 60) * 3000) previous = false;
    path += `${previous ? 'L' : 'M'}${x(row)},${y(value)} `; previous = true;
    previousTime = Date.parse(row.timestamp);
  }
  return <svg viewBox="0 0 710 195" role="img" aria-label={`${sensor.label}: ${number(values.at(-1), 2)} ${sensor.unit}`} className="robot-chart">
    {[min, (min + max) / 2, max].map(v => <g key={v}><line x1="100" x2="650" y1={y(v)} y2={y(v)} stroke="var(--chart-grid)"/><text x="90" y={y(v) + 4} textAnchor="end">{number(v, 2)}</text></g>)}
    <path d={path} stroke="var(--chart-primary)" strokeWidth="2" fill="none"/>
    {limits.map(([key, limit]) => <line key={key} x1="100" x2="650" y1={y(limit)} y2={y(limit)} stroke={key.includes('critical') ? 'var(--danger)' : 'var(--warning)'} strokeDasharray="8 6"><title>Заданная граница: {number(limit, 2)} {sensor.unit}</title></line>)}
    {measured.map(r => <circle key={r.timestamp} cx={x(r)} cy={y(r[sensor.key] as number)} r="3" fill="var(--chart-primary)"><title>{date(r.timestamp)} · {number(r[sensor.key], 3)} {sensor.unit}</title></circle>)}
    <text x="100" y="187">{clock(rows[0].timestamp)}</text><text x="650" y="187" textAnchor="end">{clock(rows.at(-1)!.timestamp)}</text>
  </svg>;
}

export function Robots({state}: {state: TwinState}) {
  const robots = state.robots ?? [];
  const [selected, setSelected] = useState(robots[0]?.robot_id ?? '');
  const [history, setHistory] = useState<{total: number; rows: RobotObservation[]} | null>(null);
  const [error, setError] = useState('');
  const [config, setConfig] = useState<RobotConfig | null>(null);
  const [analytics, setAnalytics] = useState<Analytics | null>(null);
  const [computedAt, setComputedAt] = useState('');
  const [revision, setRevision] = useState(0), [offset, setOffset] = useState(0);
  const [start, setStart] = useState(''), [end, setEnd] = useState('');
  const [period, setPeriod] = useState(''), [search, setSearch] = useState('');
  const [robotPage, setRobotPage] = useState(0);
  const filteredRobots = robots.filter(r => `${r.robot_id} ${r.line_section}`.toLowerCase().includes(search.toLowerCase()));
  const pageCount = Math.max(1, Math.ceil(filteredRobots.length / 24));
  const currentPage = Math.min(robotPage, pageCount - 1);
  const activeView = useRef('');
  const robot = robots.find(r => r.robot_id === selected) ?? robots[0];
  useEffect(() => {
    const controller = new AbortController(); setError('');
    const view = `${robot?.robot_id}:${period}:${offset}`;
    if (activeView.current !== view) {setHistory(null); setAnalytics(null); activeView.current = view;}
    const query = `robot_id=${encodeURIComponent(robot?.robot_id ?? '')}${period}`;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        if (!robot || controller.signal.aborted || document.hidden) return;
        const value = await request<{history: {total: number; rows: RobotObservation[]}; analytics: Analytics | null; config: RobotConfig; warning: string | null; computed_at: string}>(`/robots/detail?${query}&offset=${offset}`, {signal: controller.signal});
        if (!controller.signal.aborted) {
          setHistory(value.history); setAnalytics(value.analytics); setConfig(value.config); setError(value.warning || ''); setComputedAt(value.computed_at);
        }
      } catch (e) {
        if (!controller.signal.aborted) setError(String(e));
      } finally {
        // Poll even after the last packet: a cached view must eventually catch up
        // when no later state change arrives. Never overlap requests.
        if (!controller.signal.aborted && robot) timer = setTimeout(() => void refresh(), 2000);
      }
    };
    void refresh();
    return () => {controller.abort(); clearTimeout(timer);};
  }, [robot?.robot_id, state.run_id, revision, period, offset]);
  return <div className="robots-view">
    <section className="panel"><div className="panel-heading"><div><span className="eyebrow">Фактические наблюдения</span><h2>Телеметрия роботов</h2></div><a className="button secondary" href="/api/robots/export" download>Скачать измерения</a></div>
      <p className="muted">{number(robots.length)} робота · {number(state.observation_count)} измерений · {date(state.observation_start)} — {date(state.telemetry_updated_at)} (UTC+5)</p>
      <p className="intro">Статистика и тренды рассчитываются из измерений. Настройте пороги и справочник кодов для вашего оборудования. Выпуск и OEE требуют отдельных производственных счётчиков.</p>
      <label className="robot-search">Найти робота или участок<input type="search" aria-label="Найти робота или участок" value={search} onChange={e => {setSearch(e.target.value); setRobotPage(0);}}/></label>
      <div className="robot-grid">{filteredRobots.slice(currentPage*24, (currentPage+1)*24).map(r => <button key={r.robot_id} type="button" data-testid={`robot-${r.robot_id}`} className={`robot-card ${robot?.robot_id === r.robot_id ? 'selected' : ''}`} aria-pressed={robot?.robot_id === r.robot_id} onClick={() => {setSelected(r.robot_id); setOffset(0);}}>
        <strong>{r.robot_id}</strong><span>{r.line_section || 'Участок не указан'}</span><b>{statusLabel(r.cycle_status)}</b><span>Код: {r.error_code || '—'}</span><small>Наблюдение: {date(r.timestamp)} (UTC+5)</small>
      </button>)}</div>
      <div className="robot-actions"><span>Найдено: {filteredRobots.length} · страница {currentPage+1} из {pageCount}</span><button className="button secondary" disabled={currentPage === 0} onClick={() => setRobotPage(currentPage-1)}>Предыдущие роботы</button><button className="button secondary" disabled={currentPage+1 >= pageCount} onClick={() => setRobotPage(currentPage+1)}>Следующие роботы</button></div>
    </section>
    {robot && <section className="panel"><div className="panel-heading"><div><span className="eyebrow">История оборудования · UTC+5</span><h2>Датчики {robot.robot_id}</h2></div></div>
      {config && <p className="muted">{Date.parse(robot.timestamp) > Date.now() ? 'Метка последнего наблюдения в будущем: проверьте часы источника.' : Date.now() - Date.parse(robot.timestamp) > config.expected_interval_seconds * 3000 ? 'Последнее наблюдение устарело относительно заданного интервала.' : 'Последнее наблюдение в пределах заданного интервала.'}</p>}
      <form className="robot-period" onSubmit={e => {e.preventDefault(); setOffset(0); setPeriod((start ? `&start=${encodeURIComponent(new Date(start + '+05:00').toISOString())}` : '') + (end ? `&end=${encodeURIComponent(new Date(end + '+05:00').toISOString())}` : ''));}}>
        <label>С (UTC+5)<input type="datetime-local" step="1" aria-label="Начало периода" value={start} onChange={e => setStart(e.target.value)}/></label>
        <label>По (UTC+5)<input type="datetime-local" step="1" aria-label="Конец периода" value={end} onChange={e => setEnd(e.target.value)}/></label>
        <button type="submit" className="button secondary">Применить период</button><button type="button" className="button secondary" onClick={() => {setStart(''); setEnd(''); setPeriod(''); setOffset(0);}}>Вся история</button>
      </form>
      <div className="robot-actions"><a className="button secondary" href={`/api/robots/export?robot_id=${encodeURIComponent(robot.robot_id)}${period}`} download>Экспорт робота за период</a><button type="button" className="button secondary" onClick={() => setRevision(v => v + 1)}>Обновить историю</button></div>
      {error && <p role="alert" className="inline-error">{error}</p>}
      {!history && !error && <p>Загружаем измерения…</p>}
      {history && <><p className="muted">Показано {history.rows.length} из {history.total} наблюдений. Полная история доступна в экспорте. Пропуск измерения: «—».</p><p className="small-note">Расчёт: {date(computedAt)} (UTC+5). При поступлении данных история и аналитика обновляются с задержкой до 5 секунд плюс интервал опроса экрана.</p>
        <div className="robot-actions"><button type="button" className="button secondary" disabled={offset + 500 >= history.total} onClick={() => setOffset(v => v + 500)}>Более ранние 500</button><button type="button" className="button secondary" disabled={offset === 0} onClick={() => setOffset(v => Math.max(0, v - 500))}>Более поздние 500</button><span>Пропущено последних записей: {number(offset)}</span></div>
        {!history.rows.length && <p className="intro">В выбранном периоде нет измерений.</p>}
        <div className="robot-sensors">{sensors.filter(s => history.rows.some(r => r[s.key] !== null)).map(s => <article key={s.key}><h3>{s.label} <small>· {s.unit}</small></h3><strong className="robot-reading">{number(history.rows.at(-1)?.[s.key], 2)} <small>{s.unit} · конец страницы</small></strong><SensorChart rows={history.rows} sensor={s} config={config}/><SensorStatistics stats={analytics?.sensors[s.key]} unit={s.unit}/></article>)}</div>
        {analytics && <div className="robot-status-summary"><h3>Состояния за выбранный период</h3><p className="small-note">{analytics.interval_assumption}</p><div className="robot-actions">{Object.entries(analytics.status_seconds).map(([status, seconds]) => <span key={status}>{statusLabel(status)}: {number(seconds / 60, 2)} мин</span>)}<span>Неизвестный интервал: {number(analytics.unknown_seconds / 60, 2)} мин</span></div></div>}
        <details className="csv-details"><summary>Точные значения наблюдений</summary><div className="robot-table"><table><thead><tr><th>Время (UTC+5)</th><th>Температура, °C</th><th>Вибрация, мм/с</th><th>Гидравлика, бар</th><th>Пневматика, бар</th><th>Пневматика, ед. не указана</th><th>Статус</th><th>Код</th></tr></thead><tbody>{history.rows.map(r => <tr key={r.timestamp}><td>{date(r.timestamp)}</td>{sensors.map(s => <td key={s.key}>{number(r[s.key], 3)}</td>)}<td>{statusLabel(r.cycle_status)}</td><td>{r.error_code || '—'}</td></tr>)}</tbody></table></div></details>
      </>}
      <RobotSettings key={robot.robot_id} robotId={robot.robot_id} sensors={sensors} onSaved={value => {setConfig(value); setRevision(v => v + 1);}}/>
    </section>}
  </div>;
}

function SensorStatistics({stats, unit}: {stats?: SensorStats; unit: string}) {
  if (!stats) return null;
  return <div className="sensor-statistics"><p>За период: мин. {number(stats.min, 2)} · среднее {number(stats.mean, 2)} · макс. {number(stats.max, 2)} {unit}</p><p>Измерений: {number(stats.count)} · пропусков: {number(stats.missing)} · σ {number(stats.stddev, 2)}</p>
    {stats.trend && <p>Тренд: {number(stats.trend.per_minute, 3)} {unit}/мин · R² {number(stats.trend.r_squared, 3)} · {stats.trend.samples} точек</p>}
    {stats.trend?.crossing && <p className="trend-crossing">При сохранении тренда граница {number(stats.trend.crossing.limit, 2)} {unit} будет достигнута через {number(stats.trend.crossing.eta_minutes, 1)} мин после последнего измерения: {date(stats.trend.crossing.timestamp)} (UTC+5).</p>}
    <p className="small-note">{stats.trend_reason}</p>
  </div>;
}


