import {createContext, useContext, useEffect, useState, type ReactNode} from 'react';
import {flushSync} from 'react-dom';

export type Theme = 'dark' | 'light';
const STORAGE_KEY = 'allur-theme';
const ThemeContext = createContext<{theme:Theme; toggle:(origin?:HTMLElement|null)=>void} | null>(null);
const isTheme = (value:unknown):value is Theme => value === 'dark' || value === 'light';

function savedTheme():Theme {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (isTheme(saved)) return saved;
  } catch { /* The current tab still supports themes when browser storage is unavailable. */ }
  return 'dark';
}

function applyTheme(theme:Theme) {
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
}

/** Apply the saved palette before React mounts, including the login screen. */
export function initializeTheme() { applyTheme(savedTheme()); }

export function ThemeProvider({children}:{children:ReactNode}) {
  const [theme,setTheme] = useState<Theme>(savedTheme);
  useEffect(() => {
    applyTheme(theme);
    try { localStorage.setItem(STORAGE_KEY,theme); } catch { /* Keep the in-memory choice. */ }
  },[theme]);
  useEffect(() => {
    const sync = (event:StorageEvent) => {
      if (event.key === STORAGE_KEY || event.key === null) setTheme(isTheme(event.newValue) ? event.newValue : 'dark');
    };
    window.addEventListener('storage',sync);
    return () => window.removeEventListener('storage',sync);
  },[]);
  /** Switch palettes with a circular reveal that grows from the toggle (View Transitions; instant elsewhere). */
  const toggle = (origin?:HTMLElement|null) => {
    const next:Theme = theme === 'dark' ? 'light' : 'dark';
    const doc = document as Document & {startViewTransition?:(update:()=>void)=>{ready:Promise<void>; finished:Promise<void>}};
    let reduce = false;
    try { reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch { /* Treat as full motion. */ }
    if (!doc.startViewTransition || reduce || document.hidden) { setTheme(next); return; }
    const rect = origin?.getBoundingClientRect();
    const x = rect ? rect.left + rect.width / 2 : window.innerWidth - 48;
    const y = rect ? rect.top + rect.height / 2 : 32;
    const radius = Math.hypot(Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y));
    const root = document.documentElement;
    root.dataset.themeSwitching = '';
    try {
      const transition = doc.startViewTransition(() => { applyTheme(next); flushSync(() => setTheme(next)); });
      transition.ready.then(() => root.animate(
        {clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`]},
        {duration: 620, easing: 'cubic-bezier(.65,0,.35,1)', pseudoElement: '::view-transition-new(root)'},
      )).catch(() => {});
      transition.finished.catch(() => {}).finally(() => { delete root.dataset.themeSwitching; });
    } catch {
      delete root.dataset.themeSwitching; setTheme(next);
    }
  };
  return <ThemeContext.Provider value={{theme,toggle}}>{children}</ThemeContext.Provider>;
}

export function ThemeToggle() {
  const context = useContext(ThemeContext);
  if (!context) throw new Error('ThemeToggle requires ThemeProvider');
  const {theme,toggle} = context;
  const label = theme === 'dark' ? 'Переключить на светлую тему' : 'Переключить на тёмную тему';
  return <button type="button" className="theme-toggle" onClick={event => toggle(event.currentTarget)} aria-label={label} title={label} aria-pressed={theme==='light'} data-testid="theme-toggle">
    <svg key={theme} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {theme === 'dark' ? <path d="M20.8 13.1A9 9 0 0 1 10.9 3.2 9 9 0 1 0 20.8 13.1Z"/> : <><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></>}
    </svg>
    <span>{theme==='dark'?'Тёмная':'Светлая'}</span>
  </button>;
}
