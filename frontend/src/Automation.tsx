import {useEffect, useState} from 'react';
import {date, number, post} from './api';
import {useCanWrite} from './permissions';
import {useLive, ReportImport, type ManufacturingData} from './Manufacturing';
import type {RobotObservation, TwinState} from './types';
import {RobotSettings} from './RobotSettings';
import {ScadaPage} from './Scada';
import {Emulation} from './Emulation';
import {Icon} from './Icons';
import {OperationCards, OperationCharacteristics, operationDefinitions, type AutomationOperation} from './OperationOverview';
import './automation.css';
export type {AutomationOperation} from './OperationOverview';

export type LiveRobot = RobotObservation & {operation: string; connection: string; age_seconds: number; controller_mode?: string; safety_state?: string; [key: string]: unknown};
type Command = {id:string;robot_id:string;status:string;action:string;speed_percent:number|null;reason:string;origin:string;expires_at:number;result_detail?:string};
export type AutomationData = {robots:LiveRobot[]; commands:Command[]; physical_enabled:boolean};
const operations: Record<string,string> = {welding:'Сварка',painting:'Окраска',assembly:'Сборка',unknown:'Участок не указан'};
const statuses: Record<string,string> = {in_progress:'В работе',running:'В работе',idle:'Простой',warning:'Предупреждение',fault:'Неисправность',error:'Ошибка',stopped:'Остановлен',welding:'Сварка',painting:'Окраска',assembly:'Сборка'};
const controllerLabels: Record<string,string> = {automatic:'автоматический',manual:'ручной',offline:'отключён',unknown:'неизвестен'};
const safetyLabels: Record<string,string> = {normal:'штатное состояние',protective_stop:'защитная остановка',emergency_stop:'аварийная остановка',unknown:'неизвестно'};
const commandNames: Record<string,string> = {hold:'Запросить остановку',resume:'Продолжить работу',set_speed_percent:'Изменить скорость'};
const commandStates: Record<string,string> = {proposed:'Предложение',approved:'Подтверждено',dispatched:'Выдано шлюзу',succeeded:'Выполнено контроллером',failed:'Отклонено / не выполнено',uncertain:'Результат неизвестен — нужна сверка',expired:'Срок истёк',cancelled:'Отменено'};
const limitSensors = [{key:'paint_volume_l',label:'Остаток краски',unit:'л'},{key:'electrode_count',label:'Сварочные электроды',unit:'шт.'},{key:'welding_current_a',label:'Сварочный ток',unit:'А'},{key:'motor_current_a',label:'Ток двигателя',unit:'А'},{key:'joint_temperature_c',label:'Температура привода',unit:'°C'},{key:'vibration_mm_s',label:'Вибрация',unit:'мм/с'}];

