import {test, expect, type APIResponse} from '@playwright/test';

type Source = 'plant' | 'simulation' | 'emulation';
type Facts = {
  data_source: Source;
  source_label: string;
  captured_at: string;
  observed_at: string | null;
  has_data: boolean;
  notice: string;
  metrics: {key: string; label: string; value: number | string | null; unit?: string; detail?: string}[];
  readings: {id: string; asset: string; label: string; value: number | string | null; unit?: string; observed_at?: string; status?: string}[];
  findings: {id: string; title: string; severity: string; evidence: string; recommendation: string}[];
  missing_data: string[];
  next_steps: string[];
};

async function json<T>(response: APIResponse): Promise<T> {
  expect(response.ok(), `${response.status()} ${response.url()}: ${await response.text()}`).toBeTruthy();
  return response.json();
}

test('AI shows measured context before inference and renders evidence-backed actions for the selected source', async ({page, request}, info) => {
  test.setTimeout(90_000);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const originalCommands = (await json<{commands: unknown[]}>(await request.get('/api/automation'))).commands;
  const factsFor = (source: Source, robot?: string) => request.get(
    `/api/ai/context?data_source=${source}${robot ? `&robot_id=${encodeURIComponent(robot)}` : ''}`,
  ).then(json<Facts>);
  const plant = await factsFor('plant');
  const simulation = await factsFor('simulation');
  const emulation = await factsFor('emulation');
  expect(plant.data_source).toBe('plant');
  expect(simulation.data_source).toBe('simulation');
  expect(emulation.data_source).toBe('emulation');
  expect(simulation.source_label).not.toBe(plant.source_label);
  expect(emulation.source_label).not.toBe(plant.source_label);
  expect(emulation.has_data).toBe(true);
  expect(emulation.metrics.length).toBeGreaterThan(0);
  expect(emulation.readings.length).toBeGreaterThan(0);

  // The preview uses the real API. Only model availability/output is stubbed;
  // real inference is exercised separately by ops/ai_report_check.py.
  await page.route('**/api/ai/status', route => route.fulfill({json: {
    ready: true, model: 'qwen3:4b', local_only: true, message: 'Локальная модель готова',
  }}));
  const modelRequests: {data_source: Source; allow_command: boolean; robot_id: string | null}[] = [];
  const analysis = 'Учебные показания доступны. Сначала проверьте узел по показаниям датчиков и указанному ниже плану.';
  const recommendationTitle = 'Проверить показания выбранного узла';
  const steps = ['Сопоставить показание датчика с допустимым диапазоном узла.', 'Проверить изменение показания после осмотра и сохранить результат.'];
  await page.route('**/api/ai/analyze', async route => {
    const body = route.request().postDataJSON();
    modelRequests.push(body);
    const facts = await factsFor(body.data_source, body.robot_id || undefined);
    const evidence = facts.findings[0] || facts.readings[0];
    await route.fulfill({json: {
      model: 'qwen3:4b', local_only: true, data_source: body.data_source,
      answer_kind: 'model', analysis, action: 'none', speed_percent: null,
      reason: facts.findings[0]?.evidence || `Текущие показания ${facts.readings[0]?.asset || facts.source_label}`,
      proposal: null, maintenance_facts: null, emulation_facts: null, facts,
      recommendations: [{title: recommendationTitle, reason: facts.findings[0]?.evidence || 'Доступны показания выбранного источника.',
        steps, priority: 'warning', evidence_ids: evidence ? [evidence.id] : [], origin: 'model'}],
    }});
  });

  await page.goto('/');
  await page.getByTestId('nav-ai').click();
  const context = page.getByTestId('ai-context');
  const selectSource = (source: Source) => page.getByRole('combobox', {name: 'Источник данных ИИ', exact: true}).selectOption(source);
  await selectSource('plant');
  await expect(context).toContainText(plant.source_label);
  if (!plant.has_data) {
    await expect(context).toContainText(plant.notice);
    expect(plant.next_steps.length).toBeGreaterThan(0);
    await expect(context).toContainText(plant.next_steps[0]);
  }

  await selectSource('emulation');
  await expect(context).toContainText(emulation.source_label);
  await expect(context).toContainText(emulation.metrics[0].label);
  await expect(context).toContainText(emulation.readings[0].asset);
  await expect(context).toContainText(emulation.readings[0].label);
  const firstReading = emulation.readings[0];
  const formattedValue = typeof firstReading.value === 'number'
    ? firstReading.value.toLocaleString('ru-RU', {maximumFractionDigits: 2}) : firstReading.value ?? '—';
  await expect(page.getByTestId(`ai-reading-${firstReading.id}`)).toContainText(String(formattedValue));
  expect(modelRequests).toEqual([]); // Data must appear before a model request.

  await page.getByTestId('ai-analyze').click();
  const answer = page.getByTestId('ai-answer');
  const recommendations = page.getByTestId('ai-recommendations');
  await expect(answer).toContainText(analysis);
  await expect(recommendations).toContainText(recommendationTitle);
  for (const step of steps) await expect(recommendations).toContainText(step);
  expect(modelRequests).toHaveLength(1);
  expect(modelRequests[0]).toMatchObject({data_source: 'emulation', allow_command: false});
  await page.screenshot({path: info.outputPath('ai-report-desktop.png'), fullPage: true, animations: 'disabled'});

  await page.setViewportSize({width: 390, height: 844});
  await expect(recommendations).toContainText(steps[1]);
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.screenshot({path: info.outputPath('ai-report-mobile.png'), fullPage: true, animations: 'disabled'});

  // Switching source invalidates the old answer; virtual metrics stay labelled.
  await selectSource('simulation');
  await expect(context).toContainText(simulation.source_label);
  if (simulation.metrics.length) await expect(context).toContainText(simulation.metrics[0].label);
  await expect(answer).toHaveCount(0);
  await expect(page.getByText(analysis, {exact: true})).toHaveCount(0);
  await selectSource('plant');
  await expect(context).toContainText(plant.source_label);
  expect(modelRequests).toHaveLength(1);
  expect((await json<{commands: unknown[]}>(await request.get('/api/automation'))).commands).toEqual(originalCommands);
  expect(errors).toEqual([]);
});
