import {useCallback, useEffect, useRef, useState} from 'react';
import {number, request} from './api';
import {Icon} from './Icons';
import './monitoring.css';

function date(value: string | null | undefined) {
  if (!value) return 'Нет данных';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 'Нет данных' : parsed.toLocaleString('ru-RU', {timeZone: 'Asia/Qyzylorda', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'});
}

export type MonitoringAlert = {
  id: string; robot_id: string; line_section: string; type: string;
  severity: 'warning' | 'critical'; status: 'active' | 'resolved';
  title: string; description: string; observed_at: string;
  first_seen_at: string; last_seen_at: string; resolved_at: string | null;
  acknowledged: boolean; recommendation: string;
  ai_status: 'pending' | 'ready' | 'unavailable' | 'not_needed';
  ai_analysis: string | null; ai_actions: string[]; ai_error: string | null; ai_observed_at?: string | null;
};
type MonitoringSnapshot = {
  enabled: boolean; interval_seconds: number; last_scan_at: string | null;
  last_error: string | null; robots_monitored: number; active_count: number;
  unread_count: number; ai_enabled: boolean; alerts: MonitoringAlert[];
};
type Notice = {count: number; title: string; critical: boolean};

export function useMonitoring() {
  const [data, setData] = useState<MonitoringSnapshot | null>(null);
  const [error, setError] = useState('');
  const [actionError, setActionError] = useState('');
  const [busyId, setBusyId] = useState('');
  const [notice, setNotice] = useState<Notice | null>(null);
  const alive = useRef(false);
  const revision = useRef(0);
  const polling = useRef<AbortController | null>(null);
  const accepting = useRef<AbortController | null>(null);
  const seen = useRef(new Map<string, string>());

  const refresh = useCallback(async () => {
    if (!alive.current || polling.current) return;
    const controller = new AbortController();
    polling.current = controller;
    const startedAtRevision = revision.current;
    try {
      const next = await request<MonitoringSnapshot>('/monitoring', {signal: controller.signal});
      if (!alive.current || controller.signal.aborted || startedAtRevision !== revision.current) return;
      const incoming = next.alerts.filter(item => item.status === 'active' && !item.acknowledged && seen.current.get(item.id) !== item.severity);
      seen.current = new Map(next.alerts.map(item => [item.id, item.severity]));
      setData(next); setError('');
      if (incoming.length) setNotice({count: incoming.length, title: incoming[0].title, critical: incoming.some(item => item.severity === 'critical')});
    } catch (cause) {
      if (alive.current && !controller.signal.aborted && startedAtRevision === revision.current) setError(cause instanceof Error ? cause.message : 'Не удалось получить события мониторинга.');
    } finally {
      if (polling.current === controller) polling.current = null;
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void refresh();
    const timer = setInterval(() => void refresh(), 10000);
    const onVisible = () => {if (!document.hidden) void refresh();};
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      alive.current = false; revision.current += 1;
      polling.current?.abort(); polling.current = null;
      accepting.current?.abort(); accepting.current = null;
      clearInterval(timer); document.removeEventListener('visibilitychange', onVisible);
    };
  }, [refresh]);

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 15000);
    return () => clearTimeout(timer);
  }, [notice]);

  const acknowledge = useCallback(async (id: string) => {
    if (accepting.current || !alive.current) return;
    const controller = new AbortController();
    accepting.current = controller; revision.current += 1;
    polling.current?.abort(); polling.current = null;
    setBusyId(id); setActionError('');
    try {
      const item = await request<MonitoringAlert>(`/monitoring/${encodeURIComponent(id)}/acknowledge`, {method: 'POST', body: '{}', signal: controller.signal});
      if (!alive.current || controller.signal.aborted) return;
      setData(previous => {
        if (!previous) return previous;
        const old = previous.alerts.find(alert => alert.id === item.id);
        return {...previous, alerts: previous.alerts.map(alert => alert.id === item.id ? item : alert), unread_count: Math.max(0, previous.unread_count - (old?.status === 'active' && !old.acknowledged ? 1 : 0))};
      });
      setNotice(null);
    } catch (cause) {
      if (alive.current && !controller.signal.aborted) setActionError(cause instanceof Error ? cause.message : 'Не удалось принять событие в работу.');
    } finally {
      if (accepting.current === controller) accepting.current = null;
      if (alive.current && !controller.signal.aborted) {
        revision.current += 1; setBusyId('');
        (polling.current as AbortController | null)?.abort(); polling.current = null;
        void refresh();
      }
    }
  }, [refresh]);

  return {data, error, actionError, busyId, notice, acknowledge, refresh, dismissNotice: () => setNotice(null)};
}

export type MonitoringController = ReturnType<typeof useMonitoring>;

