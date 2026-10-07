import { test as base, expect, type APIRequestContext, type APIResponse, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';

interface Station {
  id: string;
  cycle_seconds: number;
  manual_stop: boolean;
  status: string;
  queue: number;
  completed: number;
  rejected: number;
}
interface State {
  run_id: string;
  source: string;
  running: boolean;
  sim_time: number;
  stations: Station[];
  metrics: { good: number; produced: number; rejected: number; wip: number };
  telemetry_updated_at?: string | null; telemetry_available?: {production: boolean; robots: boolean};
}

const test = base.extend<{ javascriptErrors: string[] }>({
  request: async ({ playwright, baseURL }, use) => {
    const context = await playwright.request.newContext({baseURL, storageState: '/tmp/allur-auth.json', extraHTTPHeaders: {'X-CSRF-Token': readFileSync('/tmp/allur-csrf', 'utf8')}});
    await use(context);
    await context.dispose();
  },
  javascriptErrors: [async ({ page }, use) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await use(errors);
    expect(errors, 'The browser must not report uncaught JavaScript exceptions.').toEqual([]);
  }, { auto: true }],
});

async function json<T = any>(response: APIResponse): Promise<T> {
  expect(response.ok(), `${response.status()} ${response.url()}: ${await response.text()}`).toBeTruthy();
  return response.json();
}

async function state(request: APIRequestContext): Promise<State> {
  return json<State>(await request.get('/api/state'));
}

async function action(page: Page, path: string, callback: () => Promise<unknown>, method = 'POST'): Promise<any> {
  const pending = page.waitForResponse(response => {
    return new URL(response.url()).pathname === path && response.request().method() === method;
  });
  await callback();
  const response = await pending;
  expect(response.ok(), `${response.status()} ${path}: ${await response.text()}`).toBeTruthy();
  return response.json();
}

async function openTab(page: Page, name: 'overview' | 'scenarios' | 'incidents' | 'data') {
  const menuButton = page.getByRole('button', { name: 'Открыть меню', exact: true });
  const mobile = await menuButton.isVisible();
  if (mobile) {
    await expect(menuButton).toHaveAttribute('aria-expanded', 'false');
    await expect(menuButton).toHaveAttribute('aria-controls', 'main-sidebar');
    await menuButton.click();
    await expect(menuButton).toHaveAttribute('aria-expanded', 'true');
  }
  await page.getByTestId(`nav-${name}`).click();
  if (mobile) {
    await expect(menuButton).toHaveAttribute('aria-expanded', 'false');
    await expect.poll(async () => {
      const bounds = await page.locator('#main-sidebar').boundingBox();
      return bounds ? bounds.x + bounds.width : Infinity;
    }, { message: 'The mobile sidebar must finish closing and leave the content unobstructed.' }).toBeLessThanOrEqual(0);
    await expect(page.getByRole('button', { name: 'Закрыть меню', exact: true })).toHaveCount(0);
  }
}

function displayedNumber(value: number) {
  return new Intl.NumberFormat('ru-RU').format(value);
}

const headers = 'timestamp,station_id,status,produced,rejected,queue,cycle_seconds';
const stationIds = ['warehouse', 'welding', 'painting', 'assembly', 'quality'];
const defaults = [
  { id: 'warehouse', cycle: 36, buffer: 16 },
  { id: 'welding', cycle: 44, buffer: 12 },
  { id: 'painting', cycle: 52, buffer: 10 },
  { id: 'assembly', cycle: 48, buffer: 12 },
  { id: 'quality', cycle: 38, buffer: 10 },
];

test.beforeEach(async ({ request, page }) => {
  await json(await request.get('/api/health'));
  await json(await request.post('/api/source', { data: { source: 'simulation' } }));
  await json(await request.post('/api/control', { data: { running: false, speed: 30, shift_plan: 480 } }));
  await json(await request.post('/api/reset', { data: { confirm: true } }));
  for (const station of defaults) {
    await json(await request.patch(`/api/stations/${station.id}`, {
      data: { cycle_seconds: station.cycle, capacity: 1, buffer_capacity: station.buffer, defect_rate: 0, speed_factor: 1, manual_stop: false },
    }));
  }
  await page.goto('/');
  await expect(page.getByTestId('nav-overview')).toBeVisible();
  await expect(page.getByTestId('station-painting')).toBeVisible();
});

