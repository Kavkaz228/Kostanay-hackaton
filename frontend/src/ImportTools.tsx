import {useCanWrite} from './permissions';
import { useEffect, useState } from 'react';
import { date, number, request } from './api';

interface Preview {kind: string; count: number; identifiers: string[]; start: string; end: string; preview: Record<string, unknown>[]; warnings?:string[]}
const kinds:Record<string,string> = {robots:'Роботы',production:'Участки',case:'Отчёт кейса',quality:'Контроль качества'};
export function ImportPreview({file}: {file: File | null}) {
  const canWrite = useCanWrite();
  const [value, setValue] = useState<Preview | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  useEffect(() => {setValue(null); setError('');}, [file]);
  return <div className="import-preview"><button type="button" className="button secondary" disabled={!canWrite || !file || busy} onClick={async () => {
    if (!file) return; setBusy(true); setValue(null); setError('');
    const data = new FormData(); data.append('file', file);
    try {setValue(await request<Preview>('/import/preview', {method: 'POST', body: data}));}
    catch (e) {setError(e instanceof Error ? e.message : String(e));} finally {setBusy(false);}
  }}>{busy ? 'Проверяем файл…' : 'Проверить без сохранения'}</button>
    {error && <p role="alert" className="inline-error">{error}</p>}
    {value && <div className="preview-result"><h3>Проверка пройдена · {kinds[value.kind] ?? value.kind}</h3><p>{number(value.count)} строк · {number(value.identifiers.length)} объектов · {date(value.start)} — {date(value.end)}. Данные ещё не сохранены.</p><div className="robot-table"><table><thead><tr>{Object.keys(value.preview[0] ?? {}).map(k => <th key={k}>{k}</th>)}</tr></thead><tbody>{value.preview.map((r, i) => <tr key={i}>{Object.entries(r).map(([k, v]) => <td key={k}>{v === null ? '—' : String(v)}</td>)}</tr>)}</tbody></table></div>{value.warnings?.map(w => <p className="report-warning" key={w}>{w}</p>)}<p className="small-note">Первые {value.preview.length} строк. При импорте файл проверяется повторно.</p></div>}
  </div>;
}

export function ImportLog({revision}: {revision: string}) {
  const [rows, setRows] = useState<{id: string; filename: string; count: number; kind: string; imported_at: string}[]>([]), [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    void request<typeof rows>('/imports').then(value => {if (alive) {setRows(value); setError('');}}).catch(e => {if (alive) setError(String(e));});
    return () => {alive = false;};
  }, [revision]);
  return <section className="panel"><div className="panel-heading"><h2>Журнал загрузок</h2></div>{error && <p role="alert">{error}</p>}{rows.length ? <div className="robot-table"><table><thead><tr><th>Загружено</th><th>Файл / источник</th><th>Тип</th><th>Строк</th></tr></thead><tbody>{rows.map(r => <tr key={r.id}><td>{date(r.imported_at)}</td><td>{r.filename}</td><td>{kinds[r.kind] ?? r.kind}</td><td>{number(r.count)}</td></tr>)}</tbody></table></div> : <p className="muted">Новые успешные загрузки будут записаны здесь.</p>}</section>;
}

