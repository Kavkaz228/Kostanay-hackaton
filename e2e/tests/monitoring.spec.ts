import {test, expect, type APIResponse} from '@playwright/test';
import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';

type Alert = {
  id: number | string;
  robot_id: string;
  severity: 'warning' | 'critical';
  status: 'active' | 'resolved';
  acknowledged: boolean;
  recommendation: string;
  observed_at: string;
  resolved_at: string | null;
};
type Monitoring = {enabled: boolean; unread_count: number; alerts: Alert[]};

async function json<T>(response: APIResponse): Promise<T> {
  expect(response.ok(), `${response.status()} ${response.url()}: ${await response.text()}`).toBeTruthy();
  return response.json();
}

test('background robot monitoring advises staff, reopens acknowledgement on escalation and preserves recovery', async ({page, request}, testInfo) => {
  test.setTimeout(150_000);
  const robotId = `QA-MONITOR-${randomUUID().slice(0, 8)}`;
  const headers = {'X-CSRF-Token': readFileSync('/tmp/allur-csrf', 'utf8')};
  const monitoring = () => request.get('/api/monitoring').then(json<Monitoring>);
  let previousTimestamp = 0;

  async function ingest(temperature: number) {
    // Use the real clock: future samples would defeat stale-data detection.
    await expect.poll(() => Date.now(), {timeout: 2_000, intervals: [10]}).toBeGreaterThan(previousTimestamp);
    previousTimestamp = Date.now();
    await json(await request.post('/api/robots/measurements', {
      headers,
      data: {measurements: [{
        robot_id: robotId,
        timestamp: new Date(previousTimestamp).toISOString(),
        line_section: 'QA Проверка мониторинга',
        cycle_status: 'In_Progress',
        joint_temperature_c: temperature,
      }]},
    }));
  }

  expect((await monitoring()).enabled).toBe(true);
  await ingest(65);
  await json(await request.put(`/api/robots/config?robot_id=${encodeURIComponent(robotId)}`, {
    headers,
    data: {
      expected_interval_seconds: 3600,
      limits: {joint_temperature_c: {high_warning: 60, high_critical: 80}},
      error_codes: {},
    },
  }));

  // No explicit scan endpoint or browser action: the server must notice the sample itself.
  await expect.poll(async () => (await monitoring()).alerts.find(a => a.robot_id === robotId), {
    timeout: 30_000, intervals: [500, 1000, 2000],
  }).toMatchObject({severity: 'warning', status: 'active', acknowledged: false});
  const warning = (await monitoring()).alerts.find(a => a.robot_id === robotId)!;
  expect(warning.recommendation.trim()).not.toBe('');
  expect(Date.parse(warning.observed_at)).toBe(previousTimestamp);
  expect((await monitoring()).unread_count).toBeGreaterThanOrEqual(1);

  await page.goto('/');
  await expect(page.getByTestId('monitoring-badge')).toHaveClass(/has-unread/);
  await expect(page.getByTestId('monitoring-notice')).toBeVisible();
  await page.getByTestId('nav-monitoring').click();
  const card = page.getByTestId(`monitoring-alert-${warning.id}`);
  await expect(card).toBeVisible();
  await expect(card).toContainText(robotId);
  await expect(card).toContainText(warning.recommendation);
  await card.getByRole('button', {name: 'Принять в работу', exact: true}).click();
  await expect.poll(async () => (await monitoring()).alerts.find(a => a.id === warning.id)?.acknowledged).toBe(true);
  await expect(card.getByText('Принято в работу', {exact: true})).toBeVisible();

  await ingest(85);
  await expect.poll(async () => (await monitoring()).alerts.find(a => a.id === warning.id), {
    timeout: 30_000, intervals: [500, 1000, 2000],
  }).toMatchObject({robot_id: robotId, severity: 'critical', status: 'active', acknowledged: false});
  expect((await monitoring()).alerts.filter(a => a.robot_id === robotId)).toHaveLength(1);
  expect((await monitoring()).unread_count).toBeGreaterThanOrEqual(1);
  await expect(card.getByRole('button', {name: 'Принять в работу', exact: true})).toBeEnabled({timeout: 15_000});
  await expect(page.getByTestId('monitoring-notice')).toHaveClass(/critical/);
  await page.screenshot({path: testInfo.outputPath('monitoring-desktop.png'), fullPage: true, animations: 'disabled'});

  await ingest(40);
  await expect.poll(async () => (await monitoring()).alerts.find(a => a.id === warning.id)?.status, {
    timeout: 30_000, intervals: [500, 1000, 2000],
  }).toBe('resolved');
  const resolved = (await monitoring()).alerts.find(a => a.id === warning.id)!;
  expect(resolved.resolved_at).not.toBeNull();

  await page.reload();
  await page.getByTestId('nav-monitoring').click();
  await page.getByRole('button', {name: 'Вся история', exact: true}).click();
  await expect(card).toBeVisible();
  await expect(card).toContainText(robotId);
  await expect(card).toContainText('Восстановлено по данным');
  const persisted = (await monitoring()).alerts.filter(a => a.robot_id === robotId);
  expect(persisted).toHaveLength(1);
  expect(persisted[0]).toMatchObject({id: warning.id, status: 'resolved', resolved_at: resolved.resolved_at});

  // Recommendations and acknowledgements must not create controller commands.
  const automation = await json<{commands: {robot_id: string}[]}>(await request.get('/api/automation'));
  expect(automation.commands.filter(command => command.robot_id === robotId)).toEqual([]);

  await page.setViewportSize({width: 390, height: 844});
  await expect(card).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.screenshot({path: testInfo.outputPath('monitoring-mobile.png'), fullPage: true, animations: 'disabled'});
});
