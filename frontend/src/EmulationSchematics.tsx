import {useEffect, useState, type KeyboardEvent, type ReactNode} from 'react';
import {number} from './api';
import type {Component, State} from './EmulationTypes';

type XY=[number,number];
const levels:Record<string,number>={ok:0,warn:1,alarm:2};
const colors:Record<string,string>={ok:'var(--equipment-ok)',warn:'var(--equipment-warning)',alarm:'var(--equipment-fault)'};
const levelOf=(components:Component[],node:string)=>components.filter(c=>c.node===node).reduce((p,c)=>levels[c.level]>levels[p]?c.level:p,'ok');
const shift=(p:XY,a:number,l:number):XY=>[p[0]+Math.sin(a*Math.PI/180)*l,p[1]-Math.cos(a*Math.PI/180)*l];
const path=(p:XY)=>`${p[0].toFixed(2)} ${p[1].toFixed(2)}`;

/*
 * Shared work cycle of the virtual line. Every schematic on the page reads the same clock, so the
 * conveyor, the line overview and the selected robot stay in step: the conveyor indexes cars by one
 * station, then every robot approaches its car, works at two points and retracts. The clock runs while
 * the line is in operation (also when model time is paused) and freezes in place when the line stops.
 * It advances about 30 times per second, which keeps the motion smooth and the schematic clickable.
 */
const CYCLE_MS=5600, MOVE_END=.34;
const lineClock={value:0,since:null as number|null};
const clockAt=(now:number)=>lineClock.since===null?lineClock.value:lineClock.value+(now-lineClock.since)/CYCLE_MS;
function setClockRunning(run:boolean){
  const now=performance.now();
  if(run&&lineClock.since===null)lineClock.since=now;
  else if(!run&&lineClock.since!==null){lineClock.value=clockAt(now);lineClock.since=null;}
}
const smooth=(t:number)=>t<=0?0:t>=1?1:t*t*(3-2*t);
/** p: position inside the current cycle; m: eased conveyor travel (0 at the stations, 1 one pitch later). */
const cycleParts=(value:number)=>{const p=value-Math.floor(value);return {p,m:p<MOVE_END?smooth(p/MOVE_END):1};};

function useCycle(data:State,motion:boolean){
  const running=motion&&data.line==='run';
  const [value,setValue]=useState(()=>clockAt(performance.now()));
  useEffect(()=>{
    if(!motion)return;
    setClockRunning(running);
    if(!running){setValue(clockAt(performance.now()));return;}
    let id=0,last=0;
    const frame=(now:number)=>{if(now-last>=33){last=now;setValue(clockAt(now));}id=requestAnimationFrame(frame);};
    id=requestAnimationFrame(frame);
    return ()=>cancelAnimationFrame(id);
  },[motion,running]);
  return motion?value:0;
}

/** Two-link inverse kinematics; angles are measured clockwise from "up", as in shift(). */
function reach(base:XY,target:XY,l1:number,l2:number,elbow:1|-1):[number,number,XY]{
  const dx=target[0]-base[0],dy=target[1]-base[1];
  const d=Math.min(l1+l2-.01,Math.max(Math.abs(l1-l2)+.01,Math.hypot(dx,dy)));
  const theta=Math.atan2(dx,-dy)*180/Math.PI,alpha=Math.acos((l1*l1+d*d-l2*l2)/(2*l1*d))*180/Math.PI;
  const a1=theta-elbow*alpha,elbowAt=shift(base,a1,l1);
  return [a1,Math.atan2(target[0]-elbowAt[0],-(target[1]-elbowAt[1]))*180/Math.PI,elbowAt];
}
type Key=[number,XY,number];
/** Smoothly interpolates the tool tip and tool angle between timed key poses (t from 0 to 1). */
function track(keys:Key[],p:number):[XY,number]{
  for(let i=1;i<keys.length;i++){
    const [t1,x1,a1]=keys[i-1],[t2,x2,a2]=keys[i];
    if(p<=t2){const k=smooth((p-t1)/Math.max(1e-6,t2-t1));return [[x1[0]+(x2[0]-x1[0])*k,x1[1]+(x2[1]-x1[1])*k],a1+(a2-a1)*k];}
  }
  const last=keys[keys.length-1];return [last[1],last[2]];
}
const workCycle=(retract:Key[1],retractAngle:number,pick:Key[1],pickAngle:number,a:XY,aAngle:number,b:XY,bAngle:number,lift=48):Key[]=>[
  [0,retract,retractAngle],[.14,pick,pickAngle],[.22,pick,pickAngle],[.36,[a[0],a[1]-lift],aAngle],[.46,a,aAngle],[.56,a,aAngle],
  [.64,b,bAngle],[.76,b,bAngle],[.86,[b[0],b[1]-lift],bAngle],[1,retract,retractAngle],
];

