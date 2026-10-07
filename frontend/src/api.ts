let csrf = '';
export function setCsrf(value: string) {csrf = value;}

function readableError(detail: unknown): string {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map(readableError).join('\n');
  if (detail && typeof detail === 'object') {
    const item = detail as Record<string, unknown>;
    if (item.msg) return `${Array.isArray(item.loc) ? item.loc.join(' → ') + ': ' : ''}${item.msg}`;
    if (item.message) return `${typeof item.row === 'number' && item.row > 0 ? `Строка ${item.row}: ` : ''}${String(item.message)}`;
    if (item.errors) return readableError(item.errors);
    return JSON.stringify(item);
  }
  return 'Не удалось выполнить запрос. Повторите попытку.';
}

export async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (options?.signal?.aborted) controller.abort();
  else options?.signal?.addEventListener('abort', abort, {once: true});
  const timeout = setTimeout(() => controller.abort(), (path === '/scenarios' || path === '/ai/analyze') && options?.method === 'POST' ? 180000 : 20000);
  try {
    const response = await fetch(`/api${path}`, {...options, signal: controller.signal,
      headers: {...(options?.body instanceof FormData ? {} : {'Content-Type': 'application/json'}), ...(csrf ? {'X-CSRF-Token': csrf} : {}), ...options?.headers}});
    if (!response.ok) {
      if (response.status === 401 && path !== '/auth/login' && path !== '/auth/me') window.dispatchEvent(new Event('allur:unauthorized'));
      const body = await response.json().catch(() => null);
      const message = body ? readableError(body.detail ?? body.message ?? body) : `Ошибка сервера (${response.status})`;
      throw new Error(message + (response.status >= 500 && response.headers.get('X-Request-ID') ? ` Номер запроса: ${response.headers.get('X-Request-ID')}` : ''));
    }
    return await response.json() as T;
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') throw new Error('Сервер не ответил вовремя. Проверьте соединение и повторите запрос.');
    throw error;
  } finally { clearTimeout(timeout); options?.signal?.removeEventListener('abort', abort); }
}

export const post = <T>(path: string, body: unknown) => request<T>(path, {method: 'POST', body: JSON.stringify(body)});
export const finite = (value: unknown, fallback = 0): number => typeof value === 'number' && Number.isFinite(value) ? value : fallback;
export const number = (value: unknown, digits = 0) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('ru-RU', {maximumFractionDigits: digits}) : '—';
export const elapsed = (seconds: unknown): string => {
  const value = Math.max(0, Math.floor(finite(seconds)));
  return `${String(Math.floor(value / 3600)).padStart(2, '0')}:${String(Math.floor(value / 60) % 60).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
};
export const date = (value: string | null | undefined) => {
  if (!value) return 'Нет данных';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 'Нет данных' : parsed.toLocaleString('ru-RU', {timeZone: 'Asia/Qyzylorda', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'});
};
