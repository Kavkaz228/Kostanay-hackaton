import {useEffect, useRef, useState} from 'react';
import {number, post} from './api';
import {useLive} from './Manufacturing';
import {useCanWrite} from './permissions';
import {AnimatedRobotSchematic, ConveyorSchematic, LineOverview} from './EmulationSchematics';
import {EmulationAssistant, EmulationMaintenance, EmulationNotifications, EmulationStock, ForecastWindow} from './EmulationOperations';
import {nodeNames as nodes, type Component, type Point, type Sensor, type State} from './EmulationTypes';
import './emulation.css';

const labels:Record<string,string>={ok:'Норма',warn:'Внимание',alarm:'Авария'};
const states:Record<string,string>={run:'Работа',warn:'Внимание',stop:'Аварийный останов',idle:'Ожидание',pause:'Пауза времени'};
const uid=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
const hour=(t:number)=>`${number(t,2)} ч`;
function Badge({level}:{level:string}){return <span className={`scada-status ${level==='warn'?'warning':level}`}>{labels[level]??level}</span>;}

function SensorTrend({sensor,points}:{sensor:Sensor;points:Point[]}){
  const container=useRef<HTMLDivElement>(null),[width,setWidth]=useState(680);
  useEffect(()=>{
    const element=container.current;if(!element)return;
    const measure=()=>{const next=element.getBoundingClientRect().width;if(next>0)setWidth(Math.max(220,Math.round(next)));};
    measure();
    if(typeof ResizeObserver!=='undefined'){const observer=new ResizeObserver(measure);observer.observe(element);return ()=>observer.disconnect();}
    window.addEventListener('resize',measure);return ()=>window.removeEventListener('resize',measure);
  },[]);
  const low=Math.min(sensor.warn,sensor.alarm,...points.map(p=>p.value)),high=Math.max(sensor.warn,sensor.alarm,...points.map(p=>p.value));
  const min=low-(high-low||1)*.15,max=high+(high-low||1)*.15;
  const y=(v:number)=>105-(v-min)/(max-min)*90;
  const left=35,right=width-45;
  const x=(t:number)=>left+(t-(points[0]?.t??0))/Math.max(1e-9,(points.at(-1)?.t??1)-(points[0]?.t??0))*(right-left);
  return <div className="emu-trend" ref={container}><h3>{sensor.label}</h3><svg viewBox={`0 0 ${width} 145`} height="145" role="img" aria-label={`График: ${sensor.label}`}>
    {[{value:sensor.warn,color:'var(--warning)',name:'Внимание'},{value:sensor.alarm,color:'var(--danger)',name:'Авария'}].map(p=><g key={p.name}><line x1={left} x2={right} y1={y(p.value)} y2={y(p.value)} stroke={p.color} strokeDasharray="5 5"/><text x={right+5} y={y(p.value)+4} fill={p.color} fontSize="11">{number(p.value,Math.max(sensor.d,2))}</text></g>)}
    <polyline points={points.map(p=>`${x(p.t)},${y(p.value)}`).join(' ')} fill="none" stroke="var(--chart-primary)" strokeWidth="2"/>
    {points.length===1&&<circle cx={x(points[0].t)} cy={y(points[0].value)} r="3" fill="var(--chart-primary)"/>}
    <text x={left} y="130" fill="var(--chart-label)" fontSize="11">{hour(points[0]?.t??0)}</text><text x={right} y="130" textAnchor="end" fill="var(--chart-label)" fontSize="11">{hour(points.at(-1)?.t??0)} · {sensor.unit}</text>
  </svg><small>Время эмуляции · до 120 последних отсчётов · пунктиром показаны пороги</small></div>;
}

