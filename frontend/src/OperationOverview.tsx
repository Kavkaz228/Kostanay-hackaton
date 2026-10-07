import {date, elapsed, number} from './api';
import {Icon, type IconName} from './Icons';
import {statusLabels} from './StationEditor';
import {useCanWrite} from './permissions';
import type {TwinState} from './types';
import type {LiveRobot} from './Automation';
import {AnimatedNumber} from './Motion';

export type AutomationOperation='welding'|'painting'|'assembly';
export const operationDefinitions:{id:AutomationOperation;title:string;description:string;icon:IconName}[]=[
  {id:'welding',title:'Сварка',description:'Сварочные роботы, ток, электроды и состояние узлов.',icon:'bolt'},
  {id:'painting',title:'Окраска',description:'Окрасочные роботы, запас краски и параметры работы.',icon:'flask'},
  {id:'assembly',title:'Сборка',description:'Сборочные роботы, привод, конвейер и оснастка.',icon:'layers'},
];

export function OperationCards({state,robots,loading,error,onSelect}:{state:TwinState;robots:LiveRobot[];loading:boolean;error:string;onSelect:(operation:AutomationOperation)=>void}){
  return <div className="operation-cards" role="group" aria-label="Участки автоматизации">{operationDefinitions.map(operation=>{
    const station=state.stations.find(s=>s.id===operation.id),list=robots.filter(r=>r.operation===operation.id),fresh=list.filter(r=>r.connection==='fresh').length;
    return <button type="button" key={operation.id} className="operation-card" data-testid={`automation-operation-${operation.id}`} aria-label={`Открыть участок ${operation.title}`} onClick={()=>onSelect(operation.id)}>
      <span className="operation-card-top"><span className="operation-symbol"><Icon name={operation.icon} size={30}/></span><Icon name="arrowUp" size={21}/></span>
      <strong className="operation-card-title">{operation.title}</strong><span className="operation-card-description">{operation.description}</span>
      <span className="operation-card-status">{station?<><i className={`operation-state-dot ${station.status}`}/>{statusLabels[station.status]}<small>{state.source==='simulation'?'Модель':'Снимок телеметрии'}</small></>:'Нет снимка участка'}</span>
      <span className="operation-card-metrics"><span><b><AnimatedNumber value={station?.completed}/></b><small>обработано {state.source==='simulation'?'в модели':'по снимку'}</small></span><span><b>{loading?'…':error?'—':number(list.length)}</b><small>роботов с телеметрией</small></span></span>
      <span className="operation-card-footer"><span>{loading?'Загрузка показаний…':error?'Связь с показаниями потеряна':`Свежие показания: ${fresh} из ${list.length}`}</span><span>Характеристики и оборудование <Icon name="arrow" size={15}/></span></span>
    </button>;
  })}</div>;
}

export function OperationCharacteristics({state,operation,onEditStation}:{state:TwinState;operation:AutomationOperation;onEditStation:(id:string)=>void}){
  const canWrite=useCanWrite(),definition=operationDefinitions.find(d=>d.id===operation)!,station=state.stations.find(s=>s.id===operation);
  const simulation=state.source==='simulation',predictions=state.predictions.filter(p=>p.station_id===operation);
  const stats: [string,unknown,string][] = station ? [['Обработано',station.completed,'ед.'],['Брак',station.rejected,'ед.'],['В очереди',station.queue,'ед.'],['В обработке',station.in_process,'ед.'],['Темп годной продукции',station.throughput,'ед./ч'],['Загрузка',station.utilization,'%']] : [];
  return <section className="panel operation-characteristics" data-testid={`operation-characteristics-${operation}`}>
    <div className="panel-heading"><div><span className="eyebrow">Характеристики участка</span><h2>{definition.title}</h2></div><span className={`source-badge ${simulation?'':'telemetry'}`}><Icon name={simulation?'flask':'database'} size={15}/>{simulation?'Производственная модель':'Телеметрия участков'}</span></div>
    <p className="intro">{simulation?'Показатели участка рассчитываются производственной моделью. Показания реальных роботов и учёт оборудования приведены отдельно ниже.':'Показатели участка получены из сохранённой телеметрии. Пропущенные значения показаны знаком «—»; роботы ниже используют собственные измерения.'}</p>
    {!station?<p className="report-warning">В текущем источнике нет снимка участка «{definition.title}». Показания роботов и зарегистрированное оборудование этого участка доступны ниже.</p>:<>
      <div className="operation-state-line"><span className={`status-pill ${station.status}`}><i/>{statusLabels[station.status]}</span><span>{simulation?(state.running?'Время модели идёт':'Время модели на паузе'):'Сохранённый снимок'} · {date(simulation?state.updated_at:state.telemetry_updated_at)} · UTC+5</span><button type="button" className="button secondary" disabled={!canWrite||!simulation} onClick={()=>onEditStation(station.id)} data-testid={`operation-edit-${operation}`}>Настроить участок <Icon name="gear" size={15}/></button></div>
      <div className="operation-statistics">{stats.map(([label,value,unit])=><div key={label}><span>{label}</span><strong>{number(value,1)} <small>{unit}</small></strong></div>)}</div>
      <dl className="operation-parameters"><div><dt>{simulation?'Базовый цикл модели':'Цикл по снимку'}</dt><dd>{number(station.cycle_seconds,1)} сек</dd></div><div><dt>Настроено рабочих мест</dt><dd>{number(station.capacity)}</dd></div><div><dt>Настроенная вместимость буфера</dt><dd>{number(station.buffer_capacity)} ед.</dd></div><div><dt>Коэффициент скорости модели</dt><dd>×{number(station.speed_factor,2)}</dd></div><div><dt>Вероятность брака в модели</dt><dd>{number(station.defect_rate*100,2)}%</dd></div><div><dt>Накопленный простой</dt><dd>{elapsed(station.downtime_seconds)}</dd></div></dl>
      {simulation?<p className="small-note">Фактический цикл модели с учётом скорости: {number(station.cycle_seconds/Math.max(.1,station.speed_factor),1)} сек. Изменение параметров применяется только к производственной модели.</p>:<p className="small-note">Время цикла получено из телеметрии. Рабочие места, вместимость буфера, коэффициент скорости и вероятность брака — справочные настройки модели, а не показания оборудования.</p>}
    </>}
    {predictions.length>0&&<div className="operation-predictions">{predictions.map((p,i)=><article key={`${p.type}:${i}`} className={`operation-prediction ${p.severity}`}><strong>{p.title}</strong><p>{p.description}</p>{p.eta_minutes!==null&&<small>Расчётный горизонт: {number(p.eta_minutes,1)} мин</small>}</article>)}</div>}
  </section>;
}