test('production controls move real parts and update the diagram, counters and chart', async ({ page, request }, testInfo) => {
  const initial = await state(request);
  expect(initial.sim_time).toBe(0);
  expect(initial.metrics.good).toBe(0);
  await expect(page.getByTestId('source-label')).toContainText(/симуляц|модел/i);
  await expect(page.getByTestId('metric-good')).toContainText('0');
  for (const id of stationIds) await expect(page.getByTestId(`station-${id}`)).toBeVisible();

  const first: State = await action(page, '/api/advance', () => page.getByTestId('control-advance').click());
  expect(first.sim_time).toBe(600);
  expect(first.metrics.good).toBeGreaterThan(0);
  await expect(page.getByTestId('metric-good')).toContainText(displayedNumber(first.metrics.good));
  const chart = page.getByTestId('production-chart');
  await expect(chart.locator('svg')).toBeVisible();
  await expect(chart.locator('svg')).toHaveAttribute('aria-label', `Готовая продукция: ${displayedNumber(first.metrics.produced)} ед.; Брак на линии: ${displayedNumber(first.metrics.rejected)} ед.`);
  await expect.poll(async () => chart.locator('polyline').first().getAttribute('points')).not.toBeNull();
  const firstPoints = await chart.locator('polyline').first().getAttribute('points');
  expect(firstPoints).not.toMatch(/NaN|Infinity/);

  const second: State = await action(page, '/api/advance', () => page.getByTestId('control-advance').click());
  expect(second.sim_time).toBe(1200);
  expect(second.metrics.good).toBeGreaterThan(first.metrics.good);
  await expect(page.getByTestId('metric-good')).toContainText(displayedNumber(second.metrics.good));
  await expect(chart.locator('svg')).toHaveAttribute('aria-label', `Готовая продукция: ${displayedNumber(second.metrics.produced)} ед.; Брак на линии: ${displayedNumber(second.metrics.rejected)} ед.`);
  await expect.poll(async () => chart.locator('polyline').first().getAttribute('points')).not.toBe(firstPoints);
  const history = await json<any[]>(await request.get('/api/history?limit=120'));
  expect(history.at(-1).sim_time).toBe(1200);
  expect(history.at(-1).produced).toBe(second.metrics.produced);

  await action(page, '/api/control', () => page.getByTestId('control-running').click());
  await expect.poll(async () => (await state(request)).sim_time).toBeGreaterThan(1200);
  await expect(page.getByTestId('sim-time')).not.toHaveText('00:20:00');
  const paused: State = await action(page, '/api/control', () => page.getByTestId('control-running').click());
  expect(paused.running).toBe(false);
  await expect(page.getByTestId('control-running')).toContainText(/запус|пуск|старт/i);
  await page.screenshot({ path: testInfo.outputPath('overview-desktop.png'), fullPage: true, animations: 'disabled' });
});

test('station edits stop production, generate an incident, and resume the same line', async ({ page, request }, testInfo) => {
  await page.getByTestId('station-assembly').click();
  await page.getByTestId('operation-edit-assembly').click();
  await page.getByTestId('station-cycle').fill('96');
  await page.getByTestId('station-stop').check();
  const stopped: State = await action(page, '/api/stations/assembly', () => page.getByTestId('station-save').click(), 'PATCH');
  expect(stopped.stations.find(station => station.id === 'assembly')).toMatchObject({ cycle_seconds: 96, manual_stop: true, status: 'stopped' });

  await openTab(page, 'overview');
  // Editor may close after saving; the persisted card itself is the evidence.
  await expect(page.getByTestId('station-assembly')).toContainText(/останов/i);
  const halted: State = await action(page, '/api/advance', () => page.getByTestId('control-advance').click());
  expect(halted.metrics.good).toBe(0);
  const incidents = await json<any[]>(await request.get('/api/incidents'));
  const incident = incidents.find(item => item.station_id === 'assembly' && item.kind === 'manual_stop' && item.status === 'open');
  expect(incident, 'A manual stop must create a real persisted incident.').toBeTruthy();
  await openTab(page, 'incidents');
  await expect(page.getByTestId(`incident-${incident.id}`)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('incidents-desktop.png'), fullPage: true, animations: 'disabled' });
  await action(page, `/api/incidents/${incident.id}/acknowledge`, () => page.getByTestId(`incident-ack-${incident.id}`).click());
  expect((await json<any[]>(await request.get('/api/incidents'))).find(item => item.id === incident.id).status).toBe('acknowledged');

  await openTab(page, 'overview');
  await page.getByTestId('station-assembly').click();
  await page.getByTestId('operation-edit-assembly').click();
  await page.getByTestId('station-cycle').fill('48');
  await page.getByTestId('station-stop').uncheck();
  const resumed: State = await action(page, '/api/stations/assembly', () => page.getByTestId('station-save').click(), 'PATCH');
  expect(resumed.stations.find(station => station.id === 'assembly')!.manual_stop).toBe(false);
  await openTab(page, 'overview');
  const advanced: State = await action(page, '/api/advance', () => page.getByTestId('control-advance').click());
  expect(advanced.metrics.good).toBeGreaterThan(0);
  expect(advanced.run_id).toBe(halted.run_id);
  const resolved = (await json<any[]>(await request.get('/api/incidents'))).find(item => item.id === incident.id);
  expect(resolved.status).toBe('resolved');
});

