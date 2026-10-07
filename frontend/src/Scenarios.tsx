import {useCanWrite} from './permissions';
import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { date, finite, number, post } from './api';
import type { Scenario, StationChange, TwinState } from './types';
import { Icon } from './Icons';
import { LineChart } from './Charts';

export function Scenarios({state, scenarios, onCreated}: {state: TwinState; scenarios: Scenario[]; onCreated: (value: Scenario) => void}) {
  const canWrite = useCanWrite();
  const [name, setName] = useState('Оптимизация производственной линии');
  const [horizon, setHorizon] = useState(120);
  const [changes, setChanges] = useState<StationChange[]>([]);
  const [stationId, setStationId] = useState(state.stations[0]?.id ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(scenarios[0]?.id ?? null);
  useEffect(() => {if (!selectedId && scenarios[0]) setSelectedId(scenarios[0].id);}, [selectedId, scenarios]);
  const selected = scenarios.find(item => item.id === selectedId) ?? scenarios[0];
  const available = state.stations.filter(item => !changes.some(change => change.station_id === item.id));
  function addStation() {
    const station = available.find(item => item.id === stationId) ?? available[0];
    if (!station) return;
    setChanges(items => [...items, {station_id: station.id, cycle_seconds: station.cycle_seconds, speed_factor: station.speed_factor, capacity: station.capacity, buffer_capacity: station.buffer_capacity, defect_rate: station.defect_rate, manual_stop: station.manual_stop}]);
    setStationId(available.find(item => item.id !== station.id)?.id ?? '');
  }
  function change(index: number, patch: Partial<StationChange>) {setChanges(items => items.map((item, i) => i === index ? {...item, ...patch} : item));}
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const scenario = await post<Scenario>('/scenarios', {name: name.trim(), horizon_minutes: horizon, changes});
      onCreated(scenario); setSelectedId(scenario.id);
    } catch (error) {setError(error instanceof Error ? error.message : 'Не удалось рассчитать сценарий');} finally {setBusy(false);}
  }
  return <div className="scenarios-layout">
    <section className="panel scenario-builder"><div className="panel-heading"><div><span className="eyebrow">Эксперимент</span><h2>Что, если?</h2></div><span className="square-icon"><Icon name="flask"/></span></div>
      <p className="muted intro">Сравните два будущих состояния линии из одной исходной точки. Текущая модель продолжит работать.</p>
      {state.source === 'telemetry' && <div className="info-note"><Icon name="info" size={17}/><p>CSV не содержит состояние рабочих мест и незавершённых операций. Для корректного сравнения сценариев переключитесь на симуляцию.</p></div>}
      <form onSubmit={submit}><fieldset disabled={!canWrite || busy || state.source === 'telemetry'} className="form-fieldset"><label>Название сценария<input data-testid="scenario-name" aria-label="Название сценария" required maxLength={100} value={name} onChange={event => setName(event.target.value)}/></label>
        <label>Горизонт расчёта<select data-testid="scenario-horizon" aria-label="Горизонт расчёта" value={horizon} onChange={event => setHorizon(Number(event.target.value))}>{[15,30,60,120,240,480].map(value => <option key={value} value={value}>{value < 60 ? `${value} минут` : `${value / 60} ч`}</option>)}</select></label>
        <div className="field-title">Изменения участков <span>{changes.length}</span></div>
        <div className="add-station-row"><select data-testid="scenario-station" aria-label="Участок для сценария" value={available.some(item => item.id === stationId) ? stationId : available[0]?.id ?? ''} onChange={event => setStationId(event.target.value)} disabled={!available.length}>{available.length ? available.map(item => <option key={item.id} value={item.id}>{item.name}</option>) : <option value="">Все участки добавлены</option>}</select><button data-testid="scenario-add" type="button" className="button secondary compact" onClick={addStation} disabled={!available.length} aria-label="Добавить участок"><Icon name="plus" size={18}/></button></div>
        {!changes.length && <p className="builder-hint">Добавьте участок и задайте параметры для сравнения с текущим режимом.</p>}
        {changes.map((item, index) => <div className="scenario-change" key={item.station_id}><div className="change-heading"><strong>{state.stations.find(station => station.id === item.station_id)?.name ?? item.station_id}</strong><button type="button" className="icon-button" aria-label={`Удалить изменение ${item.station_id}`} onClick={() => setChanges(items => items.filter((_, i) => i !== index))}><Icon name="trash" size={16}/></button></div><div className="form-grid compact-grid">
          <label>Цикл, сек<input aria-label={`Цикл ${item.station_id}`} type="number" required min="5" max="600" value={item.cycle_seconds} onChange={event => change(index, {cycle_seconds: Number(event.target.value)})}/></label>
          <label>Скорость ×<input data-testid={`scenario-speed-${item.station_id}`} aria-label={`Скорость ${item.station_id}`} type="number" required min="0.1" max="2" step="0.1" value={item.speed_factor} onChange={event => change(index, {speed_factor: Number(event.target.value)})}/></label>
          <label>Рабочие места<input type="number" required min="1" max="10" step="1" value={item.capacity} onChange={event => change(index, {capacity: Number(event.target.value)})}/></label>
          <label>Буфер, ед.<input type="number" required min="1" max="200" step="1" value={item.buffer_capacity} onChange={event => change(index, {buffer_capacity: Number(event.target.value)})}/></label>
          <label>Брак, %<input type="number" required min="0" max="50" step="0.1" value={Math.round(finite(item.defect_rate) * 1000) / 10} onChange={event => change(index, {defect_rate: Number(event.target.value) / 100})}/></label>
          <label>Состояние<select value={item.manual_stop ? 'stop' : 'run'} onChange={event => change(index, {manual_stop: event.target.value === 'stop'})}><option value="run">Работает</option><option value="stop">Остановлен</option></select></label>
        </div></div>)}
        <button data-testid="scenario-run" className="button primary full-width" type="submit" disabled={!name.trim() || !changes.length || busy || state.source === 'telemetry'}><Icon name={busy ? 'refresh' : 'play'} size={17} className={busy ? 'spin' : ''}/>{busy ? 'Моделируем два варианта…' : 'Рассчитать сценарий'}</button>
      </fieldset></form>{error && <div className="inline-error" role="alert">{error}</div>}
      <div className="method-note"><Icon name="layers" size={16}/><span>Одинаковые исходные данные и случайные события. Результат сохраняется автоматически.</span></div>
    </section>
    <div className="scenario-results"><section className="panel result-panel" data-testid="scenario-comparison"><div className="panel-heading"><div><span className="eyebrow">Сравнение вариантов</span><h2>{selected?.name ?? 'Результаты эксперимента'}</h2></div>{selected && <span className="subtle-pill"><Icon name="clock" size={14}/>{selected.horizon_minutes} мин</span>}</div>
      {selected ? <><p className="muted intro">Изменение относительно текущего режима за горизонт расчёта.</p><div className="delta-grid">{([
        ['good', 'Годная продукция', 'ед.', true], ['rejected', 'Брак на линии', 'ед.', false], ['wip', 'Незавершённое производство', 'ед.', false], ['downtime_minutes', 'Простой участков', 'мин', false]
      ] as const).map(([key, label, unit, positive]) => <div className="delta-card" key={key}><span>{label}</span><strong className={selected.delta[key] === 0 ? '' : (selected.delta[key] > 0 === positive ? 'mint' : 'amber')}>{selected.delta[key] > 0 ? '+' : ''}{number(selected.delta[key], key === 'downtime_minutes' ? 1 : 0)}<small>{unit}</small></strong><p>{number(selected.baseline[key], 1)} <Icon name="arrow" size={13}/> {number(selected.variant[key], 1)}</p></div>)}</div>
      <div className="chart-title-row"><h3>Выпуск годной продукции</h3><span>Накопительно с начала расчёта</span></div><LineChart labels={(selected.timeline ?? []).map(item => `${item.minute}′`)} series={[{name: 'Текущий режим', color: 'var(--chart-secondary)', dashed: true, values: (selected.timeline ?? []).map(item => item.baseline_good)}, {name: 'Ваш сценарий', color: 'var(--chart-primary)', values: (selected.timeline ?? []).map(item => item.variant_good)}]}/>
      <div className="scenario-explanation"><Icon name="info" size={19}/><p>{selected.explanation}</p></div></> : <div className="empty-state large"><span className="empty-icon"><Icon name="flask" size={32}/></span><h3>Проверьте решение до его применения</h3><p>Задайте параметры слева. Здесь появятся выпуск, брак, очереди и простои для двух вариантов.</p></div>}
    </section><section className="panel saved-scenarios"><div className="panel-heading"><h2>История сценариев</h2><span className="count-badge">{scenarios.length}</span></div>{scenarios.length ? <div className="saved-list">{scenarios.map(item => <button type="button" key={item.id} className={`saved-scenario ${selected?.id === item.id ? 'selected' : ''}`} onClick={() => setSelectedId(item.id)}><span className="scenario-list-icon"><Icon name="flask" size={18}/></span><span><strong>{item.name}</strong><small>{date(item.created_at)} · {item.horizon_minutes} мин · {item.changes.length} изм.</small></span><span className={`saved-delta ${item.delta.good >= 0 ? 'mint' : 'amber'}`}>{item.delta.good > 0 ? '+' : ''}{number(item.delta.good)} ед.</span><Icon name="chevron" size={16}/></button>)}</div> : <p className="empty-inline">Пока нет сохранённых расчётов.</p>}</section></div>
  </div>;
}


