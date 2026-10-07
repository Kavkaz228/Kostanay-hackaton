import {useState} from 'react';
import {post,request} from './api';
import {useLive} from './Manufacturing';
import type {AutomationData} from './Automation';
type Key={id:string;robot_id:string;expires:number;active:boolean};
export function GatewayAdministration(){
  const keys=useLive<Key[]>('/admin/gateways',15000), data=useLive<AutomationData>('/automation',5000);
  const [robot,setRobot]=useState(''),[secret,setSecret]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [command,setCommand]=useState(''),[evidence,setEvidence]=useState(''),[result,setResult]=useState('failed'),[checked,setChecked]=useState(false);
  async function act(work:()=>Promise<unknown>){setBusy(true);setError('');try{await work();keys.refresh();data.refresh();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  return <><h3>Заводские шлюзы</h3><p>Каждый ключ привязан к одному роботу. Он разрешает передачу его измерений, получение уже подтверждённой команды и возврат результата. Сам ключ не включает физическое управление.</p>
    <form className="access-grid" onSubmit={e=>{e.preventDefault();void act(async()=>{const value=await post<{token:string}>('/admin/gateways',{robot_id:robot,days:30});setSecret(value.token);});}}><label>Робот заводского шлюза<select required value={robot} onChange={e=>setRobot(e.target.value)}><option value="">Выберите робота</option>{data.value?.robots.map(r=><option key={r.robot_id} value={r.robot_id}>{r.robot_id}</option>)}</select></label><button className="button primary" disabled={busy||!robot}>Создать ключ шлюза на 30 дней</button></form>
    {secret&&<label>Ключ шлюза — сохраните сейчас<input readOnly value={secret} onFocus={e=>e.target.select()}/></label>}
    {keys.value?.map(k=><div className="access-user" key={k.id}><strong>{k.robot_id}</strong><span>{k.active?'Активен':'Отозван'} · до {new Date(k.expires*1000).toLocaleDateString('ru-RU')}</span><button className="button secondary" disabled={busy||!k.active} onClick={()=>void act(()=>request(`/admin/gateways/${k.id}`,{method:'DELETE'}))}>Отозвать ключ шлюза</button></div>)}
    {data.value?.commands.some(c=>c.status==='uncertain')&&<><h3>Сверка результата с контроллером</h3><p>Выполняйте после проверки журнала контроллера и фактического состояния оборудования. Сверка снимает блокировку следующей команды, но ничего не отправляет роботу.</p><form className="access-grid" onSubmit={e=>{e.preventDefault();void act(async()=>{await post(`/admin/commands/${command}/reconcile`,{confirm:true,result,evidence});setCommand('');setEvidence('');setChecked(false);});}}><label>Команда с неизвестным результатом<select required value={command} onChange={e=>setCommand(e.target.value)}><option value="">Выберите команду</option>{data.value.commands.filter(c=>c.status==='uncertain').map(c=><option key={c.id} value={c.id}>{c.robot_id} · {c.id}</option>)}</select></label><label>Подтверждённый результат<select value={result} onChange={e=>setResult(e.target.value)}><option value="failed">Не выполнена</option><option value="succeeded">Выполнена</option></select></label><label>Основание сверки<input required minLength={20} maxLength={500} value={evidence} onChange={e=>setEvidence(e.target.value)}/></label><label><input type="checkbox" checked={checked} onChange={e=>setChecked(e.target.checked)}/> Проверка контроллера завершена</label><button className="button primary" disabled={busy||!checked||!command}>Сохранить результат сверки</button></form></>}
    {(error||keys.error)&&<p role="alert">{error||keys.error}</p>}
  </>;
}