test('scenario comparison calculates independent outcomes and preserves live state', async ({ page, request }, testInfo) => {
  await action(page, '/api/advance', () => page.getByTestId('control-advance').click());
  const before = await state(request);
  await openTab(page, 'scenarios');
  await page.getByTestId('scenario-name').fill('Проверка замедления окраски');
  await page.getByTestId('scenario-horizon').selectOption('60');
  await page.getByTestId('scenario-station').selectOption('painting');
  await page.getByTestId('scenario-add').click();
  await page.getByTestId('scenario-speed-painting').fill('0.5');
  const result = await action(page, '/api/scenarios', () => page.getByTestId('scenario-run').click());
  expect(result.baseline.good).toBeGreaterThan(0);
  expect(result.variant.good).toBeLessThan(result.baseline.good);
  expect(result.delta.good).toBe(result.variant.good - result.baseline.good);
  expect(result.timeline.length).toBeGreaterThan(10);
  expect(result.timeline.at(-1).minute).toBe(60);
  expect(result.timeline.at(-1).baseline_good).toBe(result.baseline.good);
  expect(result.timeline.at(-1).variant_good).toBe(result.variant.good);
  await expect(page.getByTestId('scenario-comparison')).toContainText(displayedNumber(result.baseline.good));
  await expect(page.getByTestId('scenario-comparison')).toContainText(displayedNumber(result.variant.good));
  await expect(page.getByTestId('scenario-comparison').locator('svg[role="img"]')).toBeVisible();
  const after = await state(request);
  expect(after.run_id).toBe(before.run_id);
  expect(after.sim_time).toBe(before.sim_time);
  expect(after.metrics).toEqual(before.metrics);
  expect(after.stations).toEqual(before.stations);
  const saved = await json<any[]>(await request.get('/api/scenarios'));
  expect(saved.some(item => item.id === result.id)).toBe(true);
  await page.reload();
  await openTab(page, 'scenarios');
  await expect(page.getByText('Проверка замедления окраски', { exact: true }).first()).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('scenario-comparison.png'), fullPage: true, animations: 'disabled' });
});