export function RobotArm({robot,emulated=false}: {robot:{robot_id:string;connection:string;cycle_status:string;joint_2_deg?:unknown;joint_3_deg?:unknown;joint_5_deg?:unknown} | null;emulated?:boolean}) {
  const known = typeof robot?.joint_2_deg === 'number' && typeof robot?.joint_3_deg === 'number';
  const shoulder = known ? Number(robot!.joint_2_deg) : -45, elbow = known ? Number(robot!.joint_3_deg) : 85;
  const live = robot?.connection === 'fresh';
  const fault = robot && ['fault','error','alarm'].includes(robot.cycle_status.toLowerCase());
  const warning = robot?.cycle_status.toLowerCase()==='warning';
  const color = !live ? 'var(--equipment-stale)' : fault ? 'var(--equipment-fault)' : warning ? 'var(--equipment-warning)' : 'var(--equipment-ok)';
  return <figure className="robot-arm"><svg viewBox="0 0 400 280" role="img" aria-label={robot ? `Механическая рука ${robot.robot_id}`:'Робот не подключён'}>
    <defs><pattern id={`arm-grid-${robot?.robot_id ?? 'empty'}`} width="25" height="25" patternUnits="userSpaceOnUse"><path d="M25 0H0V25" fill="none" stroke="var(--equipment-grid)" strokeWidth=".5"/></pattern></defs>
    <rect x="0" y="0" width="400" height="280" fill="var(--equipment-surface)" rx="12"/><path d="M35 246H365M80 50V245M150 50V245M220 50V245M290 50V245" stroke="var(--equipment-grid)" strokeWidth="1"/>
    <path d="M118 244v-28h84v28M129 216v-32h62v32" fill="var(--equipment-metal)" stroke={color} strokeWidth="2"/>
    <g transform="translate(160 192)"><circle r="23" fill="var(--equipment-joint)" stroke={color} strokeWidth="3"/>
      <g className="arm-joint" transform={`rotate(${shoulder})`}><path d="M0 -12H100V12H0Z" fill="var(--equipment-metal)" stroke={color} strokeWidth="2"/><circle cx="100" r="19" fill="var(--equipment-joint)" stroke={color} strokeWidth="3"/>
        <g transform="translate(100 0)"><g className="arm-joint" transform={`rotate(${elbow})`}><path d="M0 -9H83V9H0Z" fill="var(--equipment-metal-light)" stroke={color} strokeWidth="2"/><circle cx="83" r="12" fill="var(--equipment-joint)" stroke={color} strokeWidth="2"/>
          <g transform={`translate(83 0) rotate(${typeof robot?.joint_5_deg==='number'?Number(robot.joint_5_deg):0})`}><path d="M12 -6h19v12H12M31 -6l10 -7M31 6l10 7" stroke={color} strokeWidth="5" fill="none"/></g>
        </g></g>
      </g>
    </g><circle cx="28" cy="28" r="5" fill={color}/><text x="42" y="33" fill={color} fontSize="12">{emulated?(fault?'ЭМУЛЯЦИЯ · АВАРИЯ':'ЭМУЛЯЦИЯ · РОБОТ'):!robot?'НЕТ ПОКАЗАНИЙ':!live?'ИСТОРИЧЕСКОЕ СОСТОЯНИЕ':fault?'НЕИСПРАВНОСТЬ':warning?'ПРЕДУПРЕЖДЕНИЕ':'ПОЛУЧАЕМ ПОКАЗАНИЯ'}</text>
  </svg><figcaption>{emulated?'Движение виртуального манипулятора по циклу сборки.':known?'Схема по углам осей 2, 3 и 5; не геометрия рабочей зоны.':'Положение не передано: показана условная неподвижная схема.'}</figcaption></figure>;
}

function Reading({title,value,unit}: {title:string;value:unknown;unit:string}) {return <div className="arm-reading"><span>{title}</span><strong>{number(value,1)} <small>{unit}</small></strong></div>;}

