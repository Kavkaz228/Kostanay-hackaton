import {useEffect, useState, type FormEvent} from 'react';
import {date, number, post, request} from './api';
import {useCanWrite} from './permissions';
import {ImportPreview} from './ImportTools';

export type DailyReport = {
  id: string; filename: string; imported_at: string; planned_total: number; plan_gap: number | null;
  warnings: string[]; targets: Record<string, number>;
  plans: {vehicle: string; quantity: number}[];
  lines: {date: string; line: string; operation: string; plan: number; actual: number; hours: number; utilization: number}[];
  downtimes: {date: string; section: string; operation: string; equipment: string; reason: string; minutes: number}[];
  quality: {date: string; section: string; quantity: number; rejected: number; calculated_percent: number | null}[];
};
type Vehicle = {timestamp: string; record_id: string; brand: string; model: string; color: string; quantity: number; rejected: number};
type Quality = {total: number; quantity: number; accepted: number; rejected: number; rows: Vehicle[]; groups: Vehicle[]};
export type ManufacturingData = {report: DailyReport | null; quality: Quality};
const recordId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2,'0')).join('');

export function useLive<T>(path: string, interval=2000) {
  const [value, setValue] = useState<T | null>(null), [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {if (!document.hidden) {const result = await request<T>(path, {signal: controller.signal}); if (!controller.signal.aborted) {setValue(result); setError('');}}}
      catch (e) {if (!controller.signal.aborted) setError((e as Error).message);}
      finally {if (!controller.signal.aborted) timer = setTimeout(() => void load(), interval);}
    };
    void load(); return () => {controller.abort(); clearTimeout(timer);};
  }, [path, interval, revision]);
  return {value, error, refresh: () => setRevision(v => v+1)};
}

export function ReportImport({onSaved}: {onSaved: () => void}) {
  const canWrite = useCanWrite();
  const [file, setFile] = useState<File | null>(null), [busy, setBusy] = useState(false), [message, setMessage] = useState(''), [error, setError] = useState('');
  async function upload(e: FormEvent) {
    e.preventDefault(); if (!file) return; setBusy(true); setError(''); setMessage('');
    const form = new FormData(); form.append('file', file);
    try {const result = await request<{message: string}>('/import', {method: 'POST', body: form}); setMessage(result.message); onSaved();}
    catch(e) {setError((e as Error).message);} finally {setBusy(false);}
  }
  return <section className="panel"><div className="panel-heading"><div><span className="eyebrow">Источник данных</span><h2>Загрузить отчёт или измерения</h2></div></div>
    <p className="intro">Документ DOCX из кейса содержит суточные отчёты. CSV качества содержит марки, модели, цвета и количество проверенных автомобилей. CSV роботов содержит текущие показания. Данные не подменяют друг друга.</p>
    <form onSubmit={upload} className="manufacturing-upload"><label>Файл CSV или DOCX<input aria-label="Файл отчёта или качества" type="file" accept=".csv,.docx" disabled={!canWrite || busy} onChange={e => {setFile(e.target.files?.[0] ?? null); setMessage(''); setError('');}}/></label><button className="button primary" disabled={!canWrite || !file || busy}>{busy ? 'Сохраняем…' : 'Загрузить данные'}</button></form>
    <ImportPreview file={file}/>{error && <p role="alert" className="inline-error">{error}</p>}{message && <p role="status" className="inline-success">{message}</p>}
    <div className="robot-actions"><a className="button secondary" href="/api/quality/template" download>Шаблон контроля качества</a><a className="button secondary" href="/api/import/robots-template" download>Шаблон показаний робота</a></div>
  </section>;
}