test('CSV validation is atomic, valid telemetry is shown, and simulation can be restored', async ({ page, request }, testInfo) => {
  await action(page, '/api/advance', () => page.getByTestId('control-advance').click());
  const simulation = await state(request);
  // A reusable disposable database may already contain an earlier successful
  // test import. Append later observations without deleting its durable series.
  let previousTelemetry: State | undefined;
  if (simulation.telemetry_available?.production) {
    previousTelemetry = await json<State>(await request.post('/api/source', { data: { source: 'telemetry', telemetry_kind: 'production' } }));
    await json(await request.post('/api/source', { data: { source: 'simulation' } }));
  }
  await openTab(page, 'data');
  const invalid = `${headers}\n2026-10-16T08:00:00Z,assembly,running,10,20,0,48\n`;
  await page.getByTestId('data-import-file').setInputFiles({ name: 'invalid.csv', mimeType: 'text/csv', buffer: Buffer.from(invalid) });
  const failed = page.waitForResponse(response => new URL(response.url()).pathname === '/api/import' && response.request().method() === 'POST');
  await page.getByTestId('data-import-submit').click();
  expect((await failed).status()).toBe(422);
  await expect(page.getByTestId('import-error')).toContainText(/ошиб|строк|брак|rejected/i);
  await expect(page.getByTestId('import-error')).toContainText(/строка\s*2/i);
  await page.screenshot({ path: testInfo.outputPath('data-validation-error.png'), fullPage: true, animations: 'disabled' });
  const unchanged = await state(request);
  expect(unchanged.source).toBe('simulation');
  expect(unchanged.metrics).toEqual(simulation.metrics);
  expect(unchanged.sim_time).toBe(simulation.sim_time);

  const rows: string[] = [headers];
  const start = previousTelemetry?.telemetry_updated_at
    ? Date.parse(previousTelemetry.telemetry_updated_at) + 60_000
    : Date.parse('2026-10-16T08:00:00Z');
  const offsets = stationIds.map(id => previousTelemetry?.stations.find(station => station.id === id)?.completed || 0);
  const previousRejects = stationIds.map(id => previousTelemetry?.stations.find(station => station.id === id)?.rejected || 0);
  for (const [hour, counts] of [[0, [200, 190, 180, 170, 160]], [1, [260, 245, 230, 215, 200]]] as const) {
    const timestamp = new Date(start + hour * 3_600_000).toISOString();
    stationIds.forEach((id, index) => rows.push(`${timestamp},${id},running,${offsets[index] + counts[index]},${previousRejects[index] + (index === 4 ? 3 : 1)},2,${defaults[index].cycle}`));
  }
  const expectedProduced = offsets[4] + 200;
  const expectedGood = expectedProduced - previousRejects[4] - 3;
  await page.getByTestId('data-import-file').setInputFiles({ name: 'telemetry.csv', mimeType: 'text/csv', buffer: Buffer.from(rows.join('\n') + '\n') });
  const imported = await action(page, '/api/import', () => page.getByTestId('data-import-submit').click());
  expect(imported.imported).toBe(10);
  expect(imported.source).toBe('telemetry');
  await expect(page.getByTestId('source-label')).toContainText(/телеметр|CSV/i);
  const telemetry = await state(request);
  expect(telemetry.source).toBe('telemetry');
  expect(telemetry.metrics.produced).toBe(expectedProduced);
  expect(telemetry.metrics.good).toBe(expectedGood);
  await expect(page.getByTestId('import-success')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('data-telemetry.png'), fullPage: true, animations: 'disabled' });
  await openTab(page, 'overview');
  await expect(page.getByTestId('metric-good')).toContainText(displayedNumber(expectedGood));
  await expect(page.getByTestId('control-advance')).toBeDisabled();
  await expect(page.getByTestId('control-running')).toBeDisabled();
  const denied = await request.post('/api/advance', { data: { seconds: 600 } });
  expect([400, 409, 422]).toContain(denied.status());
  expect((await state(request)).metrics).toEqual(telemetry.metrics);

  await openTab(page, 'data');
  const restored: State = await action(page, '/api/source', () => page.getByTestId('source-simulation').click());
  expect(restored.source).toBe('simulation');
  expect(restored.run_id).toBe(simulation.run_id);
  expect(restored.sim_time).toBe(simulation.sim_time);
  expect(restored.metrics).toEqual(simulation.metrics);
  await openTab(page, 'overview');
  await expect(page.getByTestId('control-advance')).toBeEnabled();
  await expect(page.getByTestId('metric-good')).toContainText(displayedNumber(simulation.metrics.good));
});

