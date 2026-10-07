import {useCanWrite} from './permissions';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { date, elapsed, finite, number, post, request } from './api';
import type { HistoryPoint, Incident, Scenario, Station, TwinState } from './types';
import { Icon } from './Icons';
import type { IconName } from './Icons';
import { LineChart } from './Charts';
import { StationEditor, statusLabels } from './StationEditor';
import { Scenarios } from './Scenarios';
import { Robots } from './Robots';
import { ImportPreview, ImportLog } from './ImportTools';
import { QualityPage } from './Manufacturing';
import { AutomationPage, type AutomationOperation } from './Automation';
import { ThemeToggle } from './Theme';
import { LocalAI } from './LocalAI';
import { ScadaPage } from './Scada';
import { AnimatedNumber } from './Motion';

type Page = 'overview' | 'quality' | 'automation' | 'scada' | 'ai' | 'scenarios' | 'incidents' | 'data';
const navigation: {id: Page; title: string; icon: IconName}[] = [
  {id: 'overview', title: 'Обзор производства', icon: 'grid'},
  {id: 'quality', title: 'Контроль качества', icon: 'check'},
  {id: 'automation', title: 'Автоматизация', icon: 'gear'},
  {id: 'scada', title: 'Оборудование и ТО', icon: 'layers'},
  {id: 'ai', title: 'Локальный ИИ', icon: 'activity'},
  {id: 'scenarios', title: 'Сценарии', icon: 'flask'},
  {id: 'incidents', title: 'Инциденты', icon: 'bell'},
  {id: 'data', title: 'Данные и настройки', icon: 'database'},
];
const pageDescriptions: Record<Page, string> = {
  overview: 'Вся производственная линия. В одном пространстве.',
  quality: 'Проверенные автомобили: марка, модель, цвет, количество и результат.',
  automation: 'Сварка, покраска и сборка. Показания, расходные материалы и состояние роботов.',
  scada: 'Узлы роботов и конвейеров, ресурс, обслуживание и запасные части.',
  ai: 'Локальный анализ данных и проверяемые предложения действий.',
  scenarios: 'Проверяйте решения и оценивайте их влияние на выпуск.',
  incidents: 'События на линии, причины и состояние реакции.',
  data: 'Источники данных, параметры смены и прозрачность модели.',
};
type Action = <T>(key: string, operation: () => Promise<T>, success?: string) => Promise<T | undefined>;