export function QualityPage() {
  const canWrite = useCanWrite();
  const [search, setSearch] = useState(''), [offset, setOffset] = useState(0);
  const {value, error, refresh} = useLive<ManufacturingData>(`/manufacturing?search=${encodeURIComponent(search)}&offset=${offset}`);
  const [form, setForm] = useState({brand: '', model: '', color: '', quantity: '1', rejected: '0'});
  const [entryId, setRecordId] = useState(recordId);
  const [stamp, setStamp] = useState(() => new Date(Date.now()+5*3600000).toISOString().slice(0,19));
  const [busy, setBusy] = useState(false), [saveError, setSaveError] = useState(''), [message, setMessage] = useState('');
  async function save(e: FormEvent) {
    e.preventDefault(); setBusy(true); setSaveError(''); setMessage('');
    try {
      const result = await post<{message: string}>('/quality/records', {records: [{...form, quantity: Number(form.quantity), rejected: Number(form.rejected), record_id: entryId, timestamp: new Date(stamp+'+05:00').toISOString()}]});
      setMessage(result.message); setRecordId(recordId()); setStamp(new Date(Date.now()+5*3600000).toISOString().slice(0,19)); refresh();
    } catch(e) {setSaveError((e as Error).message);} finally {setBusy(false);}
  }
  const q = value?.quality, report = value?.report;
  return <div className="manufacturing-page">
    {error && <p className="connection-banner" role="alert">{error}</p>}
    <div className="manufacturing-metrics"><Metric label="Проверено автомобилей" value={q?.quantity} unit="шт."/><Metric label="Принято" value={q?.accepted} unit="шт."/><Metric label="Брак" value={q?.rejected} unit="шт."/><Metric label="Доля брака" value={q?.quantity ? q.rejected/q.quantity*100 : null} unit="%"/></div>
    <section className="panel"><div className="panel-heading"><div><span className="eyebrow">Проверенные партии</span><h2>Марка, модель, цвет и количество</h2></div><a href="/api/quality/export" className="button secondary" download>Экспорт всех записей</a></div>
      <label className="robot-search">Поиск по марке, модели или цвету<input type="search" value={search} onChange={e => {setSearch(e.target.value); setOffset(0);}}/></label>
      {q?.groups.length ? <div className="robot-table"><table><thead><tr><th>Марка</th><th>Модель</th><th>Цвет</th><th>Проверено</th><th>Принято</th><th>Брак</th></tr></thead><tbody>{q.groups.map(r => <tr key={`${r.brand}:${r.model}:${r.color}`}><td>{r.brand}</td><td>{r.model}</td><td>{r.color}</td><td>{number(r.quantity)}</td><td>{number(r.quantity-r.rejected)}</td><td>{number(r.rejected)}</td></tr>)}</tbody></table><p className="small-note">До 100 групп для выбранного поиска. Счётчики выше учитывают все найденные записи. Выгрузка содержит весь журнал.</p></div> : <p className="intro">Записей по автомобилям пока нет. Суточный отчёт из кейса не содержит марку, модель и цвет проверенных машин. Добавьте реальные записи ниже или загрузите CSV.</p>}
      <details><summary>Журнал проверок · {number(q?.total)} записей</summary><div className="robot-table"><table><thead><tr><th>Время</th><th>Марка / модель</th><th>Цвет</th><th>Количество</th><th>Брак</th><th>ID записи</th></tr></thead><tbody>{q?.rows.map(r => <tr key={r.record_id}><td>{date(r.timestamp)}</td><td>{r.brand} {r.model}</td><td>{r.color}</td><td>{r.quantity}</td><td>{r.rejected}</td><td>{r.record_id}</td></tr>)}</tbody></table></div><div className="robot-actions"><button className="button secondary" disabled={!offset} onClick={() => setOffset(v => Math.max(0,v-100))}>Предыдущие записи</button><button className="button secondary" disabled={!q || offset+100>=q.total} onClick={() => setOffset(v => v+100)}>Следующие записи</button></div></details>
    </section>
    <section className="panel"><h2>Зарегистрировать контроль качества</h2><p className="intro">Количество — размер проверенной партии, включая брак. Одна запись учитывается один раз, даже при повторе запроса.</p>
      <form onSubmit={save} className="quality-form">{([['brand','Марка авто'],['model','Модель авто'],['color','Цвет авто']] as const).map(([key,label]) => <label key={key}>{label}<input required maxLength={key==='model'?120:80} disabled={!canWrite || busy} value={form[key]} onChange={e => setForm({...form,[key]:e.target.value})}/></label>)}
        <label>Время проверки (UTC+5)<input type="datetime-local" step="1" required value={stamp} disabled={!canWrite||busy} onChange={e=>setStamp(e.target.value)}/></label>
        <label>Количество автомобилей<input type="number" min="1" max="1000000" required disabled={!canWrite || busy} value={form.quantity} onChange={e => setForm({...form,quantity:e.target.value})}/></label><label>Количество брака<input type="number" min="0" max={form.quantity} required disabled={!canWrite || busy} value={form.rejected} onChange={e => setForm({...form,rejected:e.target.value})}/></label>
        <button className="button primary" disabled={!canWrite || busy}>{busy?'Сохраняем…':'Сохранить проверку'}</button>
      </form>{saveError && <p role="alert" className="inline-error">{saveError}</p>}{message && <p role="status" className="inline-success">{message}</p>}
    </section>
    <ReportImport onSaved={refresh}/>
    {report && <><section className="panel"><div className="panel-heading"><div><span className="eyebrow">Суточные данные · {report.filename}</span><h2>Качество по участкам</h2></div></div><p className="intro">Один автомобиль проходит несколько участков. Значения разных участков не складываются в общий выпуск автомобилей.</p><div className="robot-table"><table><thead><tr><th>Дата</th><th>Участок</th><th>Выпущено</th><th>Брак</th><th>Доля брака</th><th>Целевой предел</th></tr></thead><tbody>{report.quality.map((r,i) => <tr key={i}><td>{r.date}</td><td>{r.section}</td><td>{r.quantity}</td><td>{r.rejected}</td><td className={r.calculated_percent!==null && r.calculated_percent>report.targets.defect_max_percent?'alarm-text':''}>{number(r.calculated_percent,2)}%</td><td>{number(report.targets.defect_max_percent)}%</td></tr>)}</tbody></table></div></section>
      <section className="panel"><h2>План по моделям автомобилей</h2><div className="robot-table"><table><thead><tr><th>Модель из документа</th><th>План на месяц</th><th>Цвет</th><th>Фактически проверено по модели</th></tr></thead><tbody>{report.plans.map(r => <tr key={r.vehicle}><td>{r.vehicle}</td><td>{number(r.quantity)}</td><td>В документе не указан</td><td>В документе не указано</td></tr>)}</tbody></table></div><p className="intro">Сумма по моделям: {number(report.planned_total)}. Общий ориентир: {number(report.targets.monthly_min_quantity)}. Расхождение: {number(report.plan_gap)}.</p>{report.warnings.map(w => <p className="report-warning" key={w}>{w}</p>)}</section></>}
  </div>;
}

export function Metric({label,value,unit}: {label:string; value:unknown; unit:string}) {return <article className="panel manufacturing-metric"><span>{label}</span><strong>{number(value,1)} <small>{unit}</small></strong></article>;}