/* Side view of one station: the car stops in front of the robot at CAR_X. */
const CAR_X=266,CAR_Y=300,CAR_SCALE=2.2,CAR_PITCH=240;
const BODY='M-30 0V-11Q-29-15-22-15L-12-26H10L19-15H26Q30-14 30-9V0Z';
const stationWork:Record<string,[XY,number,XY,number]>={
  R1:[[298,251],150,[229,251],200], // windscreen, then rear window
  R2:[[306,286],165,[226,286],182], // front wheel, then rear wheel
  R3:[[257,239],172,[277,239],165], // seat through the opening
  R4:[[319,263],150,[308,263],158], // module into the front bay
};
function stationPose(asset:string,p:number,joints:number[]):number[]{
  const [a,aAngle,b,bAngle]=stationWork[asset]??stationWork.R1;
  const [tip,toolAngle]=track(workCycle([262,168],118,[236,122],84,a,aAngle,b,bAngle),p);
  const wrist=shift(tip,toolAngle+180,70);
  const [a2,a3]=reach([122,237],wrist,98,103,1);
  return [joints[0]??0,(a2-24)/.38-65,35-(a3-a2-74)/.4,joints[3]??0,(toolAngle-a3-38)/.48-20,joints[5]??0];
}
/** Parts a car carries after a station: index into the station list, plus what this station already fitted. */
function CarParts({done,current,p}:{done:number;current?:string;p:number}){
  const has=(station:number,part:'a'|'b'|'all')=>station<done||(current===['R1','R2','R3','R4'][station]&&(part==='a'?p>=.56:p>=.76));
  return <>{has(0,'a')&&<path d="M10-25L18-16" stroke="var(--equipment-glass)" strokeWidth="2.4" vectorEffect="non-scaling-stroke"/>}{has(0,'b')&&<path d="M-21-16L-13-25" stroke="var(--equipment-glass)" strokeWidth="2.4" vectorEffect="non-scaling-stroke"/>}
    {has(1,'a')&&<circle cx="18" cy="-1" r="5" fill="var(--equipment-tire)"/>}{has(1,'b')&&<circle cx="-18" cy="-1" r="5" fill="var(--equipment-tire)"/>}
    {has(2,'all')&&<rect x="-6" y="-22" width="7" height="7" rx="1" fill="var(--equipment-metal)"/>}{has(3,'all')&&<rect x="20" y="-13" width="7" height="6" rx="1" fill="var(--equipment-metal)"/>}</>;
}
function StationScene({asset,value,running}:{asset:string;value:number;running:boolean}){
  const {p,m}=cycleParts(value),index=Math.max(0,['R1','R2','R3','R4'].indexOf(asset)),travel=(Math.floor(value)+m)*CAR_PITCH;
  const car=(x:number,done:number,current?:string)=><g transform={`translate(${x.toFixed(1)} ${CAR_Y}) scale(${CAR_SCALE})`}><path d={BODY} fill={running?'var(--equipment-body)':'var(--warning)'} stroke="var(--equipment-body-border)" vectorEffect="non-scaling-stroke"/><CarParts done={done} current={current} p={p}/></g>;
  return <g className="emu-station-scene" aria-hidden="true" pointerEvents="none">
    <defs><clipPath id={`emu-zone-${asset}`}><rect x="176" y="190" width="240" height="122"/></clipPath></defs>
    <rect x="176" y="300" width="240" height="9" rx="2" fill="var(--equipment-surface)" stroke="var(--equipment-metal-light)"/>
    <line x1="180" x2="412" y1="304.5" y2="304.5" stroke="var(--equipment-metal-light)" strokeDasharray="6 8" strokeDashoffset={(-travel%14).toFixed(1)}/>
    <g clipPath={`url(#emu-zone-${asset})`}>{p<MOVE_END?<>{car(CAR_X+m*CAR_PITCH,index+1)}{car(CAR_X-CAR_PITCH+m*CAR_PITCH,index,asset)}</>:car(CAR_X,index,asset)}</g>
  </g>;
}

