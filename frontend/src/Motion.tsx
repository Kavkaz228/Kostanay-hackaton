import { useEffect, useRef, useState } from 'react';
import { number } from './api';

/** Motion helpers. All of them fall back to static output when the user prefers reduced motion. */
export function reducedMotion() {
  try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch { return false; }
}

const easeOut = (t: number) => 1 - Math.pow(1 - t, 3);

/** Smoothly moves a displayed value towards its target. Unknown values are passed through unchanged. */
export function useTweenedNumber(target: unknown, duration = 700): unknown {
  const numeric = typeof target === 'number' && Number.isFinite(target) ? target : null;
  const [display, setDisplay] = useState<number | null>(() => numeric === null ? null : reducedMotion() ? numeric : 0);
  const current = useRef<number | null>(display);
  useEffect(() => {
    if (numeric === null) { current.current = null; setDisplay(null); return; }
    const from = current.current;
    if (from === null || from === numeric || reducedMotion() || document.hidden) { current.current = numeric; setDisplay(numeric); return; }
    let frame = 0;
    const start = performance.now();
    const step = (now: number) => {
      const progress = Math.min(1, (now - start) / duration);
      const value = progress >= 1 ? numeric : from + (numeric - from) * easeOut(progress);
      current.current = value; setDisplay(value);
      if (progress < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [numeric, duration]);
  return numeric === null ? target : display;
}

/** A number formatted like `number()` that counts towards new values instead of jumping. */
export function AnimatedNumber({value, digits = 0, duration}: {value: unknown; digits?: number; duration?: number}) {
  const shown = useTweenedNumber(value, duration);
  return <>{number(shown, digits)}</>;
}

const SPOTLIGHT = '.metric-card, .station-card, .operation-card, .source-option';

/** One delegated listener lets cards follow the pointer with a soft highlight (desktop pointers only). */
export function installSpotlight() {
  if (typeof window === 'undefined' || !window.matchMedia?.('(hover: hover) and (pointer: fine)').matches) return;
  let frame = 0;
  let last: PointerEvent | null = null;
  document.addEventListener('pointermove', event => {
    last = event;
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      const target = last?.target instanceof Element ? last.target.closest<HTMLElement>(SPOTLIGHT) : null;
      if (!target || !last) return;
      const rect = target.getBoundingClientRect();
      target.style.setProperty('--mx', `${Math.round(last.clientX - rect.left)}px`);
      target.style.setProperty('--my', `${Math.round(last.clientY - rect.top)}px`);
    });
  }, {passive: true});
}