export function Emulation(){
  const live=useLive<State>('/emulation'),data=live.value,canWrite=useCanWrite();
  const [asset,setAsset]=useState('R1'),[node,setNode]=useState('J2'),[selected,setSelected]=useState('');
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[reset,setReset]=useState(false);
  const [motion,setMotion]=useState(()=>{try{return localStorage.getItem('allur-emulation-motion')!=='off';}catch{return true;}});
  const [reducedMotion,setReducedMotion]=useState(()=>window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  useEffect(()=>{if(!notice)return;const timer=setTimeout(()=>setNotice(''),6000);return ()=>clearTimeout(timer);},[notice]);
  useEffect(()=>{try{localStorage.setItem('allur-emulation-motion',motion?'on':'off');}catch{/* The preference remains active for this screen. */}},[motion]);
  useEffect(()=>{const media=window.matchMedia('(prefers-reduced-motion: reduce)'),change=()=>setReducedMotion(media.matches);media.addEventListener('change',change);return ()=>media.removeEventListener('change',change);},[]);
  const availableNodes=Array.from(new Set(data?.components.filter(c=>c.robot===asset).map(c=>c.node)??[]));
  const activeNode=availableNodes.includes(node)?node:availableNodes[0];
  const components=data?.components.filter(c=>c.robot===asset&&c.node===activeNode)??[];
  const current=components.find(c=>c.id===selected)??components[0];
  const history=useLive<{id:string;sensors:Sensor[]}>(`/emulation/components/${encodeURIComponent(current?.id??'R1-J2-mot')}`);
  async function act(action:string,extra:Record<string,unknown>={}){
    if(!data||busy||!canWrite)return false;
    setBusy(true);setError('');setNotice('');
    try {const result=await post<{line:string;alarms:number}>('/emulation/commands',{request_id:uid(),expected_revision:data.revision,action,...extra});if(action==='restart'&&result.line!=='run'){setError('Пуск заблокирован: проверка датчиков обнаружила аварии.');return false;}setNotice('Состояние эмуляции обновлено.');setReset(false);return true;}
    catch(e){setError((e as Error).message);return false;}finally{setBusy(false);live.refresh();history.refresh();}
  }
  const disabled=!canWrite||busy||!!live.error;
  if(!data)return <section className="panel">{live.error?<p role="alert">{live.error}</p>:'Загружаем эмуляцию стенда…'}</section>;
  const selectedStation=data.stations.find(s=>s.id===asset);
  const part=data.parts.find(p=>p.id===current?.part);
  const assetComponents=data.components.filter(c=>c.robot===asset);
  function inspect(c:Component){setAsset(c.robot);setNode(c.node);setSelected(c.id);document.getElementById('emu-equipment')?.scrollIntoView({block:'start'});}
  function chooseNode(n:string){setNode(n);setSelected('');}
  return <div className="emu-page" data-testid="emulation">
    <section className="panel"><div className="panel-heading"><div><span className="eyebrow">SCADA · Виртуальный стенд из вашего архива</span><h2>Эмуляция роботов и конвейера</h2></div><span className="subtle-pill">ЭМУЛЯЦИЯ</span></div>
      <p className="intro">4 робота, 107 узлов и 11 сценариев неисправностей. Показания, ресурс, учебные поставки и уведомления рассчитываются на сервере. Учебные данные и команды относятся только к этому стенду.</p>
      <nav className="emu-nav" aria-label="Разделы виртуального стенда">{[['line','emu-equipment','Линия и узлы'],['maintenance','emu-maintenance','ТО и прогноз стенда'],['stock','emu-stock','Учебный склад'],['assistant','emu-assistant','Учебный ассистент'],['journal','emu-journal','Журнал и уведомления']].map(([key,id,label])=><a key={key} href={`#${id}`} data-testid={`emulation-nav-${key}`}>{label}</a>)}</nav>
      <div className="robot-actions"><button className="button primary" disabled={disabled} onClick={()=>void act(data.paused?'play':'pause')}>{data.paused?'Запустить эмуляцию':'Пауза эмуляции'}</button><button className="button secondary" disabled={disabled||data.line!=='run'} onClick={()=>void act('stop')}>Остановить линию</button><button className="button secondary" disabled={disabled||data.line==='run'||data.alarms.length>0} onClick={()=>void act('restart')}>Пуск линии после аварии</button><label>Скорость времени<select aria-label="Скорость эмуляции" value={data.speed} disabled={disabled} onChange={e=>void act('speed',{value:Number(e.target.value)})}>{[1,60,600,3600].map(s=><option key={s} value={s}>×{s}</option>)}</select></label><button className="button secondary" disabled={disabled||!data.paused} onClick={()=>void act('advance',{value:600})}>Рассчитать 10 минут</button></div>
      <p className="small-note" data-testid="emulation-status">{data.paused?'Время на паузе':'Время идёт'} · {data.line==='run'?'Линия в работе':'Линия остановлена'} · {new Date(data.model_time).toLocaleString('ru-RU',{timeZone:'Asia/Qyzylorda'})} · UTC+5 · {data.shift}-я смена</p>
      <label className="emu-motion"><input type="checkbox" aria-label="Анимация стенда" checked={motion} onChange={e=>setMotion(e.target.checked)}/> Анимация стенда{reducedMotion?' · отключена настройкой уменьшения движения системы':''}</label>
      {live.error&&<p role="alert" className="connection-banner">Связь со стендом потеряна: {live.error}. Показано последнее полученное состояние.</p>}{error&&<p role="alert" className="inline-error">{error}</p>}{notice&&<p role="status" className="small-note">{notice}</p>}
      {!canWrite&&<p className="small-note">Ваша роль позволяет наблюдать за эмуляцией. Управление доступно оператору и администратору.</p>}
    </section>
    <div className="manufacturing-metrics">{[['Собрано кузовов',number(data.cars)],['Расчётный брак',number(data.rejected)],['Время работы',hour(data.run_hours)],['Простой',hour(data.down_hours)],['Остановки по аварии',number(data.stops)]].map(([name,value])=><section className="panel manufacturing-metric" key={name}><span>{name}</span><strong>{value}</strong></section>)}</div>
    <div className="manufacturing-metrics">{[['Доступность',data.summary.availability_pct],['Качество',data.summary.quality_pct],['Производительность',data.summary.performance_pct],['OEE модели',data.summary.oee_pct]].map(([name,value])=><section className="panel manufacturing-metric" key={name}><span>{name}</span><strong>{number(value,1)}%</strong></section>)}</div>
    <section className="panel" id="emu-equipment"><h2>Линия сборки</h2><p className="small-note">Андон показывает работу, ожидание, предупреждение и остановку станций. При аварии виртуальная линия останавливается по модели дзидока.</p><div className="equipment-list">{[...data.stations,{id:'CV',name:'Конвейер',operation:'Транспортировка кузовов',level:data.components.some(c=>c.robot==='CV'&&c.level==='alarm')?'alarm':data.components.some(c=>c.robot==='CV'&&c.level==='warn')?'warn':'ok',operational_status:data.conveyor.operational_status}].map(s=>{const weakest='weakest_component_id' in s?data.components.find(c=>c.id===s.weakest_component_id):data.components.filter(c=>c.robot===s.id).sort((a,b)=>(a.remaining_hours??Infinity)-(b.remaining_hours??Infinity))[0];return <button className={`equipment-item ${asset===s.id?'selected':''}`} key={s.id} onClick={()=>{setAsset(s.id);setSelected('');}}><strong>{s.id} · {s.name}</strong><span>{s.operation}</span><Badge level={s.level}/><small>{states[s.operational_status]}</small>{'body_number' in s&&s.body_number!==undefined&&<small>Кузов № {s.body_number} · {number(('phase' in s?s.phase:data.phase)*100)}% операции</small>}{'phase' in s&&<meter aria-label={`Ход операции ${s.id}`} min={0} max={100} value={s.phase*100}/>} {weakest&&<small>Ближайшее ТО: {weakest.name} · {hour(weakest.remaining_hours??0)}</small>}</button>;})}</div>
      <LineOverview data={data} motion={motion&&!reducedMotion}/>
      <div className="automation-tabs" role="group" aria-label="Узлы эмуляции">{availableNodes.map(n=><button key={n} className={`button ${activeNode===n?'primary':'secondary'} compact`} aria-pressed={n===activeNode} onClick={()=>{setNode(n);setSelected('');}}>{nodes[n]??n}</button>)}</div>
      <div className="arm-layout">{selectedStation?<div><h3>{selectedStation.name} · {selectedStation.payload}</h3><p className="small-note">{selectedStation.tool}</p><AnimatedRobotSchematic data={data} motion={motion&&!reducedMotion} asset={asset} components={assetComponents} selected={activeNode} choose={chooseNode}/><div className="joint-values">{selectedStation.joints.map((v,i)=><div className="arm-reading" key={i}><span>J{i+1}</span><strong>{number(v,1)}°</strong><small>Нагрузка: {number(assetComponents.find(c=>c.node===`J${i+1}`&&c.kind==='reducer')?.load_pct,1)}%</small></div>)}</div></div>:<div><h3>Конвейер</h3><ConveyorSchematic data={data} motion={motion&&!reducedMotion} selected={activeNode} choose={chooseNode}/><div className="arm-readings">{[['Скорость',`${number(data.conveyor.speed_m_min,1)} м/мин`],['Нагрузка привода',`${number(data.conveyor.load_pct,1)}%`],['Ток двигателя',`${number(data.conveyor.current_a,1)} A`],['Пробег цепи',`${number(data.conveyor.distance_km,1)} км`],['Смазка',`${number(data.conveyor.lubrication_pct,1)}%`],['Подача смазки',`${number(data.conveyor.lubrication_flow_pct,1)}%`],['Остановки',number(data.conveyor.stops)]].map(([name,value])=><div className="arm-reading" key={name}><span>{name}</span><strong>{value}</strong></div>)}</div><p className="small-note">Сопротивление роликов +{number(data.conveyor.roller_resistance_pct,1)}%, цепи +{number(data.conveyor.chain_resistance_pct,1)}%, заклинивание +{number(data.conveyor.jam_pct,1)}% к базовой нагрузке 60%.</p></div>}
        <div className="equipment-list">{components.map(c=><button className={`equipment-item ${current?.id===c.id?'selected':''}`} key={c.id} onClick={()=>setSelected(c.id)}><strong>{c.name}</strong><span>Износ {number(c.wear_pct,1)}%</span><Badge level={c.level}/></button>)}</div></div>
    </section>
    {current&&<section className="panel"><div className="panel-heading"><div><span className="eyebrow">{current.id} · {nodes[current.node]??current.node}</span><h2>{current.name}</h2></div><Badge level={current.level}/></div><div className="arm-readings">{current.sensors.map(s=><div className="arm-reading" key={s.k}><span>{s.label}</span><strong>{number(s.value,s.d)} <small>{s.unit}</small></strong><Badge level={s.level}/><small>Внимание {s.lo?'≤':'≥'} {s.warn}; авария {s.lo?'≤':'≥'} {s.alarm}</small>{s.sampled_at!==null&&s.sampled_at<data.t-1e-6&&<small>Последний отсчёт: {hour(s.sampled_at)}; датчик удерживает показание при остановке.</small>}</div>)}</div>
      {!current.sensors.length&&<p className="intro">Для этого узла модель рассчитывает ресурс по наработке и циклам.</p>}
      <div className="consumable-grid"><article className="consumable"><span>Расчётный износ</span><strong>{number(current.wear_pct,1)}%</strong><meter aria-label="Износ узла эмуляции" min="0" max="100" value={Math.min(100,current.wear_pct)}/></article><article className="consumable"><span>Оценка остаточного ресурса</span><strong>{number(current.remaining_hours,1)} ч</strong><small>При текущих условиях модели</small></article><article className="consumable"><span>Учебный склад · {part?.name}</span><strong>{part?.service?'Работа по ТО':`${part?.stock??0} шт.`}</strong></article></div>
      <p className="small-note">Прогноз обслуживания: <ForecastWindow forecast={current.forecast}/> · {current.forecast.basis}. {current.load_pct!==null&&`Нагрузка ${number(current.load_pct,1)}% от номинальной.`}</p>
      {current.level!=='ok'&&<p className="report-warning">{current.cause}. {current.action}.</p>}
      <div className="robot-actions"><button className="button secondary" disabled={disabled||!part||!part.service&&part.stock<1} onClick={()=>void act('replace',{target:current.id})}>{part?.service?'Выполнить учебное ТО':'Заменить узел в эмуляции'}</button>{part&&!part.service&&<button className="button secondary" disabled={disabled} onClick={()=>void act('stock',{target:part.id,value:1})}>Добавить 1 деталь на учебный склад</button>}</div>
      {history.error&&<p role="alert">Не удалось обновить историю: {history.error}</p>}{history.value?.id===current.id&&current.sensors.map(s=><SensorTrend key={current.id+':'+s.k} sensor={s} points={history.value?.sensors.find(v=>v.k===s.k)?.history??[]}/>)}
    </section>}
    <section className="panel"><details><summary>Все узлы {asset==='CV'?'конвейера':asset} · {assetComponents.length}</summary><div className="robot-table"><table><thead><tr><th>Узел</th><th>Компонент</th><th>Износ / ресурс</th><th>Датчики</th><th>Состояние</th></tr></thead><tbody>{assetComponents.map(c=><tr key={c.id} data-testid={`emulation-component-${c.id}`}><td>{nodes[c.node]??c.node}</td><td><button className="button secondary compact" onClick={()=>inspect(c)}>{c.name}</button><small className="scada-subtext">{c.id}</small></td><td>{number(c.wear_pct,1)}% · {number(c.remaining_hours,1)} ч</td><td>{c.sensors.map(s=>`${s.label}: ${number(s.value,s.d)} ${s.unit}`).join(' · ')||'Оценка по наработке и циклам'}</td><td><Badge level={c.level}/></td></tr>)}</tbody></table></div></details><div className="emu-issues">{assetComponents.filter(c=>c.level!=='ok').map(c=><button className="button secondary compact" key={c.id} onClick={()=>inspect(c)}><Badge level={c.level}/>{nodes[c.node]??c.node} · {c.name}: {c.cause}</button>)}</div></section>
    <section className="panel"><h2>Сценарии неисправностей</h2><p className="intro">Включите неисправность и наблюдайте за датчиками. Нагрев развивается постепенно. После устранения причины квитируйте аварию и запустите виртуальную линию.</p><div className="emu-faults">{data.faults.map(f=><article className="consumable" key={f.id}><span>{f.where}</span><h3>{f.name}</h3><p>{f.hint}</p><button className={`button ${f.active?'primary':'secondary'} compact`} aria-pressed={f.active} disabled={disabled} onClick={()=>void act('fault',{target:f.id,active:!f.active})}>{f.active?'Устранить':'Ввести'}: {f.name}</button></article>)}</div></section>
    <section className="panel"><div className="panel-heading"><h2>Аварии эмуляции</h2><span className="subtle-pill">{data.alarms.length}</span></div>{!data.alarms.length?<p className="intro">Незакрытых аварий нет.</p>:<div className="command-list">{data.alarms.map(a=>{const c=data.components.find(c=>c.id===a.component_id);return <article key={a.id}><div><strong>{a.component_id} · {a.label}</strong><p>{a.recovered?'Причина устранена, можно квитировать':'Причина активна — квитирование заблокировано'}</p><small>Модельное время: {hour(a.t)} · Срабатываний: {a.trips??1}</small>{a.value!==undefined&&<p className="small-note">При срабатывании: {number(a.value,2)} {a.unit}</p>}{c&&<><p className="small-note">Вероятная причина: {a.cause??c.cause}. Рекомендация: {a.action??c.action}.</p><button className="button secondary compact" onClick={()=>inspect(c)}>На схему: {a.component_id}</button></>}</div><button className="button secondary" disabled={disabled||!a.recovered} onClick={()=>void act('ack',{target:a.id})}>Квитировать {a.component_id}</button></article>;})}</div>}</section>
    <EmulationMaintenance data={data} disabled={disabled} act={act} inspect={inspect}/>
    <EmulationStock data={data} disabled={disabled} act={act}/>
    <EmulationAssistant data={data} disabled={disabled} act={act}/>
    <section className="panel" id="emu-journal"><h2>Журнал стенда</h2><p className="small-note">Последние 300 событий. Действия операторов также сохраняются в общем журнале аудита.</p><div className="robot-table emu-events"><table><thead><tr><th>Время модели</th><th>Событие</th></tr></thead><tbody>{data.events.map(e=><tr key={e.id}><td>{hour(e.t)}</td><td>{e.text}</td></tr>)}</tbody></table></div><details><summary>Начать учебный сценарий заново</summary><p className="intro">Сброс удалит историю этого стенда, вернёт исходный износ и учебный склад. Время будет на паузе.</p><label><input type="checkbox" checked={reset} onChange={e=>setReset(e.target.checked)}/> Сбросить только эмуляцию SCADA</label><button className="button secondary" disabled={disabled||!reset} onClick={()=>void act('reset')}>Сбросить эмуляцию</button></details></section>
    <EmulationNotifications data={data} disabled={disabled} act={act}/>
    {(error||notice)&&<aside className={`emu-feedback ${error?'emu-feedback-error':''}`} aria-label="Результат команды стенда"><span>{error||notice}</span><button type="button" aria-label="Закрыть результат команды" onClick={()=>{setError('');setNotice('');}}>×</button></aside>}
  </div>;
}