test('history and import template download usable CSV files', async ({ page, request }, testInfo) => {
  await action(page, '/api/advance', () => page.getByTestId('control-advance').click());
  const current = await state(request);
  await openTab(page, 'data');
  const exported = page.waitForEvent('download');
  await page.getByTestId('data-export').click();
  const historyDownload = await exported;
  expect(await historyDownload.failure()).toBeNull();
  expect(historyDownload.suggestedFilename()).toMatch(/\.csv$/i);
  await historyDownload.saveAs(testInfo.outputPath('history.csv'));
  const historyResponse = await request.get('/api/export');
  expect(historyResponse.ok()).toBeTruthy();
  const history = (await historyResponse.text()).replace(/^\uFEFF/, '').trim().split(/\r?\n/);
  expect(history.length).toBeGreaterThan(1);
  expect(history[0]).toContain('sim_time');
  expect(history[0]).toContain('produced');
  const exportedHeaders = history[0].split(',');
  const last = history.at(-1)!.split(',');
  expect(Number(last[exportedHeaders.indexOf('sim_time')])).toBe(current.sim_time);
  expect(Number(last[exportedHeaders.indexOf('produced')])).toBe(current.metrics.produced);

  const template = page.waitForEvent('download');
  await page.getByTestId('data-template').click();
  const templateDownload = await template;
  expect(await templateDownload.failure()).toBeNull();
  expect(templateDownload.suggestedFilename()).toMatch(/\.csv$/i);
  await templateDownload.saveAs(testInfo.outputPath('import-template.csv'));
  const templateResponse = await request.get('/api/import/template');
  expect(templateResponse.ok()).toBeTruthy();
  expect((await templateResponse.text()).replace(/^\uFEFF/, '').split(/\r?\n/)[0]).toBe(headers);
});

test('shift settings persist and a confirmed new shift preserves archived history', async ({ page, request }) => {
  await action(page, '/api/advance', () => page.getByTestId('control-advance').click());
  const previous = await state(request);
  await openTab(page, 'data');
  await page.getByRole('spinbutton', { name: 'План смены', exact: true }).fill('900');
  const configured = await action(page, '/api/control', () => page.getByRole('button', { name: 'Сохранить план', exact: true }).click());
  expect(configured.shift_plan).toBe(900);
  await page.getByTestId('reset-open').click();
  await expect(page.getByTestId('reset-confirm')).toBeDisabled();
  await page.getByRole('checkbox', { name: 'Подтверждаю начало новой смены', exact: true }).check();
  const reset = await action(page, '/api/reset', () => page.getByTestId('reset-confirm').click());
  expect(reset.run_id).not.toBe(previous.run_id);
  expect(reset.sim_time).toBe(0);
  expect(reset.metrics.good).toBe(0);
  expect(reset.metrics.wip).toBe(0);
  expect(reset.shift_plan).toBe(900);
  expect(reset.stations.map((station: Station) => station.cycle_seconds)).toEqual(previous.stations.map(station => station.cycle_seconds));
  const archived = await json<any[]>(await request.get(`/api/history?run_id=${encodeURIComponent(previous.run_id)}`));
  expect(archived.at(-1).sim_time).toBe(previous.sim_time);
  expect(archived.at(-1).produced).toBe(previous.metrics.produced);
  await page.reload();
  await openTab(page, 'data');
  await expect(page.getByRole('spinbutton', { name: 'План смены', exact: true })).toHaveValue('900');
});