export function AutomationPage({state,selectedOperation=null,onSelectOperation,onEditStation}:{state:TwinState;selectedOperation?:AutomationOperation|null;onSelectOperation:(operation:AutomationOperation|null)=>void;onEditStation:(stationId:string)=>void}){
  const live=useLive<AutomationData>('/automation'),daily=useLive<ManufacturingData>('/manufacturing',10000);
  const robots=live.value?.robots??[],report=daily.value?.report,section=selectedOperation;
  const unknown=robots.filter(r=>!operationDefinitions.some(d=>d.id===r.operation));
  return <div className="manufacturing-page automation-page" data-testid="automation-page">
    {!section?<>
      {live.error&&<p className="connection-banner" role="alert">Не удалось обновить показания роботов: {live.error}</p>}
      <OperationCards state={state} robots={robots} loading={!live.value&&!live.error} error={live.error} onSelect={onSelectOperation}/>
      <p className="intro">Выберите участок, чтобы открыть его характеристики, показания роботов, оборудование и обслуживание.</p>
      {unknown.length>0&&<UnknownRobots value={live.value} error={live.error} refresh={live.refresh} count={unknown.length}/>}
      <details className="operation-disclosure"><summary>Загрузить показания или суточный отчёт</summary><ReportImport onSaved={()=>{live.refresh();daily.refresh();}}/></details>
    </>:<>
      <div className="operation-navigation"><button type="button" className="button secondary" onClick={()=>onSelectOperation(null)} data-testid="automation-back">Все участки</button><h2>{operations[section]}</h2><Icon name="chevron" size={17}/><span className="muted">Характеристики и оборудование</span></div>
      <OperationCharacteristics state={state} operation={section} onEditStation={onEditStation}/>
      <OperationRobots key={section} section={section} value={live.value} error={live.error} refresh={live.refresh}/>
      <section className="operation-scada"><div className="operation-scada-title"><span className="eyebrow">Учёт оборудования · {operations[section]}</span><h2>Оборудование и обслуживание участка</h2><p className="intro">Узлы, показания, аварии и работы по ТО, связанные с выбранным участком. Остатки запчастей учитываются на общем складе установки.</p></div><ScadaPage key={section} operation={section}/></section>
      {daily.error&&<p className="connection-banner" role="alert">Не удалось обновить суточный отчёт: {daily.error}</p>}
      {report&&<section className="panel"><span className="eyebrow">Суточный отчёт · {report.filename}</span><h2>План и факт · {operations[section]}</h2><div className="robot-table"><table><thead><tr><th>Дата</th><th>Линия</th><th>План</th><th>Факт</th><th>Работа, ч</th><th>Загрузка</th></tr></thead><tbody>{report.lines.filter(r=>r.operation===section).map((r,i)=><tr key={i}><td>{r.date}</td><td>{r.line}</td><td>{r.plan}</td><td>{r.actual}</td><td>{r.hours}</td><td>{r.utilization}%</td></tr>)}</tbody></table></div>{!report.lines.some(r=>r.operation===section)&&<p className="small-note">В загруженном отчёте нет строк этого участка.</p>}<h3>Простои оборудования</h3>{report.downtimes.filter(r=>r.operation===section).map((r,i)=><p className="report-warning" key={i}>{r.date} · {r.equipment} · {r.reason} · {r.minutes} мин</p>)}{!report.downtimes.some(r=>r.operation===section)&&<p className="small-note">Простои этого участка в отчёте не зарегистрированы.</p>}</section>}
      <ReportImport onSaved={()=>{live.refresh();daily.refresh();}}/>
      {section==='assembly'&&<AssemblyStand/>}
    </>}
  </div>;
}

function UnknownRobots({value,error,refresh,count}:{value:AutomationData|null;error:string;refresh:()=>void;count:number}){
  const [open,setOpen]=useState(false);
  return <details className="panel operation-disclosure" open={open} onToggle={e=>setOpen(e.currentTarget.open)}><summary>Оборудование без указанного участка · {count}</summary><p className="intro">Эти роботы не отнесены к сварке, окраске или сборке. Их измерения и команды доступны здесь; для распределения передайте корректное поле operation или название участка в телеметрии.</p>{open&&<OperationRobots section="unknown" value={value} error={error} refresh={refresh}/>}</details>;
}

function AssemblyStand(){
  const [open,setOpen]=useState(false);
  return <details className="panel operation-disclosure" open={open} onToggle={e=>setOpen(e.currentTarget.open)}><summary>Учебный стенд сборки</summary><p className="intro">Самостоятельная учебная модель из SCADA-архива: остекление, колёса, сиденья, тяжёлые узлы и конвейер. Её команды изменяют только виртуальный стенд и не затрагивают реальные показания, склад и оборудование.</p>{open&&<Emulation/>}</details>;
}