/* Overview: cars index along the conveyor while four ceiling-mounted robots work on them. */
const LINE_PITCH=217;
const miniWork:[XY,XY][]=[[[111,68],[31,69]],[[118,84],[34,84]],[[66,58],[84,58]],[[122,78],[112,80]]];
function MiniRobot({index,p,level}:{index:number;p:number;level:string}){
  const x0=25+index*LINE_PITCH,base:XY=[x0+75,24],[a,b]=miniWork[index];
  const [wrist]=track(workCycle([x0+72,50],180,[x0+64,46],180,[x0+a[0],a[1]],180,[x0+b[0],b[1]],180,18),p);
  const [l1,,elbow]=reach(base,wrist,38,36,-1);
  const color=colors[level]??colors.ok;
  return <g><rect x={base[0]-10} y="19" width="20" height="7" rx="2" fill="var(--equipment-metal)" stroke="var(--equipment-metal-light)"/>
    <line x1={base[0]} y1={base[1]} x2={elbow[0]} y2={elbow[1]} stroke="var(--equipment-metal)" strokeWidth="6" strokeLinecap="round"/>
    <line x1={elbow[0]} y1={elbow[1]} x2={wrist[0]} y2={wrist[1]} stroke="var(--equipment-metal-light)" strokeWidth="4" strokeLinecap="round"/>
    <line x1={wrist[0]} y1={wrist[1]} x2={wrist[0]} y2={wrist[1]+8} stroke={color} strokeWidth="3" strokeLinecap="round"/>
    {[base,elbow,wrist].map((j,i)=><circle key={i} cx={j[0]} cy={j[1]} r={i===0?4:3.2} fill="var(--equipment-joint)" stroke={color} strokeWidth="1.6"/>)}
    <text x={base[0]+14} y="16" fill="var(--chart-label)" fontSize="12">R{index+1}</text>
    <title>{`R${index+1}: угол плеча ${Math.round(l1)}°`}</title></g>;
}

export function LineOverview({data,motion}:{data:State;motion:boolean}){
  const value=useCycle(data,motion),{p,m}=cycleParts(value),travel=Math.floor(value)+m,running=data.line==='run';
  const roller=(travel*LINE_PITCH/13*180/Math.PI)%360;
  return <svg className="emu-conveyor" viewBox="0 0 920 160" role="img" aria-label="Виртуальный конвейер: кузова и работа роботов R1–R4">
    <defs><clipPath id="emu-line-clip"><rect x="20" y="40" width="880" height="110" rx="26"/></clipPath></defs>
    <line x1="20" x2="900" y1="19" y2="19" stroke="var(--equipment-metal)" strokeWidth="3"/>
    <rect x="20" y="92" width="880" height="54" rx="26" fill="var(--equipment-surface)" stroke="var(--equipment-metal-light)"/>
    {Array.from({length:24},(_,i)=><g key={i} transform={`translate(${35+i*37} 119) rotate(${roller.toFixed(1)})`}><circle r="13" fill="var(--equipment-tire)" stroke="var(--equipment-metal-light)"/><line y1="-10" y2="10" stroke="var(--equipment-metal-light)" strokeWidth="1.5" opacity=".7"/></g>)}
    <g clipPath="url(#emu-line-clip)">{[-1,0,1,2,3].map(k=>{
      const x=25+(k+m)*LINE_PITCH,current=p>=MOVE_END&&k>=0?['R1','R2','R3','R4'][k]:undefined;
      return <g key={k} transform={`translate(${x.toFixed(1)} 68)`}><path d="M0 20h22l18-22h58l26 22h27v30H0Z" fill={running?'var(--equipment-body)':'var(--warning)'} stroke="var(--equipment-ok)"/><g transform="translate(75 50) scale(2.5 2)"><CarParts done={Math.max(0,p>=MOVE_END?k:k+1)} current={current} p={p}/></g></g>;
    })}</g>
    {data.stations.slice(0,4).map((s,i)=><MiniRobot key={s.id} index={i} p={p} level={s.level}/>)}
  </svg>;
}