export function MonitoringBadge({monitoring, onOpen}: {monitoring: MonitoringController; onOpen: () => void}) {
  const {data, error} = monitoring;
  const unavailable = Boolean(error || data?.last_error || data?.enabled === false);
  const label = unavailable ? 'Мониторинг роботов: требуется внимание к подключению' : data ? `Мониторинг роботов: активных ${data.active_count}, не принятых в работу ${data.unread_count}` : 'Мониторинг роботов: подключение';
  return <button type="button" data-testid="monitoring-badge" className={`monitoring-badge ${unavailable ? 'unavailable' : data?.unread_count ? 'has-unread' : ''}`} aria-label={label} title={label} onClick={onOpen}>
    <Icon name={unavailable ? 'warning' : 'bell'} size={17}/><span className="monitoring-badge-label">Роботы</span>
    <span className="monitoring-badge-count">{data ? data.active_count : '—'}</span>
    {Boolean(data?.unread_count) && <span className="monitoring-unread-dot" aria-hidden="true"/>}
  </button>;
}

export function MonitoringNotice({monitoring, onOpen}: {monitoring: MonitoringController; onOpen: () => void}) {
  const notice = monitoring.notice;
  if (!notice) return null;
  return <aside className={`monitoring-notice ${notice.critical ? 'critical' : ''}`} role="status" aria-live="polite" data-testid="monitoring-notice">
    <Icon name="warning" size={21}/><div><strong>{notice.count === 1 ? 'Новое событие робота' : `Новых событий: ${notice.count}`}</strong><p>{notice.title}</p><button type="button" className="text-button" onClick={() => {monitoring.dismissNotice(); onOpen();}}>Открыть мониторинг <Icon name="arrow" size={14}/></button></div>
    <button type="button" className="icon-button" aria-label="Скрыть уведомление" onClick={monitoring.dismissNotice}><Icon name="close" size={16}/></button>
  </aside>;
}

export function MonitoringPage({monitoring, canWrite, onData, onSettings}: {monitoring: MonitoringController; canWrite: boolean; onData: () => void; onSettings: () => void}) {
  const {data, actionError, busyId, acknowledge} = monitoring;
  const [scope, setScope] = useState<'active' | 'all'>('active');
  const [severity, setSeverity] = useState<'all' | 'warning' | 'critical'>('all');
  const alerts = (data?.alerts ?? []).filter(item => (scope === 'all' || item.status === 'active') && (severity === 'all' || item.severity === severity))
    .sort((a, b) => Number(b.status === 'active') - Number(a.status === 'active') || Number(b.severity === 'critical') - Number(a.severity === 'critical') || b.last_seen_at.localeCompare(a.last_seen_at));
  const criticalCount = data?.alerts.filter(item => item.status === 'active' && item.severity === 'critical').length ?? 0;
  return <div className="monitoring-page">
    <section className="panel monitoring-service">
      <div className="monitoring-service-title"><span className={`status-light ${!data?.enabled || data?.last_error || monitoring.error ? 'paused' : ''}`}/><div><h2>{data?.enabled ? 'Непрерывный контроль телеметрии' : data ? 'Мониторинг выключен' : 'Подключаем мониторинг'}</h2><p>Проверка показаний на сервере каждые {data?.interval_seconds ?? 10} сек. Уведомления доступны сотрудникам в приложении.</p></div></div>
      <div className="monitoring-service-meta"><span>Последняя проверка <strong>{date(data?.last_scan_at)} · UTC+5</strong></span><span>ИИ-анализ <strong>{data ? data.ai_enabled ? 'Включён · статус в событиях' : 'Выключен · правила работают' : 'Подключение…'}</strong></span></div>
    </section>
    <div className="monitoring-stats">
      <div><Icon name="factory" size={18}/><span>Роботов с телеметрией</span><strong>{data ? number(data.robots_monitored) : '—'}</strong></div>
      <div><Icon name="activity" size={18}/><span>Активных событий</span><strong>{data ? number(data.active_count) : '—'}</strong></div>
      <div className={criticalCount ? 'critical' : ''}><Icon name="warning" size={18}/><span>Критических в журнале</span><strong>{data ? number(criticalCount) : '—'}</strong></div>
      <div className={data?.unread_count ? 'warning' : ''}><Icon name="bell" size={18}/><span>Ожидают сотрудника</span><strong>{data ? number(data.unread_count) : '—'}</strong></div>
    </div>
    <section className="panel monitoring-events">
      <div className="panel-heading"><div><span className="eyebrow">Проблема → данные → действия</span><h2>События роботов</h2></div><button className="button secondary compact" type="button" onClick={() => void monitoring.refresh()}><Icon name="refresh" size={15}/>Обновить</button></div>
      <div className="monitoring-filters"><div className="tab-filters" role="group" aria-label="Статус события">{(['active', 'all'] as const).map(value => <button key={value} type="button" aria-pressed={scope === value} className={scope === value ? 'active' : ''} onClick={() => setScope(value)}>{value === 'active' ? 'Активные' : 'Вся история'}</button>)}</div><label>Важность<select value={severity} onChange={event => setSeverity(event.target.value as typeof severity)}><option value="all">Все уровни</option><option value="critical">Критические</option><option value="warning">Предупреждения</option></select></label></div>
      <p className="monitoring-caption">«Принято в работу» видно всем сотрудникам. Проблема остаётся активной до новых показаний, подтверждающих восстановление. В журнале — до 200 событий.</p>
      {actionError && <div className="inline-error" role="alert">{actionError}</div>}
      {!data ? <div className="empty-state"><Icon name="activity" size={28}/><h3>{monitoring.error ? 'Ожидаем соединение' : 'Загружаем события'}</h3><p>Автоматически проверяем подключение каждые 10 секунд.</p></div>
        : alerts.length ? <div className="monitoring-alerts">{alerts.map(item => <MonitoringCard key={item.id} alert={item} canWrite={canWrite} busy={Boolean(busyId)} accepting={busyId === item.id} onAcknowledge={() => void acknowledge(item.id)}/>)}</div>
          : <div className="empty-state monitoring-empty"><span className="empty-icon"><Icon name={data.robots_monitored ? 'check' : 'database'} size={28}/></span><h3>{!data.robots_monitored ? 'Ожидаем телеметрию роботов' : 'Нет событий по выбранным фильтрам'}</h3><p>{!data.robots_monitored ? 'Подключите поступление показаний через API или загрузите CSV роботов. Для контроля температуры, вибрации и давления задайте пороги конкретного оборудования.' : 'Мониторинг проверяет сохранённые измерения. Для контроля датчиков должны быть заданы пороги оборудования.'}</p><div className="monitoring-empty-actions"><button type="button" className="button secondary" onClick={onData}>Источники данных</button><button type="button" className="button secondary" onClick={onSettings}>Роботы и пороги</button></div></div>}
    </section>
  </div>;
}

