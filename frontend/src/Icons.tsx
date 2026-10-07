import type { CSSProperties } from 'react';

export type IconName = 'grid' | 'flask' | 'bell' | 'database' | 'arrow' | 'arrowUp' | 'play' | 'pause' | 'clock' | 'check' | 'close' | 'chevron' | 'factory' | 'gear' | 'bolt' | 'activity' | 'download' | 'upload' | 'refresh' | 'plus' | 'trash' | 'warning' | 'info' | 'layers' | 'target' | 'menu' | 'box';
const paths: Record<IconName, React.ReactNode> = {
  grid: <><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></>,
  flask: <><path d="M9 3h6M10 3v7l-6 9a1.3 1.3 0 0 0 1 2h14a1.3 1.3 0 0 0 1-2l-6-9V3M7.5 15h9"/></>,
  bell: <><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></>,
  database: <><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/></>,
  arrow: <path d="M4 12h16m-6-6 6 6-6 6"/>,
  arrowUp: <path d="M6 18 18 6M6 6h12v12"/>,
  play: <path d="m8 4 13 8-13 8Z"/>,
  pause: <><path d="M8 4v16M16 4v16"/></>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  check: <path d="m5 12 4 4L19 6"/>,
  close: <path d="m6 6 12 12M6 18 18 6"/>,
  chevron: <path d="m9 5 7 7-7 7"/>,
  factory: <><path d="M3 21V9l6 4V9l6 4V3h5l1 18ZM7 17v1m5-1v1m5-1v1"/></>,
  gear: <><path d="m9 3-.5 3-2 .8L4 5.5 1.5 10 4 12l-.1 2-2.4 2L4 20.5 7 19l2 .8.5 3h5l.5-3 2-.8 3 1.5 2.5-4.5-2.4-2-.1-2 2.5-2L20 5.5l-2.5 1.3-2-.8-.5-3Z"/><circle cx="12" cy="13" r="3"/></>,
  bolt: <path d="m13 2-9 12h7l-1 8 10-12h-7Z"/>,
  activity: <path d="M2 12h5l3-8 4 16 3-8h5"/>,
  download: <><path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/></>,
  upload: <><path d="M12 16V4m-5 5 5-5 5 5M4 16v5h16v-5"/></>,
  refresh: <><path d="M20 8a8 8 0 1 0 0 8M20 3v5h-5"/></>,
  plus: <path d="M12 5v14M5 12h14"/>,
  trash: <><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/></>,
  warning: <><path d="m12 3 10 18H2ZM12 9v5m0 3v.5"/></>,
  info: <><circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v.5"/></>,
  layers: <><path d="m12 3 10 5-10 5L2 8Zm-10 9 10 5 10-5M2 16l10 5 10-5"/></>,
  target: <><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/></>,
  menu: <path d="M4 6h16M4 12h16M4 18h16"/>,
  box: <><path d="m12 2 9 5v10l-9 5-9-5V7Zm0 10 9-5m-9 5L3 7m9 5v10M7.5 4.5l9 5"/></>,
};
export function Icon({name, size = 20, className = '', style}: {name: IconName; size?: number; className?: string; style?: CSSProperties}) {
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" className={className ? `icon-${name} ${className}` : `icon-${name}`} style={style}>{paths[name]}</svg>;
}
