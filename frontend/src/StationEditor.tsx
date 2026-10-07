import {useCanWrite} from './permissions';
import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import type { Station, TwinState } from './types';
import { elapsed, number, request } from './api';
import { Icon } from './Icons';

export const statusLabels = {running: 'Работает', stopped: 'Остановлен', starved: 'Ожидает подачу', blocked: 'Буфер заполнен'};
export function StationEditor({station, telemetry, onClose, onSaved}: {station: Station; telemetry: boolean; onClose: () => void; onSaved: (state: TwinState) => void}) {
  const canWrite = useCanWrite();
  const [cycle, setCycle] = useState(station.cycle_seconds);
  const [capacity, setCapacity] = useState(station.capacity);
  const [buffer, setBuffer] = useState(station.buffer_capacity);
  const [defect, setDefect] = useState(station.defect_rate * 100);
  const [speed, setSpeed] = useState(station.speed_factor);
  const [stopped, setStopped] = useState(station.manual_stop);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy) onClose();
      if (event.key === 'Tab') {
        const elements = panelRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href]');
        if (!elements?.length) return;
        const first = elements[0], last = elements[elements.length - 1];
        if (event.shiftKey && document.activeElement === first) {event.preventDefault(); last.focus();}
        if (!event.shiftKey && document.activeElement === last) {event.preventDefault(); first.focus();}
      }
    };
    document.addEventListener('keydown', handleKey);
    return () => {document.removeEventListener('keydown', handleKey); previous?.focus();};
  }, [onClose, busy]);
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const state = await request<TwinState>(`/stations/${encodeURIComponent(station.id)}`, {method: 'PATCH', body: JSON.stringify({cycle_seconds: cycle, capacity, buffer_capacity: buffer, defect_rate: defect / 100, speed_factor: speed, manual_stop: stopped})});
      onSaved(state); onClose();
    } catch (error) {setError(error instanceof Error ? error.message : 'Не удалось сохранить участок');} finally {setBusy(false);}
  }
  return <div className="modal-backdrop" onClick={() => !busy && onClose()}><aside className="station-drawer" ref={panelRef} role="dialog" aria-modal="true" aria-labelledby="station-editor-title" onClick={event => event.stopPropagation()}>
    <div className="drawer-heading"><div><div className="eyebrow">Параметры участка · {station.id}</div><h2 id="station-editor-title">{station.name}</h2></div><button className="icon-button" ref={closeRef} aria-label="Закрыть параметры участка" onClick={onClose} disabled={!canWrite || busy}><Icon name="close"/></button></div>
    <span className={`status-pill ${station.status}`}><i/>{statusLabels[station.status] ?? station.status}</span>
    <div className="drawer-stats"><div><span>Обработано</span><strong>{number(station.completed)}</strong></div><div><span>В очереди</span><strong>{number(station.queue)}</strong></div><div><span>Простой</span><strong>{elapsed(station.downtime_seconds)}</strong></div></div>
    <div className="info-note"><Icon name="info" size={18}/><p>{telemetry ? 'Показаны данные импортированной телеметрии. Для изменения модели переключитесь на симуляцию.' : 'Изменения применятся к текущей модели сразу. Для проверки без влияния на линию используйте «Сценарии».'}</p></div>
    <form onSubmit={save}><fieldset disabled={!canWrite || busy || telemetry} className="form-fieldset"><div className="form-grid">
      <label>Время цикла, сек<input data-testid="station-cycle" type="number" min="5" max="600" step="1" required value={cycle} onChange={event => setCycle(Number(event.target.value))}/><small>Обработка одной единицы</small></label>
      <label>Рабочие места<input data-testid="station-capacity" type="number" min="1" max="10" step="1" required value={capacity} onChange={event => setCapacity(Number(event.target.value))}/><small>Параллельная обработка</small></label>
      <label>Вместимость буфера<input data-testid="station-buffer" type="number" min="1" max="200" step="1" required value={buffer} onChange={event => setBuffer(Number(event.target.value))}/><small>Максимум единиц в очереди</small></label>
      <label>Вероятность брака, %<input data-testid="station-defect" type="number" min="0" max="50" step="0.1" required value={defect} onChange={event => setDefect(Number(event.target.value))}/><small>Для каждой завершённой операции</small></label>
      <label className="span-full">Коэффициент скорости<input data-testid="station-speed" type="number" min="0.1" max="2" step="0.1" required value={speed} onChange={event => setSpeed(Number(event.target.value))}/><small>1 — обычный режим, 0,5 — вдвое медленнее</small></label>
    </div><label className="switch-row"><span><strong>Остановить участок</strong><small>Подача на соседние участки изменится</small></span><input data-testid="station-stop" type="checkbox" checked={stopped} onChange={event => setStopped(event.target.checked)}/><span className="switch-track" aria-hidden="true"/></label></fieldset>
    {error && <div className="inline-error" role="alert">{error}</div>}
    <div className="drawer-actions"><button type="button" className="button secondary" onClick={onClose} disabled={!canWrite || busy}>Закрыть</button><button data-testid="station-save" type="submit" className="button primary" disabled={!canWrite || busy || telemetry}>{busy ? 'Сохраняем…' : 'Применить изменения'}<Icon name="check" size={17}/></button></div></form>
  </aside></div>;
}
