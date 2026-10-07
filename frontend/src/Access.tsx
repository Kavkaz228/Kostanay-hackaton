import { Component, useEffect, useState, type FormEvent, type ReactNode } from 'react';
import App from './App';
import {ThemeToggle} from './Theme';
import {Permissions} from './permissions';
import { post, request, setCsrf } from './api';
import {GatewayAdministration} from './GatewayAdministration';

type User = {id: string; username: string; role: string; active: boolean; must_change: boolean; csrf?: string};
type Token = {id: string; name: string; active: boolean; expires: number};
type Audit = {id: number; timestamp: number; actor: string; action: string; detail: string};
type SystemStatus = {version: string; storage_version: number; users: number; active_sessions: number; robot_measurements: number; database_bytes: number | null; worker_alive: boolean; last_worker_error: string | null; secure_cookie: boolean};
const roles: Record<string, string> = {admin: 'Администратор', operator: 'Оператор', viewer: 'Наблюдатель'};

export class ErrorBoundary extends Component<{children: ReactNode}, {failed: boolean}> {
  state = {failed: false};
  static getDerivedStateFromError() {return {failed: true};}
  render() {return this.state.failed ? <div className="access-card"><h1>Не удалось открыть экран</h1><p>Сохранённые данные остаются на сервере. Перезагрузите страницу; если ошибка повторится, обратитесь к администратору.</p><button className="button primary" onClick={() => location.reload()}>Перезагрузить</button></div> : this.props.children;}
}

export default function Access() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [nextPassword, setNextPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [panel, setPanel] = useState(false);
  const [change, setChange] = useState(false);
  useEffect(() => {
    if (!panel) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const close = (event: KeyboardEvent) => {if (event.key === 'Escape') setPanel(false);};
    window.addEventListener('keydown', close);
    return () => {document.body.style.overflow = previous; window.removeEventListener('keydown', close);};
  }, [panel]);
  useEffect(() => {
    const expired = () => {setUser(null); setCsrf(''); setError('Сессия завершена. Войдите снова.');};
    window.addEventListener('allur:unauthorized', expired);
    request<User>('/auth/me').then(value => {setUser(value); setCsrf(value.csrf || '');}).catch((e: Error) => {if (!e.message.includes('Войдите')) setError(e.message);}).finally(() => setLoading(false));
    return () => window.removeEventListener('allur:unauthorized', expired);
  }, []);
  async function submit(event: FormEvent) {
    event.preventDefault(); setError(''); setBusy(true);
    try {
      if (user) {
        await post('/auth/password', {current_password: password, new_password: nextPassword});
        setUser(null); setCsrf(''); setChange(false); setError('Пароль изменён. Войдите с новым паролем.');
      } else {
        const value = await post<User>('/auth/login', {username, password});
        setCsrf(value.csrf || ''); setUser(value);
      }
      setPassword(''); setNextPassword('');
    } catch (e) {setError((e as Error).message);} finally {setBusy(false);}
  }
  async function logout() {
    try {await post('/auth/logout', {}); setUser(null); setPanel(false); setCsrf('');} catch(e) {setError((e as Error).message);}
  }
  if (loading) return <div className="access-card">Проверяем сессию…</div>;
  if (!user || user.must_change || change) return <div className="access-screen"><div className="access-backdrop" aria-hidden="true"><span className="access-lines"/><span className="access-glow one"/><span className="access-glow two"/><span className="access-scan"/></div><div className="access-theme"><ThemeToggle/></div><form className="access-card" onSubmit={submit}>
    <span className="access-brand" aria-hidden="true"><span className="brand-symbol"><i/><i/><i/></span><span>allur<span className="brand-twin">twin</span></span></span>
    <span className="heading-kicker">Allur twin 2.0 · ЗАЩИЩЁННЫЙ ДОСТУП</span><h1>{user ? 'Изменение пароля' : 'Вход в систему'}</h1>
    <p>{user ? 'Используйте уникальную парольную фразу от 15 символов. После изменения все ваши сессии будут завершены.' : 'Используйте учётную запись вашей организации.'}</p>
    {!user && <label>Имя пользователя<input autoComplete="username" value={username} required maxLength={80} onChange={e => setUsername(e.target.value)}/></label>}
    <label>{user ? 'Текущий пароль' : 'Пароль'}<input type="password" autoComplete="current-password" value={password} required maxLength={128} onChange={e => setPassword(e.target.value)}/></label>
    {user && <label>Новый пароль<input type="password" autoComplete="new-password" value={nextPassword} required minLength={15} maxLength={128} onChange={e => setNextPassword(e.target.value)}/></label>}
    {error && <p role="alert" className="form-error" key={error}>{error}</p>}<button className="button primary" disabled={busy}>{busy ? 'Подождите…' : user ? 'Сохранить новый пароль' : 'Войти'}</button>
    {user && !user.must_change && <button type="button" className="button secondary" onClick={() => setChange(false)}>Отмена</button>}
  </form></div>;
  const account = <button type="button" className="operator-avatar" aria-label="Учётная запись" title={`${user.username} · ${roles[user.role]}`} onClick={() => setPanel(true)}>{user.username.slice(0,1).toLocaleUpperCase()}</button>;
  return <><Permissions.Provider value={user.role !== 'viewer'}><App account={account} canWrite={user.role !== 'viewer'}/></Permissions.Provider>{panel && <div className="access-overlay"><section className="access-panel" role="dialog" aria-modal="true" aria-label="Управление доступом">
    <div className="section-heading"><h2>Управление доступом</h2><button className="button secondary" onClick={() => setPanel(false)}>Закрыть</button></div>
    <p>{user.username} · {roles[user.role]}. Сессия действует до 8 часов.</p><div className="account-actions"><button className="button secondary" onClick={() => {setChange(true); setPanel(false);}}>Изменить пароль</button><button className="button secondary" onClick={() => void logout()}>Выйти</button></div>
    {error && <p role="alert">{error}</p>}{user.role === 'admin' && <Administration current={user}/>}
  </section></div>}</>;
}