export function AnimatedRobotSchematic({data,motion,asset,components,selected,choose}:{data:State;motion:boolean;asset:string;components:Component[];selected:string;choose:(node:string)=>void}){
  const value=useCycle(data,motion),station=data.stations.find(s=>s.id===asset)!;
  const pose=!motion?[0,-65,35,0,-20,0]:stationPose(asset,cycleParts(value).p,station.joints);
  return <RobotSchematic asset={asset} components={components} pose={pose} selected={selected} choose={choose} scene={<StationScene asset={asset} value={value} running={data.line==='run'}/>}/>;
}

function Hit({label,node,selected,components,choose,children}:{label:string;node:string;selected:string;components:Component[];choose:(node:string)=>void;children:ReactNode}){
  const level=levelOf(components,node);
  function key(e:KeyboardEvent<SVGGElement>){if(e.key==='Enter'||e.key===' '){e.preventDefault();choose(node);}}
  return <g className={`emu-svg-hit ${selected===node?'selected':''}`} style={{color:colors[level]}} role="button" tabIndex={0} aria-label={label} aria-pressed={selected===node} onClick={()=>choose(node)} onKeyDown={key}><title>{label} · {level==='alarm'?'Авария':level==='warn'?'Внимание':'Норма'}</title>{children}</g>;
}
function Flag({at,level}:{at:XY;level:string}){return level==='ok'?null:<g transform={`translate(${path(at)})`} className="emu-svg-flag" aria-hidden="true"><circle r="8" fill={colors[level]}/><text textAnchor="middle" dy="3.7" fill="var(--on-status)" fontSize="11" fontWeight="700">!</text></g>;}

export function RobotSchematic({asset,components,pose,selected,choose,scene}:{asset:string;components:Component[];pose:number[];selected:string;choose:(node:string)=>void;scene?:ReactNode}){
  const a2=24+(pose[1]+65)*.38,a3=a2+74-(pose[2]-35)*.4,a5=a3+38+(pose[4]+20)*.48;
  const p1:XY=[122,278],p2:XY=[122,237],p3=shift(p2,a2,98),p5=shift(p3,a3,103),p4:XY=[(p3[0]+p5[0])/2,(p3[1]+p5[1])/2],p6=shift(p5,a5,30);
  const points:Record<string,XY>={J1:p1,J2:p2,J3:p3,J4:p4,J5:p5,J6:p6};
  const tool=shift(p6,a5,25),cable:XY=[p4[0],p4[1]-21];
  const cablePath=`M138 291 Q147 251 ${path(shift(p2,a2-90,20))} L${path(shift(p3,a2-90,20))} L${path(shift(p5,a3-90,20))} L${path(shift(p6,a5-90,18))}`;
  const props={components,selected,choose};
  return <figure className="emu-schematic"><svg viewBox="0 0 420 335" role="group" aria-label={`Кинематическая схема робота ${asset}`}>
    <line x1="12" x2="408" y1="309" y2="309" stroke="var(--equipment-metal)"/>{Array.from({length:25},(_,i)=><line key={i} x1={12+i*16} x2={5+i*16} y1="310" y2="319" stroke="var(--equipment-grid)"/>)}
    {scene}
    <path d="M66 292Q81 310 98 293" fill="none" stroke="var(--equipment-metal-light)" strokeWidth="4"/>
    <rect x="96" y="291" width="52" height="18" rx="3" fill="var(--equipment-metal)" stroke="var(--equipment-metal-light)"/>
    {[[[122,296],p2], [p2,p3], [p3,p5], [p5,p6]].map(([a,b],i)=><g key={i}><line x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke="var(--equipment-metal)" strokeWidth={22-i*4} strokeLinecap="round"/><line x1={a[0]-2} y1={a[1]} x2={b[0]-2} y2={b[1]} stroke="var(--equipment-metal-light)" strokeWidth="2"/></g>)}
    <Hit {...props} node="cable" label="Кабельные пакеты"><path d={cablePath} className="emu-hit-ring" strokeWidth="17" fill="none"/><path d={cablePath} stroke="currentColor" strokeWidth="4" fill="none" strokeDasharray="7 4"/></Hit>
    <Hit {...props} node="ctrl" label="Шкаф управления"><rect className="emu-hit-ring" x="9" y="215" width="70" height="97" rx="5"/><rect x="16" y="221" width="56" height="84" rx="3" fill="var(--equipment-surface)" stroke="currentColor"/><text x="44" y="237" className="emu-svg-label" textAnchor="middle">Шкаф</text><circle cx="44" cy="261" r="12" fill="var(--equipment-joint)" stroke="currentColor"/><path d="M34 261h20M44 251v20" stroke="currentColor"/><rect x="31" y="282" width="26" height="11" rx="2" fill="var(--equipment-metal)" stroke="currentColor"/></Hit>
    <Hit {...props} node="tool" label="Оснастка робота"><g transform={`translate(${path(p6)}) rotate(${a5})`}><circle className="emu-hit-ring" r="42" cy="-29"/><rect x="-4" y="-20" width="8" height="20" fill="var(--equipment-metal)"/>
      {asset==='R1'?<><rect x="-27" y="-28" width="54" height="8" fill="var(--equipment-metal)" stroke="currentColor"/>{[-22,0,22].map(x=><rect key={x} x={x-5} y="-34" width="10" height="6" fill="currentColor"/>)}<rect x="-35" y="-38" width="70" height="4" fill="var(--equipment-glass)"/></>:asset==='R2'?<><rect x="-14" y="-27" width="28" height="13" fill="var(--equipment-metal)" stroke="currentColor"/><circle cy="-44" r="18" fill="var(--equipment-tire)" stroke="currentColor"/><circle cy="-44" r="8" fill="var(--equipment-metal-light)"/></>:asset==='R3'?<><rect x="-20" y="-23" width="40" height="6" fill="var(--equipment-metal)" stroke="currentColor"/><path d="M-18-25H18V-34H-7V-57H-18Z" fill="var(--equipment-metal-light)" stroke="currentColor"/></>:<><rect x="-29" y="-23" width="58" height="7" fill="var(--equipment-metal)"/><rect x="-36" y="-45" width="72" height="22" rx="3" fill="var(--equipment-metal)" stroke="currentColor"/><path d="M-26-33H-5M5-33H26" stroke="var(--equipment-surface)"/></>}
    </g></Hit>
    {Object.entries(points).map(([node,p])=><Hit key={node} {...props} node={node} label={`Ось ${node}`}><g transform={`translate(${path(p)})`}><circle className="emu-hit-ring" r="19"/><circle r="13" fill="var(--equipment-joint)" stroke="currentColor" strokeWidth="2"/><text className="emu-svg-label" textAnchor="middle" dy="4">{node}</text></g></Hit>)}
    {Object.entries({...points,cable,tool,ctrl:[72,221] as XY}).map(([node,p])=><Flag key={node} at={[p[0]+14,p[1]-15]} level={levelOf(components,node)}/>)}
  </svg><figcaption>Нажмите на ось, кабель, оснастку или шкаф. Цвет и знак «!» показывают состояние узла; выделение — текущий выбор.</figcaption></figure>;
}

