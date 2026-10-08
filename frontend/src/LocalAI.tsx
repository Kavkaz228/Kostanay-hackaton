import {useEffect, useRef, useState, type FormEvent} from 'react';
import {post, number, request} from './api';
import {useCanWrite} from './permissions';
import {useLive} from './Manufacturing';
import {Icon} from './Icons';
import type {AutomationData} from './Automation';
import './local-ai.css';

type Source = 'plant' | 'emulation' | 'simulation';
type Severity = 'critical' | 'warning' | 'info';
type AIStatus = {ready: boolean; model: string; local_only: boolean; message: string};
type Facts = {
  data_source: Source; source_label: string; captured_at: string; observed_at: string | null; has_data: boolean; notice: string;
  metrics: {key: string; label: string; value: number | string | null; unit?: string; detail?: string}[];
  readings: {id: string; asset: string; label: string; value: number | string | null; unit?: string; observed_at?: string | null; status?: string}[];
  findings: {id: string; title: string; severity: Severity; evidence: string; recommendation: string}[];
  missing_data: string[]; next_steps: string[];
};
type Recommendation = {title: string; reason: string; steps: string[]; priority: Severity; evidence_ids: string[]; origin: 'model' | 'rules'};
type AIAnswer = {model: string; analysis: string; action: string; reason: string; data_source?: Source; proposal: {id: string; robot_id: string} | null; facts: Facts; recommendations: Recommendation[]; answer_kind: 'model' | 'data_required'};

const sourceLabels: Record<Source, string> = {plant: 'Фактические данные', simulation: 'Симуляция линии', emulation: 'Учебный стенд SCADA'};
const questions: Record<Source, string> = {
  plant: 'Назови подтверждённые проблемы в показаниях оборудования. Для каждой предложи конкретные проверки и объясни, на каких данных основан совет.',
  simulation: 'Проанализируй выпуск, выполнение плана, качество и простои симуляции линии. Назови главное ограничение и предложи конкретные действия для проверки в модели.',
  emulation: 'Какие узлы учебного стенда требуют внимания? Приведи конкретные показания, предложи порядок проверок и укажи, какие запчасти могут понадобиться.',
};
const suggestions: Record<Source, {label: string; question: string}[]> = {
  plant: [{label: 'Приоритетные проблемы', question: questions.plant}, {label: 'Обслуживание и запчасти', question: 'Какие узлы требуют обслуживания? Укажи показания, дефицит запчастей и следующие действия сотрудника.'}],
  simulation: [{label: 'Почему не выполняется план?', question: 'Сравни текущий выпуск и план симуляции линии. Покажи, какие участки и простои ограничивают выпуск, и предложи проверки в модели.'}, {label: 'Качество и потери', question: 'Покажи фактические числа брака и простоев в симуляции, определи основные потери и предложи действия для проверки.'}],
  emulation: [{label: 'Аварии и показания', question: questions.emulation}, {label: 'Ресурс и запчасти', question: 'Какие конкретные узлы учебного стенда требуют обслуживания и каких деталей не хватает? Назови показания, артикулы и порядок проверки.'}],
};
const priorities: Record<Severity, string> = {critical: 'В первую очередь', warning: 'Требует внимания', info: 'Плановая проверка'};
const stateLabels: Record<string, string> = {ok: 'В норме', normal: 'В норме', fresh: 'Свежие данные', stale: 'Данные устарели', warn: 'Предупреждение', warning: 'Предупреждение', alarm: 'Авария', critical: 'Критично', fault: 'Неисправность', error: 'Ошибка', running: 'Работает', stopped: 'Остановлен', idle: 'Простой', unknown: 'Неизвестно', missing: 'Нет данных'};

