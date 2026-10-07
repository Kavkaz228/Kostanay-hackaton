import {useCanWrite} from './permissions';
import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { request } from './api';

export type LimitName = 'low_critical' | 'low_warning' | 'high_warning' | 'high_critical';
export interface RobotConfig {
  expected_interval_seconds: number;
  limits: Record<string, Partial<Record<LimitName, number | null>>>;
  error_codes: Record<string, {description: string; action: string; severity: 'warning' | 'critical'}>;
}
const bounds: {key: LimitName; title: string}[] = [
  {key: 'low_critical', title: 'Критическая нижняя'}, {key: 'low_warning', title: 'Нижняя'},
  {key: 'high_warning', title: 'Верхняя'}, {key: 'high_critical', title: 'Критическая верхняя'},
];

export function RobotSettings({robotId, sensors, onSaved}: {robotId: string; sensors: {key: string; label: string; unit: string}[]; onSaved: (value: RobotConfig) => void}) {
  const canWrite = useCanWrite();
  const [config, setConfig] = useState<RobotConfig | null>(null);
  const [codes, setCodes] = useState<{code: string; description: string; action: string; severity: 'warning' | 'critical'}[]>([]);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [success, setSuccess] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    void request<RobotConfig>(`/robots/config?robot_id=${encodeURIComponent(robotId)}`, {signal: controller.signal})
      .then(value => {if (!controller.signal.aborted) {setConfig(value); setCodes(Object.entries(value.error_codes).map(([code, definition]) => ({code, ...definition})));}})
      .catch(e => {if (!controller.signal.aborted) setError(String(e));});
    return () => controller.abort();
  }, [robotId]);
  async function save(event: FormEvent) {
    event.preventDefault(); if (!config) return;
    setBusy(true); setError(''); setSuccess('');
    try {
      if (new Set(codes.map(c => c.code.trim())).size !== codes.length) throw new Error('Коды ошибок не должны повторяться.');
      const body = {...config, error_codes: Object.fromEntries(codes.map(({code, ...definition}) => [code.trim(), definition]))};
      const value = await request<RobotConfig>(`/robots/config?robot_id=${encodeURIComponent(robotId)}`, {method: 'PUT', body: JSON.stringify(body)});
      setConfig(value); onSaved(value); setSuccess('Настройки сохранены. Предупреждения пересчитаны по последним измерениям.');
    } catch (e) {setError(e instanceof Error ? e.message : String(e));} finally {setBusy(false);}
  }
  return <details className="robot-settings csv-details"><summary>Пороги и справочник ошибок · {robotId}</summary>
    {error && <p className="inline-error" role="alert">{error}</p>}
    {!config ? <p>Загрузка настроек…</p> : <form onSubmit={save}><fieldset disabled={!canWrite || busy} className="form-fieldset">
      <p className="intro">Введите границы из регламента оборудования. Пустое поле отключает соответствующую проверку. Превышения проверяются включительно; код ошибки можно расшифровать по справочнику производителя.</p>
      <label>Ожидаемый интервал измерений, секунд<input aria-label="Ожидаемый интервал измерений" type="number" min="1" max="86400" required value={config.expected_interval_seconds} onChange={e => setConfig({...config, expected_interval_seconds: Number(e.target.value)})}/></label>
      <div className="robot-table"><table><thead><tr><th>Датчик</th>{bounds.map(b => <th key={b.key}>{b.title}</th>)}</tr></thead><tbody>{sensors.map(s => <tr key={s.key}><th>{s.label}<small> · {s.unit}</small></th>{bounds.map(b => <td key={b.key} data-label={b.title}><input aria-label={`${s.label}: ${b.title}`} type="number" step="any" min={s.key === 'joint_temperature_c' ? '-273.15' : '0'} max="1000000000" value={config.limits[s.key]?.[b.key] ?? ''} onChange={e => setConfig({...config, limits: {...config.limits, [s.key]: {...config.limits[s.key], [b.key]: e.target.value === '' ? null : Number(e.target.value)}}})}/></td>)}</tr>)}</tbody></table></div>
      <h3>Справочник кодов</h3>
      {codes.map((c, index) => <div className="robot-code-editor" key={index}>
        <label>Код<input aria-label={`Код ошибки ${index + 1}`} required maxLength={128} value={c.code} onChange={e => setCodes(values => values.map((v, i) => i === index ? {...v, code: e.target.value} : v))}/></label>
        <label>Описание<input aria-label={`Описание ошибки ${index + 1}`} required maxLength={500} value={c.description} onChange={e => setCodes(values => values.map((v, i) => i === index ? {...v, description: e.target.value} : v))}/></label>
        <label>Действие<input aria-label={`Действие при ошибке ${index + 1}`} maxLength={500} value={c.action} onChange={e => setCodes(values => values.map((v, i) => i === index ? {...v, action: e.target.value} : v))}/></label>
        <label>Уровень<select aria-label={`Уровень ошибки ${index + 1}`} value={c.severity} onChange={e => setCodes(values => values.map((v, i) => i === index ? {...v, severity: e.target.value as 'warning' | 'critical'} : v))}><option value="warning">Предупреждение</option><option value="critical">Критический</option></select></label>
        <button type="button" className="button secondary" onClick={() => setCodes(values => values.filter((_, i) => i !== index))}>Удалить код {c.code || index + 1}</button>
      </div>)}
      <div className="robot-actions"><button type="button" className="button secondary" disabled={codes.length >= 100} onClick={() => setCodes(values => [...values, {code: '', description: '', action: '', severity: 'warning'}])}>Добавить код ошибки</button><button className="button primary" type="submit">{busy ? 'Сохраняем…' : 'Сохранить настройки робота'}</button></div>
    </fieldset></form>}
    {success && <p className="inline-success" role="status">{success}</p>}
  </details>;
}