function MonitoringCard({alert, canWrite, busy, accepting, onAcknowledge}: {alert: MonitoringAlert; canWrite: boolean; busy: boolean; accepting: boolean; onAcknowledge: () => void}) {
  const resolved = alert.status === 'resolved';
  const section = ({welding: 'Сварка', painting: 'Покраска', assembly: 'Сборка'} as Record<string, string>)[alert.line_section] ?? alert.line_section;
  return <article data-testid={`monitoring-alert-${alert.id}`} className={`monitoring-card ${alert.severity} ${resolved ? 'resolved' : ''}`}>
    <div className="monitoring-card-heading"><div className="monitoring-card-title"><span className="monitoring-severity"><Icon name={resolved ? 'check' : 'warning'} size={16}/>{alert.severity === 'critical' ? 'Критическое' : 'Предупреждение'}</span><h3>{alert.title}</h3></div><span className={`monitoring-state ${resolved ? 'resolved' : ''}`}>{resolved ? 'Восстановлено по данным' : 'Активно'}</span></div>
    <div className="monitoring-identity"><span><Icon name="factory" size={14}/><strong>{alert.robot_id}</strong>{section && ` · ${section}`}</span><span><Icon name="clock" size={14}/>Измерение: <time dateTime={alert.observed_at}>{date(alert.observed_at)}</time> · UTC+5</span></div>
    <div className="monitoring-evidence"><h4>Показания и условие срабатывания</h4><p>{alert.description}</p></div>
    <div className="monitoring-recommendations"><section className="monitoring-rule"><h4><Icon name="layers" size={15}/>Рекомендация по правилу</h4><p>{alert.recommendation}</p></section><section className="monitoring-ai"><h4><Icon name="activity" size={15}/>ИИ-анализ <span>{alert.ai_status === 'ready' ? 'Гипотеза' : alert.ai_status === 'pending' ? 'В очереди' : alert.ai_status === 'not_needed' ? 'Не требуется' : 'Недоступен'}</span></h4>{alert.ai_status === 'ready' ? <><p>{alert.ai_analysis || 'Анализ сформирован.'}</p>{alert.ai_observed_at && <p className="monitoring-ai-note">Показания для анализа: {date(alert.ai_observed_at)} · UTC+5</p>}{alert.ai_actions?.length > 0 && <ol>{alert.ai_actions.map((action, index) => <li key={index}>{action}</li>)}</ol>}<p className="monitoring-ai-note">Предложение модели. Причина не подтверждена; требуется проверка специалистом.</p></> : <p className="monitoring-ai-note">{alert.ai_status === 'pending' ? 'Разбор события в очереди или выполняется. Рекомендация по правилу уже доступна.' : alert.ai_status === 'not_needed' ? 'Дополнительный анализ не запрошен. Доступна рекомендация по правилу.' : 'Модель сейчас недоступна. Контроль по правилам и уведомления продолжают работать.'}</p>}{alert.ai_error && <details className="monitoring-ai-error"><summary>Причина недоступности анализа</summary><p>{alert.ai_error}</p></details>}</section></div>
    <div className="monitoring-card-footer"><div><span>Впервые: {date(alert.first_seen_at)}</span><span>{resolved ? `Восстановление: ${date(alert.resolved_at)}` : `Последнее срабатывание: ${date(alert.last_seen_at)}`}</span></div>{alert.acknowledged ? <span className="monitoring-accepted"><Icon name="check" size={16}/>Принято в работу</span> : !resolved && <button type="button" className="button secondary" disabled={!canWrite || busy} title={!canWrite ? 'Доступно оператору и администратору' : undefined} onClick={onAcknowledge}>{accepting ? 'Сохраняем…' : 'Принять в работу'}</button>}</div>
  </article>;
}