function stamp(value?: string | null) {
  if (!value || Number.isNaN(new Date(value).getTime())) return 'Не передано';
  return new Date(value).toLocaleString('ru-RU', {timeZone: 'Asia/Qyzylorda', day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'}) + ' · UTC+5';
}
function displayValue(value: number | string | null) {return typeof value === 'number' ? number(value, 2) : value ?? '—';}

function FactsPanel({facts, frozen}: {facts: Facts; frozen: boolean}) {
  const [expanded, setExpanded] = useState(false);
  const [allFindings, setAllFindings] = useState(false);
  const readings = expanded ? facts.readings : facts.readings.slice(0, 8);
  const findings = allFindings ? facts.findings : facts.findings.slice(0, 4);
  return <section className="panel ai-facts" data-testid="ai-context" aria-label={facts.data_source === 'emulation' ? 'Данные эмуляции для ИИ' : 'Данные для анализа'}>
    <div className="ai-section-heading"><div><span className="eyebrow">{frozen ? 'Данные, на которых основан ответ' : 'Показания до анализа'}</span><h2>{facts.source_label || sourceLabels[facts.data_source]}</h2></div><span className={`ai-source-tag ${facts.data_source}`}><Icon name={facts.data_source === 'plant' ? 'database' : 'flask'} size={14}/>{facts.data_source === 'plant' ? 'Учёт и телеметрия' : 'Расчётные данные'}</span></div>
    <p className="ai-notice">{facts.notice}</p>
    <div className="ai-snapshot-time"><span><Icon name="clock" size={13}/>{facts.data_source === 'plant' ? 'Последнее наблюдение' : 'Время модели'}: <strong>{stamp(facts.observed_at)}</strong></span><span>{frozen ? 'Снимок для ответа' : 'Снимок получен'}: {stamp(facts.captured_at)}</span></div>
    {!!facts.metrics.length && <div className="ai-metrics" aria-label="Основные показатели">{facts.metrics.map(metric => <div className="ai-metric" key={metric.key}><span>{metric.label}</span><strong>{displayValue(metric.value)}{metric.unit && <small> {metric.unit}</small>}</strong>{metric.detail && <p>{metric.detail}</p>}</div>)}</div>}
    {!!facts.readings.length && <div className="ai-reading-section"><div className="ai-subheading"><h3>Показания и расчёты</h3><span>{facts.readings.length} записей в снимке</span></div><div className="robot-table ai-readings" data-testid="ai-readings"><table><thead><tr><th>Оборудование / объект</th><th>Показатель</th><th>Значение</th><th>Состояние / время</th></tr></thead><tbody>{readings.map(reading => <tr key={reading.id} data-testid={`ai-reading-${reading.id}`}><td>{reading.asset}</td><td>{reading.label}</td><td><strong>{displayValue(reading.value)}</strong>{reading.unit && <span> {reading.unit}</span>}</td><td>{reading.status && <span className={`ai-reading-state ${reading.status}`}>{stateLabels[reading.status] || reading.status}</span>}{reading.observed_at && <small>{stamp(reading.observed_at)}</small>}{!reading.status && !reading.observed_at && '—'}</td></tr>)}</tbody></table></div>{facts.readings.length > 8 && <button type="button" className="button tertiary compact ai-expand" onClick={() => setExpanded(value => !value)}>{expanded ? 'Свернуть показания' : `Все показания (${facts.readings.length})`}<Icon name="chevron" size={14}/></button>}</div>}
    {!!facts.findings.length && <div className="ai-finding-section"><div className="ai-subheading"><h3>Выявлено по данным</h3><span>{facts.findings.length} наблюдений</span></div><div className="ai-findings">{findings.map(finding => <article className={`ai-finding ${finding.severity}`} key={finding.id} id={`ai-finding-${finding.id}`}><span className="ai-priority"><Icon name={finding.severity === 'info' ? 'info' : 'warning'} size={14}/>{priorities[finding.severity]}</span><h4>{finding.title}</h4><p>{finding.evidence}</p>{finding.recommendation && <div className="ai-rule-advice"><strong>Следующий шаг</strong><p>{finding.recommendation}</p></div>}</article>)}</div>{facts.findings.length > 4 && <button type="button" className="button tertiary compact ai-expand" onClick={() => setAllFindings(value => !value)}>{allFindings ? 'Свернуть наблюдения' : `Все наблюдения (${facts.findings.length})`}</button>}</div>}
    {facts.has_data && !facts.findings.length && <p className="ai-no-findings"><Icon name="info" size={16}/>В этом снимке сервер не выделил отклонений. Доступные показатели можно обсудить с ИИ.</p>}
    {!facts.has_data && !!facts.next_steps.length && <div className="ai-connection-steps"><h3>Следующие шаги</h3><ol>{facts.next_steps.map((step, index) => <li key={index}>{step}</li>)}</ol></div>}
    {!!facts.missing_data.length && <details className="ai-data-gaps" open={!facts.has_data}><summary>Что ограничивает анализ · {facts.missing_data.length}</summary><ul>{facts.missing_data.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}
  </section>;
}

function AnswerPanel({answer}: {answer: AIAnswer}) {
  return <article className="panel ai-answer ai-structured-answer" data-testid="ai-answer" aria-label="Ответ локального ИИ">
    <div className="ai-section-heading"><div><span className="eyebrow">{answer.answer_kind === 'data_required' ? 'Подключение данных' : `${answer.model} · локальный ответ`}</span><h2>{answer.answer_kind === 'data_required' ? 'Как получить предметный анализ' : 'Вывод и рекомендации'}</h2></div><Icon name={answer.answer_kind === 'data_required' ? 'database' : 'bolt'} size={22}/></div>
    <p className="ai-analysis">{answer.analysis}</p>
    {!!answer.recommendations?.length && <div className="ai-recommendations" data-testid="ai-recommendations">{answer.recommendations.map((recommendation, index) => <section className={`ai-recommendation ${recommendation.priority}`} key={`${index}-${recommendation.title}`}><div className="ai-recommendation-number">{String(index + 1).padStart(2, '0')}</div><div className="ai-recommendation-body"><div className="ai-recommendation-labels"><span className="ai-priority">{priorities[recommendation.priority]}</span><span>{recommendation.origin === 'rules' ? 'По правилам анализа' : 'Предложение ИИ'}</span></div><h3>{recommendation.title}</h3><p>{recommendation.reason}</p>{!!recommendation.steps.length && <ol>{recommendation.steps.map((step, stepIndex) => <li key={stepIndex}>{step}</li>)}</ol>}</div></section>)}</div>}
    {!answer.recommendations?.length && !!answer.facts?.next_steps.length && <ol className="ai-next-steps">{answer.facts.next_steps.map((step, index) => <li key={index}>{step}</li>)}</ol>}
    {answer.proposal ? <p className="report-warning">Создано предложение {answer.proposal.id} для {answer.proposal.robot_id}. Оно ещё не отправлено оборудованию. Проверьте его в разделе «Автоматизация».</p> : <p className="ai-command-note"><Icon name="info" size={14}/>Команды оборудованию не создавались.</p>}
  </article>;
}

export function LocalAI() {
  const canWrite = useCanWrite();
  const status = useLive<AIStatus>('/ai/status', 15000), robots = useLive<AutomationData>('/automation', 10000);
  const [source, setSource] = useState<Source>('plant');
  const [question, setQuestion] = useState(questions.plant);
  const [robot, setRobot] = useState(''), [allowCommand, setAllowCommand] = useState(false);
  const [answer, setAnswer] = useState<AIAnswer | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [preview, setPreview] = useState<Facts | null>(null), [previewError, setPreviewError] = useState(''), [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const version = useRef(0);
  const activeRequest = useRef<AbortController | null>(null);

  useEffect(() => {
    const revision = ++version.current;
    const controller = new AbortController();
    activeRequest.current = controller;
    setPreview(null); setPreviewError(''); setLoading(true);
    request<Facts>(`/ai/context?data_source=${source}${robot ? `&robot_id=${encodeURIComponent(robot)}` : ''}`, {signal: controller.signal})
      .then(value => {if (!controller.signal.aborted && revision === version.current) setPreview(value);})
      .catch(cause => {if (!controller.signal.aborted && revision === version.current) setPreviewError((cause as Error).message);})
      .finally(() => {if (!controller.signal.aborted && revision === version.current) setLoading(false);});
    return () => {controller.abort();};
  }, [source, robot, refresh]);

  function clearSelection() {
    ++version.current; activeRequest.current?.abort(); setPreview(null); setLoading(true); setPreviewError(''); setAnswer(null); setError(''); setAllowCommand(false);
  }
  function changeSource(next: Source) {if (next === source) return; clearSelection(); setSource(next); setRobot(''); setQuestion(questions[next]);}
  function updatePreview() {clearSelection(); setRefresh(value => value + 1);}
  async function analyze(event: FormEvent) {
    event.preventDefault(); if (!preview || busy) return;
    const revision = version.current;
    setBusy(true); setError(''); setAnswer(null);
    try {
      const result = await post<AIAnswer>('/ai/analyze', {question, robot_id: source === 'simulation' ? null : robot || null, allow_command: source === 'plant' && allowCommand, data_source: source});
      if (revision === version.current) setAnswer(result);
    } catch (cause) {if (revision === version.current) setError((cause as Error).message);}
    finally {setBusy(false);}
  }
  const facts = answer?.facts || preview;
  const empty = preview?.has_data === false;
  return <div className="manufacturing-page local-ai-page">
    <section className="panel ai-workspace">
      <div className="ai-section-heading"><div><span className="eyebrow">Локальный инженерный помощник</span><h2>От показаний к следующему действию</h2></div><span className={`connection-tag ${status.value?.ready ? 'fresh' : ''}`}>{status.value?.model || 'Локальная модель'} · {status.value?.ready ? 'готова' : 'не готова'}</span></div>
      <p className="ai-intro">Выберите источник, проверьте показания и задайте вопрос. ИИ анализирует выбранные данные и предлагает сотруднику порядок проверок.</p>
      <div className="ai-source-controls">
        <label>Источник данных ИИ<select value={source} disabled={busy} onChange={event => changeSource(event.target.value as Source)}><option data-testid="ai-source-plant" value="plant">Фактические данные</option><option data-testid="ai-source-simulation" value="simulation">Симуляция линии</option><option data-testid="ai-source-emulation" value="emulation">Учебный стенд SCADA</option></select></label>
        {source !== 'simulation' ? <label>Робот для анализа<select value={robot} disabled={busy} onChange={event => {clearSelection(); setRobot(event.target.value);}}><option value="">{source === 'emulation' ? 'Весь учебный стенд' : 'Общий анализ производства'}</option>{source === 'emulation' ? ['R1', 'R2', 'R3', 'R4', 'CV'].map(id => <option key={id} value={id}>{id === 'CV' ? 'CV · Учебный конвейер' : `${id} · Учебный робот`}</option>) : robots.value?.robots.map(item => <option key={item.robot_id} value={item.robot_id}>{item.robot_id} · {item.line_section}</option>)}</select></label> : <div className="ai-source-description"><Icon name="factory" size={20}/><span>Вся производственная линия<small>Выпуск, план, качество и состояние участков модели</small></span></div>}
        <button type="button" className="button secondary compact" disabled={busy || loading} onClick={updatePreview}><Icon name="refresh" size={15}/>Обновить показания</button>
      </div>
      {source === 'emulation' && <p className="ai-mode-note"><Icon name="flask" size={16}/>Учебный режим: показания рассчитаны эмулятором, поставщики и цены вымышлены.</p>}
      {source === 'simulation' && <p className="ai-mode-note"><Icon name="flask" size={16}/>Симуляция линии: выпуск, качество и простои рассчитаны производственной моделью.</p>}
      {empty && source === 'plant' && <div className="ai-empty-source" role="status"><div className="ai-empty-heading"><Icon name="database" size={23}/><div><strong>Фактических данных для анализа пока нет</strong><p>Подключите показания оборудования или выберите работающую модель проекта. Её показатели будут явно отмечены как расчётные.</p></div></div><div className="ai-empty-actions"><button type="button" className="button primary" data-testid="ai-select-simulation" disabled={busy} onClick={() => changeSource('simulation')}><Icon name="activity" size={16}/>Данные симуляции линии</button><button type="button" className="button secondary" data-testid="ai-select-emulation" disabled={busy} onClick={() => changeSource('emulation')}><Icon name="flask" size={16}/>Данные учебного стенда</button></div></div>}
      {loading && <p className="ai-preview-loading" role="status">Получаем показания выбранного источника…</p>}
      {previewError && <p className="inline-error" role="alert">{previewError}</p>}
      {source === 'plant' && robots.error && <p className="inline-error" role="alert">Не удалось получить список роботов: {robots.error}</p>}
    </section>

    {facts && <FactsPanel key={`${source}-${robot}-${answer ? 'answer' : 'preview'}`} facts={facts} frozen={Boolean(answer?.facts)}/>}

    <section className="panel ai-question-panel"><div className="ai-section-heading"><div><span className="eyebrow">Вопрос по выбранным данным</span><h2>{empty ? 'Подготовить подключение' : 'Что нужно проверить?'}</h2></div><Icon name="bolt" size={20}/></div>
      <div className="ai-question-suggestions">{suggestions[source].map(suggestion => <button type="button" className="button secondary compact" key={suggestion.label} disabled={!canWrite || busy} onClick={() => setQuestion(suggestion.question)}>{suggestion.label}<Icon name="arrow" size={13}/></button>)}</div>
      <form className="ai-form" onSubmit={event => void analyze(event)}>
        <label>Вопрос локальному ИИ<textarea rows={3} required minLength={3} maxLength={2000} value={question} onChange={event => setQuestion(event.target.value)} disabled={!canWrite || busy}/></label>
        {source === 'plant' && <label className="ai-permission"><input type="checkbox" checked={allowCommand} disabled={!robot || !canWrite || busy || empty} onChange={event => setAllowCommand(event.target.checked)}/>Разрешаю подготовить предложение команды выбранному роботу</label>}
        <div className="ai-submit-row"><button className="button primary" data-testid="ai-analyze" disabled={!canWrite || busy || !preview || (!empty && !status.value?.ready)}><Icon name={empty ? 'database' : 'bolt'} size={16}/>{busy ? empty ? 'Готовим шаги подключения…' : 'Модель анализирует данные…' : empty ? 'Как подключить данные' : 'Выполнить локальный анализ'}</button><span>{empty ? 'Шаги подключения доступны без запуска модели' : 'Ответ по текущему снимку · до 150 секунд'}</span></div>
      </form>
      <p className={`ai-model-status ${!status.value?.ready ? 'unavailable' : ''}`}>{status.value?.message || 'Проверяем локальную модель…'}{!status.value?.ready && facts?.has_data && <span> Показания и рекомендации по правилам доступны выше.</span>}</p>
      {busy && <p className="ai-working" role="status"><span/>Читаем выбранные данные и готовим рекомендации сотруднику…</p>}
      {(error || status.error) && <p role="alert" className="inline-error">{error || status.error}</p>}
    </section>

    {answer && <AnswerPanel answer={answer}/>}
    <details className="panel ai-boundaries"><summary>Как используются рекомендации</summary><p>Показания и выявленные отклонения рассчитывает сервер. ИИ предлагает объяснения и проверки; его вывод нужно сверять с измерениями и регламентом оборудования.</p><p>Данные остаются в этой установке. Предложение команды физическому роботу создаётся только с выбранным разрешением и требует отдельного подтверждения в разделе «Автоматизация». Для симуляции и учебного стенда физические команды недоступны.</p></details>
  </div>;
}