function Administration({current}: {current: User}) {
  const [users, setUsers] = useState<User[]>([]);
  const [tokens, setTokens] = useState<Token[]>([]);
  const [events, setEvents] = useState<Audit[]>([]);
  const [system, setSystem] = useState<SystemStatus | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState('viewer');
  const [tokenName, setTokenName] = useState('');
  const [secret, setSecret] = useState('');
  const [resetUser, setResetUser] = useState('');
  const [resetPassword, setResetPassword] = useState('');
  async function load() {const [u,t,a,s] = await Promise.all([request<User[]>('/admin/users'), request<Token[]>('/admin/tokens'), request<Audit[]>('/admin/audit'), request<SystemStatus>('/admin/system')]); setUsers(u); setTokens(t); setEvents(a); setSystem(s);}
  useEffect(() => {void load().catch(e => setError(e.message));}, []);
  async function act(operation: () => Promise<unknown>) {setBusy(true); setError(''); try {await operation(); await load();} catch(e) {setError((e as Error).message);} finally {setBusy(false);}}
  function edit(user: User, body: object) {return request(`/admin/users/${user.id}`, {method: 'PATCH', body: JSON.stringify(body)});}
  return <>{system && <><h3>Состояние установки · v{system.version}</h3><p>Пользователей: {system.users} · сессий: {system.active_sessions} · измерений: {system.robot_measurements.toLocaleString('ru-RU')} · база: {system.database_bytes === null ? '—' : (system.database_bytes/1024/1024).toFixed(1) + ' МБ'}. Обработчик: {system.worker_alive && !system.last_worker_error ? 'работает' : 'требует проверки'}. {system.secure_cookie ? 'Защищённые HTTPS-сессии.' : 'Локальный HTTP-режим; для сетевого размещения включите HTTPS.'}</p></>}
    <h3>Пользователи</h3><p>Наблюдатель читает данные; оператор импортирует и управляет моделью; администратор дополнительно управляет доступом. Изменение роли, блокировка или сброс пароля немедленно завершают сессии пользователя.</p>
    {error && <p role="alert" className="form-error">{error}</p>}
    <form className="access-grid" onSubmit={e => {e.preventDefault(); void act(async () => {await post('/admin/users', {username: name, password, role}); setName(''); setPassword('');});}}>
      <label>Логин нового пользователя<input required minLength={3} maxLength={80} pattern="[a-zA-Z0-9][a-zA-Z0-9_.@\-]{2,79}" value={name} onChange={e => setName(e.target.value)}/></label>
      <label>Временный пароль<input type="password" autoComplete="new-password" required minLength={15} maxLength={128} value={password} onChange={e => setPassword(e.target.value)}/></label>
      <label>Роль<select value={role} onChange={e => setRole(e.target.value)}>{Object.entries(roles).map(([id,label]) => <option key={id} value={id}>{label}</option>)}</select></label><button className="button primary" disabled={busy}>Создать пользователя</button>
    </form>
    <div className="access-users">{users.map(user => <div key={user.id} className="access-user"><strong>{user.username}</strong><span>{user.active ? 'Активен' : 'Заблокирован'}{user.must_change ? ' · временный пароль' : ''}</span>
      <select aria-label={`Роль ${user.username}`} value={user.role} disabled={busy || user.id === current.id} onChange={e => void act(() => edit(user, {role: e.target.value}))}>{Object.entries(roles).map(([id,label]) => <option key={id} value={id}>{label}</option>)}</select>
      <button className="button secondary compact" disabled={busy || user.id === current.id} onClick={() => void act(() => edit(user, {active: !user.active}))}>{user.active ? 'Заблокировать' : 'Разблокировать'}</button>
      <button className="button tertiary compact" disabled={busy || user.id === current.id} onClick={() => {setResetUser(user.id); setResetPassword('');}}>Сбросить пароль</button>
    </div>)}</div>
    {resetUser && <form className="access-grid" onSubmit={e => {e.preventDefault(); void act(async () => {await request(`/admin/users/${resetUser}`, {method: 'PATCH', body: JSON.stringify({password: resetPassword})}); setResetUser(''); setResetPassword('');});}}><label>Новый временный пароль<input type="password" minLength={15} maxLength={128} required value={resetPassword} onChange={e => setResetPassword(e.target.value)}/></label><button className="button primary" disabled={busy}>Подтвердить сброс</button><button className="button secondary" type="button" onClick={() => setResetUser('')}>Отмена</button></form>}
    <h3>Ключи интеграций</h3><p>Ключ позволяет отправлять измерения роботов на /api/robots/measurements с заголовком Idempotency-Key и показания узлов на /api/scada/readings с уникальным record_id каждой записи. Создавайте отдельный ключ для каждого источника.</p>
    <form className="access-grid" onSubmit={e => {e.preventDefault(); void act(async () => {const result = await post<{token: string}>('/admin/tokens', {name: tokenName, days: 90}); setSecret(result.token); setTokenName('');});}}><label>Название источника<input required maxLength={80} value={tokenName} onChange={e => setTokenName(e.target.value)}/></label><button className="button primary" disabled={busy}>Создать ключ на 90 дней</button></form>
    {secret && <label>Сохраните ключ сейчас — повторно он не отображается<input readOnly value={secret} onFocus={e => e.target.select()}/></label>}
    {tokens.map(t => <div className="access-user" key={t.id}><strong>{t.name}</strong><span>До {new Date(t.expires*1000).toLocaleDateString('ru-RU')} · {t.active ? 'Активен' : 'Отозван'}</span><button className="button secondary compact" disabled={busy || !t.active} onClick={() => void act(() => request(`/admin/tokens/${t.id}`, {method: 'DELETE'}))}>Отозвать</button></div>)}
    <GatewayAdministration/>
    <h3>Журнал действий</h3><button className="button secondary compact" disabled={busy} onClick={() => void act(async () => {})}>Обновить журнал</button>
    <div className="audit-list">{events.map(event => <p key={event.id}><time>{new Date(event.timestamp*1000).toLocaleString('ru-RU')}</time> <strong>{event.actor}</strong> · {event.action}<br/>{event.detail}</p>)}</div>
    {events.length >= 200 && <button className="button secondary" disabled={busy} onClick={async () => {setBusy(true); try {setEvents(await request<Audit[]>(`/admin/audit?before=${events[events.length-1].id}`));} catch(e) {setError((e as Error).message);} finally {setBusy(false);}}}>Предыдущие события</button>}
  </>;
}