test('mobile layout supports navigation, station editing and full-width content', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openTab(page, 'overview');
  await expect(page.getByTestId('station-assembly')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(392);
  await page.getByTestId('station-assembly').click();
  await page.getByTestId('operation-edit-assembly').click();
  await expect(page.getByTestId('station-save')).toBeVisible();
  await page.getByTestId('station-cycle').fill('49');
  const edited: State = await action(page, '/api/stations/assembly', () => page.getByTestId('station-save').click(), 'PATCH');
  expect(edited.stations.find(station => station.id === 'assembly')!.cycle_seconds).toBe(49);
  await openTab(page, 'overview');
  await action(page, '/api/advance', () => page.getByTestId('control-advance').click());
  await openTab(page, 'scenarios');
  await expect(page.getByTestId('scenario-name')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(392);
  await openTab(page, 'incidents');
  await openTab(page, 'data');
  await expect(page.getByTestId('data-import-submit')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(392);
  await openTab(page, 'overview');
  await page.screenshot({ path: testInfo.outputPath('overview-mobile.png'), fullPage: true, animations: 'disabled' });
});

test('robot CSV displays sensor history, incidents, validation, export and mobile layout', async ({ page, request }, testInfo) => {
  const stamp = Date.now();
  const times = Array.from({length: 5}, (_, i) => new Date(stamp + i * 1000).toISOString());
  const csv = 'timestamp,robot_id,line_section,joint_temperature_c,vibration_mm_s,hydraulic_pressure_bar,cycle_status,error_code\n' +
    `${times[0]},R-042,Welding_Shop,42.5,1.2,150.1,In_Progress,0\n${times[1]},R-042,Welding_Shop,42.8,1.5,149.8,In_Progress,0\n${times[2]},R-043,Assembly_Line,38.1,0.8,0.0,Idle,0\n${times[3]},R-042,Welding_Shop,45.2,4.8,155.4,Warning,W-203\n${times[4]},R-044,Painting_Shop,24.1,0.4,90.2,In_Progress,0\n`;
  await openTab(page, 'data');
  await page.getByTestId('data-import-file').setInputFiles({name: 'robots.csv', mimeType: 'text/csv', buffer: Buffer.from(csv)});
  await action(page, '/api/import', () => page.getByTestId('data-import-submit').click());
  await expect(page.getByTestId('import-success')).toContainText('5 измерений');
  await openTab(page, 'overview');
  await expect(page.getByRole('heading', {name: 'Телеметрия роботов', exact: true})).toBeVisible();
  await page.getByRole('searchbox', {name: 'Найти робота или участок'}).fill('R-042');
  await expect(page.getByTestId('robot-R-042')).toContainText('W-203');
  await expect(page.getByRole('img', {name: /Вибрация: 4,8/})).toBeVisible();
  await expect(page.getByTestId('control-running')).toBeDisabled();
  await page.getByRole('searchbox', {name: 'Найти робота или участок'}).fill('R-043');
  await page.getByTestId('robot-R-043').click();
  await expect(page.getByRole('heading', {name: 'Датчики R-043'})).toBeVisible();
  await expect(page.getByRole('img', {name: /Гидравлическое давление: 0/})).toBeVisible();
  await page.getByRole('searchbox', {name: 'Найти робота или участок'}).fill('R-042');
  await page.getByTestId('robot-R-042').click();
  await expect(page.getByRole('img', {name: /Температура узла: 45,2/})).toBeVisible();
  await page.screenshot({path: testInfo.outputPath('robots-desktop.png'), fullPage: true, animations: 'disabled'});
  const downloaded = page.waitForEvent('download');
  await page.getByRole('link', {name: 'Скачать измерения', exact: true}).click();
  const file = await downloaded; expect(await file.failure()).toBeNull();
  await file.saveAs(testInfo.outputPath('robots.csv'));
  expect(await (await request.get('/api/robots/export')).text()).toContain('W-203');
  await openTab(page, 'incidents');
  await page.getByRole('button', {name: /^Открытые/}).click();
  await expect(page.getByRole('heading', {name: 'R-042: W-203', exact: true})).toBeVisible();
  await openTab(page, 'data');
  await page.getByTestId('data-import-file').setInputFiles({name: 'duplicate.csv', mimeType: 'text/csv', buffer: Buffer.from(csv)});
  await page.getByTestId('data-import-submit').click();
  await expect(page.getByTestId('import-error')).toContainText('позже последнего наблюдения');
  await page.reload();
  await page.getByRole('searchbox', {name: 'Найти робота или участок'}).fill('R-042');
  await expect(page.getByTestId('robot-R-042')).toBeVisible();
  await page.setViewportSize({width: 390, height: 844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(392);
  await page.screenshot({path: testInfo.outputPath('robots-mobile.png'), fullPage: true, animations: 'disabled'});
});



test('preview, robot rules, trend analytics and JSON ingestion work end to end', async ({page, request}, testInfo) => {
  const id = `QA-${Date.now()}`;
  const origin = Date.now() - 600_000;
  const timestamps = Array.from({length: 10}, (_, i) => new Date(origin + i * 20_000).toISOString());
  const csv = 'timestamp,robot_id,line_section,joint_temperature_c,vibration_mm_s,hydraulic_pressure_bar,cycle_status,error_code\n' +
    timestamps.map((t, i) => `${t},${id},Test_Bench,${40 + i},${1 + i * .2},150,In_Progress,${i === 9 ? 'W-CHECK' : '0'}`).join('\n');
  const before = await state(request);
  await openTab(page, 'data');
  await page.getByTestId('data-import-file').setInputFiles({name: 'analytics.csv', mimeType: 'text/csv', buffer: Buffer.from(csv)});
  await action(page, '/api/import/preview', () => page.getByRole('button', {name: 'Проверить без сохранения'}).click());
  await expect(page.getByText('Проверка пройдена · Роботы')).toBeVisible();
  expect((await state(request)).run_id).toBe(before.run_id);
  await action(page, '/api/import', () => page.getByTestId('data-import-submit').click());
  await expect(page.getByRole('heading', {name: 'Журнал загрузок'})).toBeVisible();
  await expect(page.getByRole('cell', {name: 'analytics.csv', exact: true}).first()).toBeVisible();
  await openTab(page, 'overview');
  await page.getByRole('searchbox', {name: 'Найти робота или участок'}).fill(id);
  await page.getByTestId(`robot-${id}`).click();
  await expect(page.getByRole('heading', {name: `Датчики ${id}`})).toBeVisible();
  await page.getByText(`Пороги и справочник ошибок · ${id}`, {exact: true}).click();
  await page.getByRole('spinbutton', {name: 'Температура узла: Верхняя', exact: true}).fill('55');
  await page.getByRole('spinbutton', {name: 'Температура узла: Критическая верхняя', exact: true}).fill('65');
  await page.getByRole('spinbutton', {name: 'Вибрация: Верхняя', exact: true}).fill('2');
  await page.getByRole('spinbutton', {name: 'Вибрация: Критическая верхняя', exact: true}).fill('3');
  await page.getByRole('button', {name: 'Добавить код ошибки', exact: true}).click();
  await page.getByRole('textbox', {name: 'Код ошибки 1', exact: true}).fill('W-CHECK');
  await page.getByRole('textbox', {name: 'Описание ошибки 1', exact: true}).fill('Контрольный код из регламента испытания');
  await page.getByRole('textbox', {name: 'Действие при ошибке 1', exact: true}).fill('Осмотреть узел по инструкции');
  await page.getByRole('combobox', {name: 'Уровень ошибки 1', exact: true}).selectOption('critical');
  await action(page, '/api/robots/config', () => page.getByRole('button', {name: 'Сохранить настройки робота'}).click(), 'PUT');
  await expect(page.getByText(/граница 55 °C будет достигнута через 2 мин/)).toBeVisible();
  await page.getByText(`Пороги и справочник ошибок · ${id}`, {exact: true}).click();
  const stats = await json(await request.get(`/api/robots/analytics?robot_id=${id}`));
  expect(stats.sensors.joint_temperature_c.trend.per_minute).toBe(3);
  expect(stats.sensors.joint_temperature_c.mean).toBe(44.5);
  await page.screenshot({path: testInfo.outputPath('robot-analytics.png'), fullPage: true, animations: 'disabled'});
  await openTab(page, 'incidents');
  await page.getByRole('searchbox', {name: 'Поиск инцидентов'}).fill(id);
  await expect(page.getByText(/Контрольный код из регламента испытания/)).toBeVisible();
  await expect(page.getByRole('heading', {name: `${id}: Вибрация — выше границы`})).toBeVisible();
  const incoming = {timestamp: new Date(origin + 200_000).toISOString(), robot_id: id, cycle_status: 'In_Progress', joint_temperature_c: 68, vibration_mm_s: 4};
  expect((await request.post('/api/robots/measurements', {data: {measurements: [incoming]}})).ok()).toBeTruthy();
  await openTab(page, 'overview');
  await page.getByRole('searchbox', {name: 'Найти робота или участок'}).fill(id);
  await page.getByTestId(`robot-${id}`).click();
  await expect(page.getByRole('img', {name: /Температура узла: 68/})).toBeVisible();
  await page.getByText(`Пороги и справочник ошибок · ${id}`, {exact: true}).click();
  await expect(page.getByRole('spinbutton', {name: 'Температура узла: Верхняя', exact: true})).toHaveValue('55');
  await page.setViewportSize({width: 390, height: 844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(392);
  await page.screenshot({path: testInfo.outputPath('robot-settings-mobile.png'), fullPage: true, animations: 'disabled'});
  await page.reload();
  await page.getByRole('searchbox', {name: 'Найти робота или участок'}).fill(id);
  await page.getByTestId(`robot-${id}`).click();
  await expect(page.getByRole('img', {name: /Температура узла: 68/})).toBeVisible();
});