function OperationRobots({section,value,error,refresh}: {section:AutomationOperation|'unknown';value:AutomationData|null;error:string;refresh:()=>void}) {
  const canWrite = useCanWrite();
  const [search,setSearch] = useState(''), [selected,setSelected] = useState('');
  const [page,setPage] = useState(0), [busy,setBusy] = useState(false), [actionError,setActionError] = useState('');
  const [action,setAction] = useState('hold'), [speed,setSpeed] = useState('25'), [reason,setReason] = useState('');
  const [confirmation,setConfirmation] = useState<Command|null>(null), [checked,setChecked] = useState(false);
  const robots = value?.robots ?? [], sectionRobots=robots.filter(r=>section==='unknown'?!operationDefinitions.some(d=>d.id===r.operation):r.operation===section);
  const filtered=sectionRobots.filter(r=>`${r.robot_id} ${r.line_section}`.toLowerCase().includes(search.toLowerCase()));
  const pages = Math.max(1,Math.ceil(filtered.length/12)), current = Math.min(page,pages-1);
  const robot = filtered.find(r => r.robot_id===selected) ?? filtered[0] ?? null;
  const showPaint=section==='painting'||typeof robot?.paint_volume_l==='number'||typeof robot?.paint_capacity_l==='number';
  const showElectrodes=section==='welding'||typeof robot?.electrode_count==='number',showWelding=section==='welding'||typeof robot?.welding_current_a==='number';
  async function act(work:()=>Promise<unknown>) {setBusy(true);setActionError('');try {await work();refresh();}catch(e){setActionError((e as Error).message);}finally{setBusy(false);}}
  useEffect(()=>{setConfirmation(null);setChecked(false);},[robot?.robot_id]);
  return <div className="operation-robots">
    {error && <p className="connection-banner" role="alert">{error}</p>}

    <section className="panel"><div className="panel-heading"><h2>Роботы · {operations[section]}</h2><span className="subtle-pill">Обновление каждые 2 секунды</span></div><label className="robot-search">Найти оборудование<input type="search" value={search} onChange={e=>{setSearch(e.target.value);setPage(0);}}/></label>
      <div className="equipment-list">{filtered.slice(current*12,(current+1)*12).map(r=><button className={`equipment-item ${r.robot_id===robot?.robot_id?'selected':''}`} key={r.robot_id} onClick={()=>setSelected(r.robot_id)}><strong>{r.robot_id}</strong><span>{statuses[r.cycle_status.toLowerCase()]??r.cycle_status}</span><small>{r.connection==='fresh'?'Свежие показания':r.connection==='future'?'Проверьте часы источника':'Данные устарели'}</small></button>)}</div>
      {!value&&!error?<p className="intro">Загрузка показаний роботов…</p>:!filtered.length&&<p className="intro">{sectionRobots.length?'По вашему запросу оборудование не найдено. Измените строку поиска.':<>Для этого участка ещё нет телеметрии. Загрузите CSV с operation={section==='unknown'?'welding, painting или assembly':section} или подключите шлюз. Суточный DOCX не содержит положения и состояния манипуляторов.</>}</p>}
      {pages>1 && <div className="robot-actions"><button className="button secondary" disabled={!current} onClick={()=>setPage(v=>v-1)}>Назад</button><span>{current+1} / {pages}</span><button className="button secondary" disabled={current+1>=pages} onClick={()=>setPage(v=>v+1)}>Далее</button></div>}
    </section>
    <section className="panel"><div className="panel-heading"><div><span className="eyebrow">Манипулятор · {operations[section]}</span><h2>{robot?.robot_id ?? 'Оборудование не подключено'}</h2></div><span className={`connection-tag ${robot?.connection==='fresh'?'fresh':''}`}>{robot?statuses[robot.cycle_status.toLowerCase()]??robot.cycle_status:'Нет данных'}</span></div>
      <div className="arm-layout"><RobotArm robot={robot}/><div><p className="intro">Последнее измерение: {date(robot?.timestamp)} (UTC+5). {robot?.connection==='stale'?'Связь не подтверждена свежими данными.':''}</p><div className="arm-readings"><Reading title="Температура привода" value={robot?.joint_temperature_c} unit="°C"/><Reading title="Вибрация" value={robot?.vibration_mm_s} unit="мм/с"/><Reading title="Ток двигателя" value={robot?.motor_current_a} unit="А"/><Reading title="Скорость" value={robot?.speed_percent} unit="%"/><Reading title="Выполнение цикла" value={robot?.cycle_progress_pct} unit="%"/><Reading title="Гидравлика" value={robot?.hydraulic_pressure_bar} unit="бар"/><Reading title="Пневматика" value={robot?.pneumatic_pressure_bar??robot?.pneumatic_pressure} unit="бар"/></div><p className="intro">Контроллер: {controllerLabels[robot?.controller_mode ?? ''] ?? 'не указан'} · Защита: {safetyLabels[robot?.safety_state ?? ''] ?? 'не передана'} · Код: {robot?.error_code || '—'}</p></div></div>
      {(showPaint||showElectrodes||showWelding)&&<div className="consumable-grid operation-robot-consumables">{showPaint&&<article className="consumable"><span>Краска в ёмкости</span><strong>{number(robot?.paint_volume_l,1)} <small>л / {number(robot?.paint_capacity_l,1)} л</small></strong>{typeof robot?.paint_volume_l==='number' && typeof robot?.paint_capacity_l==='number' && robot.paint_capacity_l>0 && <meter min={0} max={robot.paint_capacity_l} value={robot.paint_volume_l} aria-label="Остаток краски"/>}<small>Отсутствующий датчик обозначается «—».</small></article>}{showElectrodes&&<article className="consumable"><span>Сварочные электроды</span><strong>{number(robot?.electrode_count)} <small>шт.</small></strong><small>Фактический остаток из телеметрии.</small></article>}{showWelding&&<article className="consumable"><span>Сварочный ток</span><strong>{number(robot?.welding_current_a,1)} <small>А</small></strong><small>Показание сварочного оборудования.</small></article>}</div>}
      <div className="joint-values">{[1,2,3,4,5,6].map(n=><Reading key={n} title={`Ось ${n}`} value={robot?.[`joint_${n}_deg`]} unit="°"/>)}</div>
      {robot && <RobotSettings key={robot.robot_id} robotId={robot.robot_id} sensors={limitSensors} onSaved={()=>refresh()}/>}
    </section>
    <section className="panel"><h2>Команды оборудования</h2><p className="intro">{value?.physical_enabled?'Команды требуют подтверждения оператора, свежих показаний и разрешения контроллера.':'Физическое управление выключено. Можно подготовить предложение; выполнение доступно после настройки заводского шлюза и пусконаладки.'} Остановка здесь — запрос контроллеру, не замена аварийной кнопки.</p>
      <form className="quality-form" onSubmit={e=>{e.preventDefault();if(robot)void act(()=>post('/automation/commands',{robot_id:robot.robot_id,action,speed_percent:action==='set_speed_percent'?Number(speed):null,reason}));}}><label>Действие<select value={action} disabled={!canWrite || busy} onChange={e=>setAction(e.target.value)}>{Object.entries(commandNames).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>{action==='set_speed_percent'&&<label>Заданная скорость, %<input type="number" min="1" max="100" required value={speed} onChange={e=>setSpeed(e.target.value)}/></label>}<label>Основание команды<input required minLength={3} maxLength={1000} value={reason} onChange={e=>setReason(e.target.value)}/></label><button className="button primary" disabled={!canWrite || !robot || busy}>Подготовить команду</button></form>
      {actionError&&<p className="inline-error" role="alert">{actionError}</p>}
      <div className="command-list">{value?.commands.filter(c=>sectionRobots.some(r=>r.robot_id===c.robot_id)&&(!robot||c.robot_id===robot.robot_id)).map(c=><article key={c.id}><div><strong>{c.robot_id} · {commandNames[c.action]} {c.speed_percent!==null?`${c.speed_percent}%`:''}</strong><p>{c.reason}</p><span>{commandStates[c.status]} · {c.origin==='local_ai'?'Предложено локальным ИИ':'Оператор'}</span>{c.result_detail&&<p>{c.result_detail}</p>}</div>{c.status==='proposed'&&<button className="button secondary" disabled={!canWrite||busy||!value.physical_enabled} onClick={()=>{setConfirmation(c);setChecked(false);}}>Проверить и подтвердить</button>}{['proposed','approved'].includes(c.status)&&<button className="button tertiary" disabled={!canWrite||busy} onClick={()=>void act(()=>post(`/automation/commands/${c.id}/cancel`,{}))}>Отменить</button>}</article>)}</div>
      {confirmation&&<div className="command-confirm" role="dialog" aria-label="Подтверждение команды оборудования"><h3>{confirmation.robot_id}: {commandNames[confirmation.action]} {confirmation.speed_percent!==null?`${confirmation.speed_percent}%`:''}</h3><p>{confirmation.reason}</p><label><input type="checkbox" checked={checked} onChange={e=>setChecked(e.target.checked)}/> Я проверил оборудование и разрешаю отправку этой команды</label><div className="robot-actions"><button className="button primary" disabled={!checked||busy} onClick={()=>void act(async()=>{await post(`/automation/commands/${confirmation.id}/approve`,{confirm:true});setConfirmation(null);})}>Разрешить отправку</button><button className="button secondary" onClick={()=>setConfirmation(null)}>Закрыть</button></div></div>}
    </section>
  </div>;
}