export default function App({account, canWrite = true}: {account?: ReactNode; canWrite?: boolean}) {
  const [page, setPage] = useState<Page>('overview');
  const [state, setState] = useState<TwinState | null>(null);
  const [history, setHistory] = useState<HistoryPoint[]>([]);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [selectedStation, setSelectedStation] = useState<string | null>(null);
  const [selectedOperation, setSelectedOperation] = useState<AutomationOperation | null>(null);
  const [connectionError, setConnectionError] = useState('');
  const [toast, setToast] = useState<{type: 'success' | 'error'; message: string} | null>(null);
  const [busy, setBusy] = useState('');
  const [menuOpen, setMenuOpen] = useState(false);
  const refreshRunning = useRef(false);
  const mutationVersion = useRef(0);
  const mounted = useRef(true);
  const refresh = useCallback(async () => {
    if (refreshRunning.current) return;
    refreshRunning.current = true;
    const version = mutationVersion.current;
    try {
      const {state: newState, history: newHistory, incidents: newIncidents} = await request<{state: TwinState; history: HistoryPoint[]; incidents: Incident[]}>('/dashboard');
      if (mounted.current && version === mutationVersion.current) {
        setState(newState); setHistory(newHistory ?? []); setIncidents(newIncidents ?? []); setConnectionError('');
      }
    } catch (error) {if (mounted.current) setConnectionError(error instanceof Error ? error.message : 'Нет связи с сервером');}
    finally {refreshRunning.current = false;}
  }, []);
  useEffect(() => {
    mounted.current = true; void refresh();
    void request<Scenario[]>('/scenarios').then(value => {if (mounted.current) setScenarios(value ?? []);}).catch(() => {});
    const timer = setInterval(() => {if (!document.hidden) void refresh();}, 2000);
    return () => {mounted.current = false; clearInterval(timer);};
  }, [refresh]);
  useEffect(() => {if (!toast) return; const timer = setTimeout(() => setToast(null), toast.type === 'error' ? 15000 : 5000); return () => clearTimeout(timer);}, [toast]);
  const action: Action = async (key, operation, success) => {
    if (!canWrite) {setToast({type: 'error', message: 'Ваша роль разрешает только просмотр.'}); return undefined;}
    setBusy(key); mutationVersion.current += 1;
    try {const result = await operation(); if (success) setToast({type: 'success', message: success}); return result;}
    catch (error) {setToast({type: 'error', message: error instanceof Error ? error.message : 'Не удалось выполнить действие'}); return undefined;}
    finally {mutationVersion.current += 1; setBusy(''); void refresh();}
  };
  const updateState = (next: TwinState) => {mutationVersion.current += 1; setState(next);};
  const control = async (payload: Record<string, unknown>) => {
    const next = await action('control', () => post<TwinState>('/control', payload));
    if (next) updateState(next);
  };
  const closeEditor = useCallback(() => setSelectedStation(null), []);
  const independentPage = ['quality', 'automation', 'scada', 'ai'].includes(page);
  const openIncidents = incidents.filter(item => item.status !== 'resolved');
  const currentStation = state?.stations?.find(item => item.id === selectedStation);
  // Continuous motion (flow along the line, pulsing status lights) only plays while the model really runs.
  const live = Boolean(state?.running && state.source === 'simulation' && !connectionError);
  const navRef = useRef<HTMLElement>(null);
  const [indicator, setIndicator] = useState<{top: number; height: number} | null>(null);
  useLayoutEffect(() => {
    const nav = navRef.current;
    if (!nav) return;
    const measure = () => {
      const active = nav.querySelector<HTMLElement>('.nav-item.active');
      setIndicator(current => !active ? null : current?.top === active.offsetTop && current.height === active.offsetHeight ? current : {top: active.offsetTop, height: active.offsetHeight});
    };
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(nav);
    window.addEventListener('resize', measure);
    return () => {observer?.disconnect(); window.removeEventListener('resize', measure);};
  }, [page]);
  const toastIds = useRef(new WeakMap<object, number>());
  const toastCounter = useRef(0);
  let toastKey = 0;
  if (toast) {
    toastKey = toastIds.current.get(toast) ?? ++toastCounter.current;
    toastIds.current.set(toast, toastKey);
  }
  function navigate(next: Page) {setPage(next); setMenuOpen(false); if(next==='automation')setSelectedOperation(null);}
  function openStation(id: string) {
    if(['welding','painting','assembly'].includes(id)) {
      setSelectedOperation(id as AutomationOperation); setPage('automation'); setMenuOpen(false);
    } else setSelectedStation(id);
  }
  return <div className="app-shell" data-live={live ? '' : undefined}>
    {menuOpen && <button className="mobile-scrim" aria-label="Закрыть меню" onClick={() => setMenuOpen(false)}/>}
    <aside id="main-sidebar" className={`sidebar ${menuOpen ? 'open' : ''}`}>
      <a href="#overview" className="brand" onClick={event => {event.preventDefault(); navigate('overview');}} aria-label="Allur twin 2.0 — обзор"><span className="brand-symbol"><i/><i/><i/></span><span>allur<span className="brand-twin">twin</span><small>PRODUCTION INTELLIGENCE</small></span></a>
      <div className="site-selector"><span className="site-icon"><Icon name="factory" size={19}/></span><span><strong>Производственная линия</strong><small>Цифровая модель · Allur</small></span><span className="site-dot"/></div>
      <span className="nav-label">РАБОЧЕЕ ПРОСТРАНСТВО</span>
      <nav ref={navRef} className={indicator ? 'has-indicator' : undefined} aria-label="Основная навигация">{indicator && <span className="nav-indicator" aria-hidden="true" style={{transform: `translateY(${indicator.top}px)`, height: indicator.height}}/>}{navigation.map(item => <button type="button" key={item.id} data-testid={`nav-${item.id}`} className={`nav-item ${page === item.id ? 'active' : ''}`} onClick={() => navigate(item.id)} aria-current={page === item.id ? 'page' : undefined}><Icon name={item.icon} size={19}/><span>{item.title}</span>{item.id === 'incidents' && openIncidents.length > 0 && <span className="nav-count" key={openIncidents.length}>{openIncidents.length}</span>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="model-status"><span className={`status-light ${connectionError ? 'offline' : ''}`}/><span>{connectionError ? 'Связь прервана' : state ? 'Система доступна' : 'Подключение…'}</span></div><p>Данные модели отделены<br/>от импортированной телеметрии.</p><div className="sidebar-footer"><span className="avatar">AT</span><span><strong>Пульт управления</strong><small>Allur twin 2.0</small></span><Icon name="gear" size={18}/></div></div>
    </aside>
    <div className="main-shell"><header className="topbar"><div className="breadcrumb"><button type="button" className="icon-button mobile-menu" aria-label="Открыть меню" aria-controls="main-sidebar" aria-expanded={menuOpen} onClick={() => setMenuOpen(true)}><Icon name="menu"/></button><span>Рабочее пространство</span><Icon name="chevron" size={13}/><strong>{navigation.find(item => item.id === page)?.title}</strong></div><div className="topbar-right"><ThemeToggle/><span className="timezone">UTC+5</span><span className="topbar-separator"/><Icon name="clock" size={15}/><span>{state ? date(state.updated_at) : 'Соединение…'}</span>{account}</div></header>
    <main id="main-content"><div className="page-heading"><div className="page-heading-copy" key={page}><div className="heading-kicker"><span/> ALLUR DIGITAL TWIN</div><h1>{page === 'overview' ? 'Обзор производства' : navigation.find(item => item.id === page)?.title}</h1><p>{pageDescriptions[page]}</p></div><div className="heading-actions">{state && !independentPage && <span data-testid="source-label" className={`source-badge ${state.source}`}><Icon name={state.source === 'simulation' ? 'flask' : 'database'} size={15}/>{state.source === 'simulation' ? 'Симуляция' : 'Телеметрия CSV / API'}</span>}<button type="button" className="button secondary compact" aria-label="Обновить данные" onClick={() => void refresh()}><Icon name="refresh" size={17}/></button></div></div>
      {connectionError && <div className="connection-banner" role="alert"><Icon name="warning" size={18}/><span><strong>Данные могут быть устаревшими.</strong> {connectionError} Автоматически повторяем подключение.</span></div>}
      {!state ? <div className="loading-panel"><span className="loading-bars" aria-hidden="true"><i/><i/><i/></span><h2>{connectionError ? 'Ожидаем соединение с сервером' : 'Подключаем производственную модель'}</h2><p>Загружаем участки, показатели и историю событий.</p></div> : <>
        {!independentPage && <div className="shift-toolbar"><div className="shift-clock"><span className={`status-light ${state.running && state.source === 'simulation' ? '' : 'paused'}`}/><strong>{state.source === 'telemetry' ? 'История наблюдений' : state.running ? 'Модель работает' : 'Модель на паузе'}</strong><span className="toolbar-divider"/><Icon name="clock" size={15}/><span data-testid="sim-time" className="mono">{elapsed(state.sim_time)}</span><span className="muted">/ {elapsed(state.shift_duration)}</span></div><div className="model-controls"><label className="speed-control"><span>Скорость</span><select data-testid="control-speed" aria-label="Скорость симуляции" value={state.speed} disabled={!canWrite || Boolean(busy) || state.source !== 'simulation'} onChange={event => void control({speed: Number(event.target.value)})}>{[1,10,30,60,120].map(speed => <option key={speed} value={speed}>×{speed}</option>)}</select></label><button data-testid="control-advance" className="button tertiary compact" type="button" disabled={!canWrite || Boolean(busy) || state.source !== 'simulation'} onClick={async () => {const next = await action('advance', () => post<TwinState>('/advance', {seconds: 600})); if (next) updateState(next);}} title="Продвинуть модель на 10 минут"><Icon name="clock" size={15}/>+10 мин</button><button data-testid="control-running" className="button light compact" type="button" disabled={!canWrite || Boolean(busy) || state.source !== 'simulation'} onClick={() => void control({running: !state.running})}><Icon name={state.running ? 'pause' : 'play'} size={14}/>{state.running ? 'Пауза' : 'Запустить'}</button></div></div>}
        {page === 'overview' && state.telemetry_kind === 'robots' && <Robots state={state}/>} 
        {page === 'overview' && state.telemetry_kind !== 'robots' && <Overview state={state} history={history} incidents={incidents} onStation={openStation} onScenarios={() => navigate('scenarios')} onIncidents={() => navigate('incidents')}/>}
        {page === 'scenarios' && <Scenarios state={state} scenarios={scenarios} onCreated={item => setScenarios(items => [item, ...items.filter(value => value.id !== item.id)])}/>}
        {page === 'quality' && <QualityPage/>}
        {page === 'automation' && <AutomationPage state={state} selectedOperation={selectedOperation} onSelectOperation={setSelectedOperation} onEditStation={setSelectedStation}/>}
        {page === 'ai' && <LocalAI/>}
        {page === 'scada' && <ScadaPage/>}
        {page === 'incidents' && <Incidents incidents={incidents} busy={busy} onAcknowledge={async id => {const item = await action('acknowledge', () => post<Incident>(`/incidents/${encodeURIComponent(id)}/acknowledge`, {}), 'Инцидент принят в работу'); if (item) setIncidents(items => items.map(value => value.id === id ? item : value));}}/>}
        {page === 'data' && <DataSettings state={state} busy={busy} action={action} onState={updateState} onRefresh={refresh}/>}
      </>}
      <footer className="main-footer"><span><Icon name="layers" size={13}/> ALLUR TWIN</span><span>{independentPage ? 'Отчёты, записи контроля и показания оборудования.' : state?.source === 'simulation' ? 'Расчёты основаны на модели. Заводское подключение не настроено.' : 'Значения из CSV. Пропуски обозначены «—».'}</span><span>Обновление каждые 2 сек</span></footer>
    </main></div>
    {currentStation && <StationEditor key={currentStation.id} station={currentStation} telemetry={state?.source === 'telemetry'} onClose={closeEditor} onSaved={next => {updateState(next); setToast({type: 'success', message: 'Параметры участка обновлены'}); void refresh();}}/>}
    {toast && <div key={toastKey} data-testid="toast" className={`toast ${toast.type}`} role={toast.type === 'error' ? 'alert' : 'status'}><Icon name={toast.type === 'error' ? 'warning' : 'check'} size={20}/><span>{toast.message}</span><button className="icon-button" onClick={() => setToast(null)} aria-label="Закрыть уведомление"><Icon name="close" size={16}/></button><span className="toast-timer" aria-hidden="true" style={{animationDuration: toast.type === 'error' ? '15s' : '5s'}}/></div>}
  </div>;
}

function Metric({label, value, digits = 0, suffix, icon, note, accent, testId}: {label: string; value: unknown; digits?: number; suffix?: string; icon: IconName; note: string; accent?: boolean; testId?: string}) {
  return <section className={`metric-card ${accent ? 'accent' : ''}`}><div className="metric-label"><span>{label}</span><Icon name={icon} size={18}/></div><div className="metric-value" data-testid={testId}><AnimatedNumber value={value} digits={digits}/><small>{suffix}</small></div><div className="metric-note"><span className="tiny-dot"/>{note}</div></section>;
}

function Overview({state, history, incidents, onStation, onScenarios, onIncidents}: {state: TwinState; history: HistoryPoint[]; incidents: Incident[]; onStation: (id: string) => void; onScenarios: () => void; onIncidents: () => void}) {
  const metrics = state.metrics;
  const stations = [...(state.stations ?? [])].sort((a,b) => a.order - b.order);
  const active = incidents.filter(item => item.status !== 'resolved');
  const critical = active.filter(item => item.severity === 'critical').length;
  const latest = history.slice(-90);
  const predictions = state.predictions ?? [];
  return <>
    <div className="metrics-grid"><Metric label="Годная продукция" value={metrics.good} suffix="ед." icon="box" note={`План смены: ${number(state.shift_plan)} единиц`} accent testId="metric-good"/><Metric label="Прогноз до конца смены" value={metrics.forecast_good} suffix={metrics.forecast_good === null ? '' : 'ед.'} icon="activity" note={metrics.forecast_good === null ? 'Недостаточно данных для прогноза' : `Выполнение плана: ${number(metrics.forecast_good / state.shift_plan * 100, 1)}%`}/><Metric label="Эффективность · OEE модели" value={metrics.oee} digits={1} suffix={metrics.oee === null ? '' : '%'} icon="target" note={metrics.oee === null ? 'Не рассчитывается из CSV' : `Доступность ${number(metrics.availability, 1)}% · качество ${number(metrics.quality, 1)}%`}/><Metric label="Активные инциденты" value={active.length} icon="warning" note={critical ? `${critical} требуют особого внимания` : 'Критических событий нет'}/></div>
    <section className="panel production-panel"><div className="panel-heading"><div><span className="eyebrow">Поток производства</span><h2>Состояние линии<span className="count-badge">{stations.length} участков</span></h2></div><div className="production-legend"><span><i className="legend-running"/>Работает</span><span><i className="legend-wait"/>Ожидание</span><span><i className="legend-stop"/>Остановка</span></div></div>
      <div className="production-map"><div className="map-rail"/><div className="map-flow-label"><span>ВХОДЯЩИЙ ПОТОК</span><span>ГОТОВАЯ ПРОДУКЦИЯ <Icon name="arrow" size={13}/></span></div><div className="stations-grid" style={{gridTemplateColumns: `repeat(${Math.max(stations.length, 1)}, minmax(144px, 1fr))`}}>{stations.map((station, index) => <StationCard key={station.id} station={station} index={index} telemetry={state.source === 'telemetry'} onClick={() => onStation(station.id)}/>)}</div></div>
      <div className="production-summary"><span><Icon name="activity" size={15}/>Темп линии<strong><AnimatedNumber value={metrics.throughput} digits={1}/> <small>ед./ч</small></strong></span><span><Icon name="layers" size={15}/>В производстве<strong><AnimatedNumber value={metrics.wip}/> <small>ед.</small></strong></span><span><Icon name="warning" size={15}/>Брак на линии<strong><AnimatedNumber value={metrics.rejected}/> <small>ед.</small></strong></span><span className="map-hint"><Icon name="info" size={14}/>Нажмите на участок для настройки</span></div>
    </section>
    <div className="overview-lower"><div className="overview-main"><section className="panel history-panel"><div className="panel-heading"><div><span className="eyebrow">Динамика смены</span><h2>Производственные показатели</h2></div><span className="subtle-pill"><span className="tiny-dot"/>{state.source === 'simulation' ? 'Время модели' : 'Время наблюдений'}</span></div><div className="chart-topline"><div><strong><AnimatedNumber value={metrics.produced}/></strong><span>готовых единиц всего</span></div><div><strong><AnimatedNumber value={metrics.quality} digits={1}/><small>%</small></strong><span>качество по всей линии</span></div></div><div data-testid="production-chart"><LineChart live={state.running && state.source === 'simulation'} labels={latest.map(item => elapsed(item.sim_time).slice(0,5))} series={[{name: 'Готовая продукция', color: 'var(--chart-primary)', values: latest.map(item => item.produced)}, {name: 'Брак на линии', color: 'var(--danger)', values: latest.map(item => item.rejected)}]}/></div></section>
    <section className="panel recent-panel"><div className="panel-heading"><h2>Последние события</h2><button type="button" className="text-button" onClick={onIncidents}>Все события<Icon name="arrow" size={15}/></button></div>{incidents.length ? <div className="recent-list">{incidents.slice(0,3).map(item => <div className="recent-row" key={item.id}><span className={`incident-symbol ${item.severity}`}><Icon name={item.severity === 'info' ? 'info' : 'warning'} size={16}/></span><div><strong>{item.title}</strong><span>{item.station_name}</span></div><span className={`incident-status ${item.status}`}>{incidentStatus(item.status)}</span><time>{elapsed(item.sim_time)}</time></div>)}</div> : <div className="empty-inline with-icon"><Icon name="check" size={18}/><span>Событий пока нет. Изменения состояния появятся здесь автоматически.</span></div>}</section></div>
    <aside className="overview-aside"><section className="panel insights-panel"><div className="panel-heading"><div className="insight-title"><Icon name="bolt" size={19}/><h2>Прогнозы и риски</h2></div><span className="count-badge">{predictions.length}</span></div><div className="insight-caption">На основе состояния и темпа линии</div>{predictions.length ? <div className="predictions-list">{predictions.map((item, index) => <div className={`prediction-card ${item.severity}`} key={`${item.station_id}-${item.type}-${index}`}><div className="prediction-top"><span className="prediction-label">{item.severity === 'critical' ? 'Высокий риск' : item.severity === 'warning' ? 'Обратите внимание' : 'Наблюдение'}</span>{item.eta_minutes !== null && <span><Icon name="clock" size={12}/>{number(item.eta_minutes, 1)} мин</span>}</div><h3>{item.title}</h3><p>{item.description}</p><button type="button" className="prediction-station" onClick={() => onStation(item.station_id)}>{item.station_name}<Icon name="arrow" size={14}/></button></div>)}</div> : <div className="no-risks"><span><Icon name="check" size={22}/></span><strong>Расчётных рисков нет</strong><p>{state.source === 'telemetry' ? 'По последнему импортированному снимку. Автоматическое обновление телеметрии не подключено.' : 'Модель продолжает следить за темпом линии и заполнением буферов.'}</p></div>}<button type="button" className="button secondary full-width" onClick={onScenarios}><Icon name="flask" size={16}/>Проверить сценарий<Icon name="arrow" size={16}/></button></section>
    <section className="panel shift-plan"><div className="panel-heading"><h2>План смены</h2><Icon name="target" size={18}/></div><div className="plan-figures"><strong><AnimatedNumber value={metrics.plan_progress} digits={1}/><small>%</small></strong><span><AnimatedNumber value={metrics.good}/> / {number(state.shift_plan)} ед.</span></div><div className="progress-track"><i style={{width: `${Math.min(100, Math.max(0, finite(metrics.plan_progress)))}%`}}/></div><div className="plan-time"><span>Прошло {number(state.sim_time / 3600, 1)} ч</span><span>Смена {number(state.shift_duration / 3600, 1)} ч</span></div></section></aside></div>
  </>;
}

function StationCard({station, index, telemetry, onClick}: {station: Station; index: number; telemetry: boolean; onClick: () => void}) {
  const fill = Math.min(100, Math.max(0, finite(station.queue) / Math.max(1, finite(station.buffer_capacity)) * 100));
  const icons: IconName[] = ['box', 'bolt', 'layers', 'gear', 'check', 'factory'];
  return <button type="button" data-testid={`station-${station.id}`} className={`station-card ${station.status}`} onClick={onClick} aria-label={`${station.name}, ${statusLabels[station.status] ?? station.status}. ${['welding','painting','assembly'].includes(station.id) ? 'Открыть автоматизацию' : 'Настроить участок'}`}><div className="station-top"><span className="station-icon"><Icon name={icons[index % icons.length]} size={22}/></span><span className="station-number">{String(index + 1).padStart(2, '0')}</span></div><h3>{station.name}</h3><span className={`status-pill ${station.status}`}><i/>{statusLabels[station.status] ?? station.status}</span><div className="station-output"><strong><AnimatedNumber value={station.completed}/></strong><span>обработано</span></div><div className="station-queue-label"><span>Буфер</span><strong>{number(station.queue)} <small>/ {number(station.buffer_capacity)}</small></strong></div><div className={`buffer-bar ${fill >= 80 ? 'full' : ''}`}><i style={{width: `${fill}%`}}/></div><div className="station-bottom"><span>{number(telemetry ? station.cycle_seconds : station.cycle_seconds / Math.max(.1, station.speed_factor), 1)} сек/цикл</span><Icon name="arrowUp" size={14}/></div></button>;
}

function incidentStatus(status: Incident['status']) {return status === 'open' ? 'Открыт' : status === 'acknowledged' ? 'В работе' : 'Завершён';}
function Incidents({incidents, busy, onAcknowledge}: {incidents: Incident[]; busy: string; onAcknowledge: (id: string) => Promise<void>}) {
  const canWrite = useCanWrite();
  const [filter, setFilter] = useState('all');
  const [query, setQuery] = useState('');
  const visible = incidents.filter(item => (filter === 'all' || item.status === filter) && `${item.title} ${item.station_name} ${item.description}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  return <section className="panel incidents-panel"><div className="panel-heading"><div><span className="eyebrow">Журнал событий</span><h2>Инциденты линии<span className="count-badge">{incidents.length}</span></h2></div><a className="button secondary" href="/api/incidents/export" download>Экспорт всех событий</a><input className="search-input" type="search" aria-label="Поиск инцидентов" placeholder="Поиск по событию или участку" value={query} onChange={event => setQuery(event.target.value)}/></div><p className="small-note">Все активные события и последние завершённые. Полная история всех источников доступна в экспорте.</p><div className="tab-filters" role="group" aria-label="Фильтр инцидентов">{[['all', 'Все события'], ['open', 'Открытые'], ['acknowledged', 'В работе'], ['resolved', 'Завершённые']].map(([id, title]) => <button type="button" key={id} className={filter === id ? 'active' : ''} onClick={() => setFilter(id)} aria-pressed={filter === id}>{title}<span>{incidents.filter(item => id === 'all' || item.status === id).length}</span></button>)}</div>
  {visible.length ? <div className="incident-list">{visible.map(item => <article key={item.id} data-testid={`incident-${item.id}`} className={`incident-card ${item.severity}`}><span className={`incident-symbol ${item.severity}`}><Icon name={item.severity === 'info' ? 'info' : 'warning'} size={21}/></span><div className="incident-body"><div className="incident-title"><h3>{item.title}</h3><span className={`incident-status ${item.status}`}>{incidentStatus(item.status)}</span></div><p>{item.description}</p><div className="incident-meta"><span><Icon name="factory" size={13}/>{item.station_name}</span><span><Icon name="clock" size={13}/>{elapsed(item.sim_time)} от начала наблюдений / смены</span><span>{date(item.created_at)}</span></div></div>{item.status === 'open' ? <button type="button" data-testid={`incident-ack-${item.id}`} className="button secondary compact" disabled={!canWrite || Boolean(busy)} onClick={() => void onAcknowledge(item.id)}><Icon name="check" size={15}/>Принять в работу</button> : <span className="incident-done"><Icon name="check" size={18}/>{item.status === 'resolved' ? 'Событие завершено' : 'Принят оператором'}</span>}</article>)}</div> : <div className="empty-state"><span className="empty-icon"><Icon name="bell" size={28}/></span><h3>{incidents.length ? 'Ничего не найдено' : 'Пока без происшествий'}</h3><p>{incidents.length ? 'Измените поисковый запрос или фильтр.' : 'Изменения состояния и расчётные риски сохраняются автоматически.'}</p></div>}</section>;
}

function DataSettings({state, busy, action, onState, onRefresh}: {state: TwinState; busy: string; action: Action; onState: (state: TwinState) => void; onRefresh: () => Promise<void>}) {
  const canWrite = useCanWrite();
  const [file, setFile] = useState<File | null>(null);
  const [importError, setImportError] = useState('');
  const [importMessage, setImportMessage] = useState('');
  const [importBusy, setImportBusy] = useState(false);
  const [shiftPlan, setShiftPlan] = useState(state.shift_plan);
  const [resetOpen, setResetOpen] = useState(false);
  const [resetAcknowledged, setResetAcknowledged] = useState(false);
  const resetDialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {if (resetOpen) resetDialog.current?.showModal(); else resetDialog.current?.close();}, [resetOpen]);
  async function importFile(event: FormEvent) {
    event.preventDefault(); if (!file) return;
    setImportBusy(true); setImportError(''); setImportMessage('');
    const form = new FormData(); form.append('file', file);
    try {
      const result = await request<{imported: number; message: string}>('/import', {method: 'POST', body: form});
      setImportMessage(`${result.message} Импортировано строк: ${number(result.imported)}.`);
      const next = await request<TwinState>('/state'); onState(next); await onRefresh();
    } catch (error) {setImportError(error instanceof Error ? error.message : 'Ошибка импорта');} finally {setImportBusy(false);}
  }
  async function source(source: TwinState['source'], telemetry_kind?: 'production' | 'robots') {const next = await action('source', () => post<TwinState>('/source', {source, telemetry_kind}), source === 'simulation' ? 'Открыта сохранённая симуляция' : 'Открыта импортированная телеметрия'); if (next) onState(next);}
  return <div className="data-layout"><div className="data-main"><section className="panel"><div className="panel-heading"><div><span className="eyebrow">Подключение</span><h2>Источник данных</h2></div><Icon name="database" size={21}/></div><p className="muted intro">Симуляция и сохранённые серии наблюдений. Переключение сохраняет вашу модель и импортированные наблюдения.</p><div className="source-options"><button type="button" data-testid="source-simulation" className={`source-option ${state.source === 'simulation' ? 'selected' : ''}`} disabled={!canWrite || Boolean(busy) || state.source === 'simulation'} onClick={() => void source('simulation')}><span className="source-option-icon"><Icon name="flask" size={25}/></span><strong>Симуляция</strong><p>Очереди, оборудование и выпуск рассчитываются производственной моделью.</p><span className="source-option-action">{state.source === 'simulation' ? 'Активный источник' : 'Переключиться'}<Icon name={state.source === 'simulation' ? 'check' : 'arrow'} size={16}/></span></button><button type="button" data-testid="source-telemetry" className={`source-option ${state.source === 'telemetry' ? 'selected' : ''}`} disabled={!canWrite || Boolean(busy) || state.source === 'telemetry' || !state.telemetry_updated_at} onClick={() => void source('telemetry')}><span className="source-option-icon"><Icon name="database" size={25}/></span><strong>Телеметрия CSV</strong><p>Наблюдения из CSV или API. Каждая запись проверяется и сохраняется.</p><span className="source-option-action">{state.source === 'telemetry' ? 'Активный источник' : state.telemetry_updated_at ? 'Переключиться' : 'Сначала загрузите CSV'}<Icon name={state.source === 'telemetry' ? 'check' : 'arrow'} size={16}/></span></button></div><div className="source-options"><button type="button" className="button secondary" disabled={!canWrite || Boolean(busy) || !state.telemetry_available?.production} onClick={() => void source('telemetry', 'production')}>Сохранённые участки</button><button type="button" className="button secondary" disabled={!canWrite || Boolean(busy) || !state.telemetry_available?.robots} onClick={() => void source('telemetry', 'robots')}>Сохранённые роботы</button></div><div className="source-updated"><Icon name="clock" size={14}/>Последняя телеметрия: {date(state.telemetry_updated_at)} <span>UTC+5</span></div></section>
    <section className="panel import-panel"><div className="panel-heading"><div><span className="eyebrow">Фактические наблюдения</span><h2>Импорт телеметрии</h2></div><a data-testid="data-template" className="button secondary compact" href="/api/import/template" download><Icon name="download" size={16}/>Шаблон CSV</a></div><p className="muted intro">Загрузите CSV датчиков роботов, контроля качества, счётчиков участков или DOCX с таблицами кейса. Формат определяется по заголовку. Все строки проходят проверку до сохранения.</p><form onSubmit={importFile}><label className={`file-drop ${file ? 'has-file' : ''}`}><input data-testid="data-import-file" type="file" accept=".csv,.docx,text/csv" onChange={event => {setFile(event.target.files?.[0] ?? null); setImportError(''); setImportMessage('');}} disabled={!canWrite || importBusy}/><span className="file-upload-icon"><Icon name={file ? 'check' : 'upload'} size={25}/></span><strong>{file ? file.name : 'Выберите CSV или DOCX'}</strong><span>{file ? `${number(file.size / 1024, 1)} КБ · нажмите, чтобы заменить` : 'Нажмите для выбора файла на компьютере'}</span><small>UTF-8 · запятая · до 5 МБ и 20 000 строк</small></label><ImportPreview key={file?.name + String(file?.lastModified)} file={file}/><div className="import-footer"><span><Icon name="info" size={15}/>После проверки данные сохранятся в своём разделе</span><button data-testid="data-import-submit" type="submit" className="button primary" disabled={!canWrite || !file || importBusy}>{importBusy ? 'Проверяем и сохраняем…' : 'Импортировать'}<Icon name="arrow" size={16}/></button></div></form>{importError && <div data-testid="import-error" className="inline-error" role="alert">{importError}</div>}{importMessage && <div data-testid="import-success" className="inline-success" role="status"><Icon name="check" size={17}/>{importMessage}</div>}<p className="intro"><a href="/api/import/robots-template" download>Скачать шаблон роботов</a> · <a href="/api/robots/export" download>Экспорт измерений роботов</a></p><details className="csv-details"><summary>Формат роботов</summary><div className="csv-columns"><code>timestamp,robot_id,line_section,joint_temperature_c,vibration_mm_s,hydraulic_pressure_bar,cycle_status,error_code</code></div><p>Допускается node_id вместо robot_id. Нужны время с часовым поясом, идентификатор, статус и хотя бы один датчик. Давление: hydraulic_pressure_bar, pneumatic_pressure_bar (бар) или pneumatic_pressure (единица не указана). Пустые датчики — неизвестное значение. Порядок столбцов свободный. Последующие файлы добавляют более новые наблюдения для каждого робота. Повторные записи отклоняются без частичного сохранения.</p></details><details className="csv-details"><summary>Формат участков и правила проверки</summary><div className="csv-columns"><code>timestamp,station_id,status,produced,rejected,queue,cycle_seconds</code></div><ul><li>Каждый снимок времени должен содержать все 5 участков.</li><li>Добавляйте только более новые снимки. Накопительные счётчики не должны уменьшаться.</li><li><strong>timestamp</strong> — время наблюдения в формате ISO 8601 с часовым поясом.</li><li><strong>station_id</strong> — идентификатор участка из модели: warehouse, welding, painting, assembly, quality.</li><li><strong>status</strong> — running, stopped, starved или blocked.</li><li><strong>produced / rejected</strong> — накопительные счётчики; брак не больше общего выпуска.</li><li><strong>queue</strong> — длина очереди; <strong>cycle_seconds</strong> — время цикла в секундах.</li></ul></details></section>
    <ImportLog revision={state.updated_at}/><section className="panel export-panel"><div className="export-symbol"><Icon name="download" size={25}/></div><div><h2>Экспорт истории</h2><p>История всех смен и источников с идентификаторами в формате CSV.</p></div><a data-testid="data-export" className="button secondary" href="/api/export" download><Icon name="download" size={16}/>Скачать CSV</a></section></div>
    <aside className="data-aside"><section className="panel"><div className="panel-heading"><h2>Параметры смены</h2><Icon name="gear" size={18}/></div><form onSubmit={async event => {event.preventDefault(); const next = await action('plan', () => post<TwinState>('/control', {shift_plan: shiftPlan}), 'План смены обновлён'); if (next) onState(next);}}><label>План годной продукции, ед.<input type="number" aria-label="План смены" min="1" max="1000000" step="1" required value={shiftPlan} disabled={!canWrite || state.source === 'telemetry' || Boolean(busy)} onChange={event => setShiftPlan(Number(event.target.value))}/></label><button className="button secondary full-width" type="submit" disabled={!canWrite || state.source === 'telemetry' || Boolean(busy) || shiftPlan === state.shift_plan}>Сохранить план</button></form><div className="settings-divider"/><div className="setting-line"><span>Длительность</span><strong>{number(state.shift_duration / 3600)} часов</strong></div><div className="setting-line"><span>Идентификатор смены</span><code title={state.run_id}>{state.run_id.slice(0,8)}</code></div><button data-testid="reset-open" type="button" className="button danger full-width" disabled={!canWrite || state.source === 'telemetry' || Boolean(busy)} onClick={() => {setResetOpen(true); setResetAcknowledged(false);}}><Icon name="refresh" size={16}/>Начать новую смену</button><p className="small-note">Счётчики и время обнулятся. Параметры участков сохранятся.</p></section>
    <section className="panel integration-panel"><div className="panel-heading"><h2>Приём измерений через API</h2></div><p className="intro">Для передачи новых измерений используйте POST /api/robots/measurements. Данные проходят те же проверки и сразу появляются на экране. Для интеграции создайте ключ в настройках учётной записи и передавайте Authorization: Bearer и Idempotency-Key.</p><p className="small-note">Пакет: объект measurements с массивом записей robot_id, timestamp, cycle_status и датчиков. До 1000 записей. Время каждой новой записи должно быть позже предыдущей для этого робота. Ответ 422 означает, что весь пакет отклонён.</p><a className="button secondary full-width" href="/api/docs" target="_blank" rel="noreferrer">Открыть справку API</a></section><section className="panel model-documentation"><div className="panel-heading"><h2>Как считаем</h2><Icon name="info" size={18}/></div><h3>{state.model?.name ?? 'Производственная модель'}</h3><p>{state.model?.description}</p><dl><dt>Годная продукция</dt><dd>Принятые единицы с последнего участка линии.</dd><dt>Качество</dt><dd>Годная продукция / (годная продукция + брак на всех участках).</dd><dt>OEE модели</dt><dd>Доступность × производительность × качество. Показатели используются как доли.</dd><dt>Производительность</dt><dd>Темп выпуска относительно номинальной пропускной способности узкого места.</dd><dt>Прогноз</dt><dd>Модельная оценка по текущему состоянию. Не гарантия фактического результата.</dd></dl><div className="info-note"><Icon name="info" size={16}/><p>Значения, которые нельзя достоверно получить из CSV, обозначены «—».</p></div></section></aside>
    <dialog ref={resetDialog} className="confirm-dialog" onCancel={event => {event.preventDefault(); setResetOpen(false);}}><div className="confirm-icon"><Icon name="refresh" size={26}/></div><h2>Начать новую смену?</h2><p>Текущее время, выпуск и незавершённое производство симуляции будут обнулены. Параметры участков сохранятся. История предыдущей смены останется в базе.</p><label className="confirm-check"><input type="checkbox" checked={resetAcknowledged} onChange={event => setResetAcknowledged(event.target.checked)}/><span>Подтверждаю начало новой смены</span></label><div className="confirm-actions"><button type="button" className="button secondary" onClick={() => setResetOpen(false)}>Отмена</button><button data-testid="reset-confirm" type="button" className="button danger" disabled={!resetAcknowledged || Boolean(busy)} onClick={async () => {const next = await action('reset', () => post<TwinState>('/reset', {confirm: true}), 'Начата новая смена'); if (next) {onState(next); setResetOpen(false);}}}>Начать смену</button></div></dialog>
  </div>;
}