export function ConveyorSchematic({data,motion,selected,choose}:{data:State;motion:boolean;selected:string;choose:(node:string)=>void}){
  const phase=cycleParts(useCycle(data,motion)).m;
  const components=data.components.filter(c=>c.robot==='CV'),props={components,selected,choose};
  const c=(id:string)=>colors[components.find(c=>c.id===`CV-${id}`)?.level??'ok'];
  const lube=components.find(c=>c.id==='CV-lube')?.sensors.find(s=>s.k==='lvl')?.value??0;
  const body='M-30 0V-11Q-29-15-22-15L-12-26H10L19-15H26Q30-14 30-9V0Z';
  return <figure className="emu-schematic"><svg viewBox="0 0 520 255" role="group" aria-label="Схема конвейера: привод, цепь, натяжитель и датчики">
    {[110,190,270,350].map((x,i)=><g key={x}><text className="emu-svg-label" x={x} y="24" textAnchor="middle">R{i+1}</text><line x1={x} x2={x} y1="31" y2="69" stroke="var(--equipment-metal)" strokeDasharray="3 4"/></g>)}
    <Hit {...props} node="chain" label="Цепь и настил конвейера"><rect className="emu-hit-ring" x="20" y="97" width="419" height="65" rx="30"/><path d="M48 109H410A21 21 0 0 1 410 151H48A21 21 0 0 1 48 109Z" fill="var(--equipment-surface)" stroke={c('chain')} strokeWidth="5" strokeDasharray="11 5" strokeDashoffset={-phase*80}/>{Array.from({length:14},(_,i)=><circle key={i} cx={72+i*23} cy="118" r="4" fill="var(--equipment-joint)" stroke={c('rollers')}/>)}<rect x="318" y="184" width="28" height="33" rx="2" fill="var(--equipment-surface)" stroke={c('lube')}/><rect x="321" y={214-lube*.27} width="22" height={lube*.27} fill="var(--equipment-ok)" opacity=".7"/><path d="M332 184v-26" stroke="var(--equipment-metal-light)"/><text className="emu-svg-label" x="332" y="234" textAnchor="middle">Смазка {number(lube)}%</text></Hit>
    {Array.from({length:6},(_,i)=>30+i*80+phase*80).filter(x=>x>40&&x<391).map((x,i)=><g key={i} transform={`translate(${x} 102)`} pointerEvents="none"><path d={body} fill="var(--equipment-body)" stroke="var(--equipment-body-border)"/>{x>112&&<path d="M10-26L19-15" stroke="var(--equipment-glass)" strokeWidth="3"/>}{x>192&&<><circle cx="-18" cy="-1" r="5" fill="var(--equipment-tire)"/><circle cx="18" cy="-1" r="5" fill="var(--equipment-tire)"/></>}{x>272&&<rect x="-6" y="-22" width="7" height="7" fill="var(--equipment-metal)"/>}</g>)}
    <Hit {...props} node="take" label="Натяжная станция"><rect className="emu-hit-ring" x="2" y="106" width="74" height="65" rx="7"/><circle cx="48" cy="130" r="18" fill="var(--equipment-surface)" stroke="currentColor"/><circle cx="48" cy="130" r="5" fill="var(--equipment-metal-light)"/><rect x="7" y="122" width="18" height="16" fill="var(--equipment-metal)" stroke="currentColor"/><path d="M25 130h20" stroke="currentColor" strokeWidth="4"/><text className="emu-svg-label" x="31" y="189" textAnchor="middle">Натяжка</text></Hit>
    <Hit {...props} node="drive" label="Привод конвейера"><rect className="emu-hit-ring" x="389" y="47" width="115" height="151" rx="8"/><circle cx="410" cy="130" r="18" fill="var(--equipment-surface)" stroke={c('sprk')}/><circle cx="410" cy="130" r="5" fill="var(--equipment-metal-light)"/><path d="M414 130h30" stroke="currentColor" strokeWidth="4"/><rect x="444" y="115" width="44" height="29" rx="3" fill="var(--equipment-surface)" stroke={c('cvgbx')}/><circle cx="479" cy="130" r="4" fill={c('cvoil')}/><rect x="448" y="151" width="33" height="23" rx="3" fill="var(--equipment-surface)" stroke={c('cvmot')}/><rect x="481" y="154" width="8" height="17" fill="var(--equipment-surface)" stroke={c('cvbrk')}/><path d="M454 154v17M461 154v17M468 154v17M464 144v7" stroke="var(--equipment-metal-light)"/><rect x="446" y="54" width="38" height="43" rx="3" fill="var(--equipment-surface)" stroke={c('vfd')}/><text x="465" y="80" className="emu-svg-label" textAnchor="middle">ЧП</text><path d="M484 76h12v87h-7" stroke="var(--equipment-metal-light)" fill="none"/><text x="466" y="193" className="emu-svg-label" textAnchor="middle">Привод</text></Hit>
    <Hit {...props} node="track" label="Энкодер и датчики позиции"><rect className="emu-hit-ring" x="77" y="124" width="282" height="79" rx="8"/>{[110,190,270,350].map(x=><path key={x} d={`M${x-6} 139h12l-6-12Z`} fill={c('pos')}/>)}<circle cx="90" cy="165" r="8" fill="var(--equipment-surface)" stroke={c('enc')}/><path d="M90 173v16m-10 0h20" stroke="currentColor"/><text x="94" y="219" className="emu-svg-label" textAnchor="middle">Энкодер</text></Hit>
    {Object.entries({chain:[236,157],drive:[495,49],take:[14,108],track:[124,145]} as Record<string,XY>).map(([node,p])=><Flag key={node} at={p} level={levelOf(components,node)}/>)}
  </svg><figcaption>Пластинчатый конвейер · шаг кузовов 6 м · детали кузова добавляются по мере прохождения станций. Выберите узел на схеме.</figcaption></figure>;
}
