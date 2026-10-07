import {test, expect, type Page} from '@playwright/test';
import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';

type Order = {id:string;part:string;quantity:number;status:string;eta_t:number;received_at:string|null};
type Part = {id:string;stock:number;deficit:number;in_transit:number};
type Notice = {id:number;channel:string;text:string;read:boolean;external_delivery:boolean};
type Snapshot = {
  revision:number;paused:boolean;t:number;orders:Order[];parts:Part[];notifications:Notice[];
  components:{id:string;wear_pct:number;forecast:{from:string|null;to:string|null}}[];
};

const csrf = () => readFileSync('/tmp/allur-csrf','utf8');
async function snapshot(page:Page):Promise<Snapshot>{
  const response = await page.request.get('/api/emulation');
  expect(response.ok(),await response.text()).toBeTruthy();
  return response.json();
}
async function command(page:Page,action:string,extra:Record<string,unknown>={}){
  const state = await snapshot(page);
  const response = await page.request.post('/api/emulation/commands',{
    headers:{'X-CSRF-Token':csrf()},
    data:{request_id:randomUUID(),expected_revision:state.revision,action,...extra},
  });
  expect(response.ok(),await response.text()).toBeTruthy();
  return response.json();
}
async function selectStand(page:Page){
  await expect(page.getByTestId('nav-scada')).toBeAttached();
  const menuButton=page.getByRole('button',{name:'Открыть меню',exact:true});
  const mobile=await menuButton.isVisible();
  if(mobile){
    await expect(menuButton).toHaveAttribute('aria-controls','main-sidebar');
    if(await menuButton.getAttribute('aria-expanded')!=='true')await menuButton.click();
    await expect(menuButton).toHaveAttribute('aria-expanded','true');
  }
  await page.getByTestId('nav-scada').click();
  if(mobile)await expect(menuButton).toHaveAttribute('aria-expanded','false');
  await page.getByRole('button',{name:'Эмуляция',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Эмуляция роботов и конвейера'})).toBeVisible();
}
async function openStand(page:Page){
  await page.goto('/');
  await selectStand(page);
}

test('full SCADA: schematic, forecast, idempotent purchase, simulated delivery, maintenance and confirmed assistant',async({page},info)=>{
  // Three working days at x3600 take approximately 48 real seconds. The test
  // advances the real server model; it does not inject a delivery or mock time.
  test.setTimeout(360_000);
  const errors:string[]=[];
  page.on('pageerror',error=>errors.push(error.message));
  await command(page,'reset');
  try {
    await openStand(page);
    const robot = page.getByRole('group',{name:'Кинематическая схема робота R1',exact:true});
    const axis = robot.getByRole('button',{name:'Ось J3',exact:true});
    await axis.focus();
    await page.keyboard.press('Enter');
    await expect(axis).toHaveAttribute('aria-pressed','true');
    await expect(page.getByRole('heading',{name:'Серводвигатель J3',exact:true})).toBeVisible();
    await robot.getByRole('button',{name:'Оснастка робота',exact:true}).click();
    await expect(page.getByRole('heading',{name:'Присоски 150 мм, 4 шт.',exact:true})).toBeVisible();

    const maintenance = page.getByTestId('emulation-maintenance');
    // The 93% worn R2 reducer is already in warning even if its calculated
    // lifetime is beyond 90 days. It must not vanish from the default plan.
    await expect(maintenance.getByTestId('emulation-forecast-R2-J3-red')).toBeVisible();
    await maintenance.getByRole('button',{name:'Все узлы',exact:true}).click();
    await expect(maintenance.locator('tbody tr')).toHaveCount(107);
    await maintenance.getByRole('button',{name:'Конвейер',exact:true}).click();
    await expect(maintenance.locator('tbody tr')).toHaveCount(13);
    const before = await snapshot(page);
    const oilForecast = before.components.find(component=>component.id==='CV-cvoil')!.forecast;
    expect(Date.parse(oilForecast.to!)).toBeGreaterThan(Date.parse(oilForecast.from!));

    const cups = page.getByTestId('emulation-part-VAC-CUP');
    const cupDeficit = before.parts.find(part=>part.id==='VAC-CUP')!.deficit;
    expect(cupDeficit).toBeGreaterThan(0);
    const orderResponsePromise = page.waitForResponse(response=>{
      if(!response.url().endsWith('/api/emulation/commands')||response.request().method()!=='POST')return false;
      const body=response.request().postDataJSON();
      return body.action==='order'&&body.target==='VAC-CUP';
    });
    await cups.getByRole('button',{name:'Учебный заказ: Присоска 150 мм, к-т 4 шт.',exact:true}).click();
    const orderResponse = await orderResponsePromise;
    expect(orderResponse.ok(),await orderResponse.text()).toBeTruthy();
    const receipt = await orderResponse.json();
    const requestBody = orderResponse.request().postDataJSON();
    const repeated = await page.request.post('/api/emulation/commands',{
      headers:{'X-CSRF-Token':csrf()},data:requestBody,
    });
    expect(repeated.ok(),await repeated.text()).toBeTruthy();
    expect(await repeated.json()).toEqual(receipt);
    const ordered = await snapshot(page);
    const cupOrders = ordered.orders.filter(order=>order.part==='VAC-CUP');
    expect(cupOrders).toHaveLength(1);
    expect(cupOrders[0].quantity).toBe(cupDeficit);
    expect(ordered.parts.find(part=>part.id==='VAC-CUP')!.deficit).toBe(0);
    await expect(cups.getByRole('button',{name:/^Учебный заказ:/})).toBeDisabled();

    // A deliberate single-spare order is supported even when its stock covers
    // demand. Its unmodified catalogue lead time makes this a bounded E2E run.
    await command(page,'order',{target:'GBX-OIL'});
    const shipping = await snapshot(page);
    const oilOrder = shipping.orders.find(order=>order.part==='GBX-OIL')!;
    const oilStock = shipping.parts.find(part=>part.id==='GBX-OIL')!.stock;
    expect(oilOrder.status).toBe('in_transit');
    expect(oilOrder.eta_t-shipping.t).toBeCloseTo(48,6);
    await command(page,'speed',{value:3600});
    await command(page,'play');
    await expect.poll(async()=>{
      const state=await snapshot(page);
      return state.orders.find(order=>order.id===oilOrder.id)?.status;
    },{timeout:240_000,intervals:[1000,2000,3000],message:'Order arrives through real simulation time'}).toBe('received');
    await command(page,'pause');
    await expect(page.getByTestId('emulation-status')).toContainText('Время на паузе');
    const delivered = await snapshot(page);
    expect(delivered.parts.find(part=>part.id==='GBX-OIL')!.stock).toBe(oilStock+oilOrder.quantity);
    expect(delivered.orders.find(order=>order.id===oilOrder.id)!.received_at).not.toBeNull();
    await page.getByTestId('emulation-stock').getByRole('button',{name:'Показать полученные поставки',exact:true}).click();
    await expect(page.getByTestId(`emulation-order-${oilOrder.id}`)).toContainText('Получено на учебный склад');

    await page.getByRole('button',{name:/^CV · Конвейер/}).click();
    const conveyor = page.getByRole('group',{name:'Схема конвейера: привод, цепь, натяжитель и датчики',exact:true});
    await conveyor.getByRole('button',{name:'Привод конвейера',exact:true}).click();
    await expect(conveyor.getByRole('button',{name:'Привод конвейера',exact:true})).toHaveAttribute('aria-pressed','true');
    await maintenance.getByTestId('emulation-forecast-CV-cvoil').getByRole('button',{name:/^Учебная замена: CV-cvoil$/}).click();
    await expect.poll(async()=>(await snapshot(page)).components.find(component=>component.id==='CV-cvoil')!.wear_pct).toBe(0);
    await expect(maintenance.getByTestId('emulation-forecast-CV-cvoil').getByRole('cell').nth(1)).toHaveText('0%');
    expect((await snapshot(page)).parts.find(part=>part.id==='GBX-OIL')!.stock).toBe(oilStock+oilOrder.quantity-1);

    const notifications = page.getByTestId('emulation-notifications');
    await notifications.getByRole('button',{name:'Прочитать все уведомления стенда',exact:true}).click();
    await expect.poll(async()=>(await snapshot(page)).notifications.filter(notice=>!notice.read).length).toBe(0);
    await expect(notifications.getByRole('button',{name:'Прочитать все уведомления стенда',exact:true})).toBeDisabled();
    const beforeQuestion = await snapshot(page);
    await page.getByRole('textbox',{name:'Вопрос по данным стенда',exact:true}).fill('Отправь сводку');
    await page.getByRole('button',{name:'Спросить ассистента',exact:true}).click();
    const confirmation = page.getByTestId('emulation-chat').getByRole('button',{name:'Подтвердить: Сохранить сводку в журнале стенда',exact:true});
    await expect(confirmation).toBeVisible();
    const suggested = await snapshot(page);
    expect(suggested.revision).toBe(beforeQuestion.revision);
    expect(suggested.notifications).toEqual(beforeQuestion.notifications);
    await confirmation.click();
    await expect(page.getByTestId('emulation-chat').getByRole('button',{name:'Команда выполнена',exact:true})).toBeDisabled();
    await expect.poll(async()=>(await snapshot(page)).notifications.filter(notice=>notice.channel==='summary').length).toBe(1);
    const withSummary = await snapshot(page);
    const summary = withSummary.notifications.find(notice=>notice.channel==='summary')!;
    expect(summary.external_delivery).toBe(false);
    await notifications.getByRole('button',{name:`Прочитано: уведомление ${summary.id}`,exact:true}).click();
    await expect.poll(async()=>(await snapshot(page)).notifications.find(notice=>notice.id===summary.id)!.read).toBe(true);

    const saved = await snapshot(page);
    await page.reload();
    await selectStand(page);
    await expect(page.getByTestId('emulation-status')).toContainText('Время на паузе');
    const restored = await snapshot(page);
    expect({t:restored.t,orders:restored.orders,parts:restored.parts,notifications:restored.notifications})
      .toEqual({t:saved.t,orders:saved.orders,parts:saved.parts,notifications:saved.notifications});
    await expect(page.getByTestId(`emulation-notification-${summary.id}`)).toContainText('Прочитано оператором');
    await page.screenshot({path:info.outputPath('scada-full-desktop.png'),fullPage:true,animations:'disabled'});
    expect(errors).toEqual([]);
  } finally {
    // Leave the shared disposable test installation paused even if an assertion
    // fails while time is running. Never touch the production installation.
    await command(page,'pause');
  }
});

test('full SCADA: mobile, reduced motion, keyboard navigation and persistent animation preference',async({page},info)=>{
  const errors:string[]=[];
  page.on('pageerror',error=>errors.push(error.message));
  await command(page,'reset');
  await page.setViewportSize({width:390,height:844});
  await page.emulateMedia({reducedMotion:'reduce'});
  await openStand(page);
  const animation = page.getByRole('checkbox',{name:'Анимация стенда',exact:true});
  await expect(animation).toBeVisible();
  await expect(animation.locator('..')).toContainText('отключена настройкой уменьшения движения системы');
  const robot = page.getByRole('group',{name:'Кинематическая схема робота R1',exact:true});
  const jointTransform = robot.getByRole('button',{name:'Ось J3',exact:true}).locator('g').first();
  const stillPose = await jointTransform.getAttribute('transform');
  await command(page,'advance',{value:15});
  await expect(page.getByTestId('emulation-status')).toContainText('07:00:15');
  expect(await jointTransform.getAttribute('transform')).toBe(stillPose);
  const cabinet = robot.getByRole('button',{name:'Шкаф управления',exact:true});
  await cabinet.focus();
  await page.keyboard.press('Space');
  await expect(cabinet).toHaveAttribute('aria-pressed','true');
  await expect(page.getByRole('heading',{name:'Батарея энкодеров',exact:true})).toBeVisible();
  await expect(page.getByTestId('emulation-maintenance')).toBeVisible();
  await expect(page.getByTestId('emulation-stock')).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1)).toBe(true);
  await page.screenshot({path:info.outputPath('scada-full-mobile.png'),fullPage:true,animations:'disabled'});
  // Explicitly changing the preference writes it; merely emulating the OS
  // preference does not prove that the user's own choice survives a reload.
  await animation.check();
  await animation.uncheck();
  await page.emulateMedia({reducedMotion:'no-preference'});
  await page.reload();
  await selectStand(page);
  await expect(page.getByRole('checkbox',{name:'Анимация стенда',exact:true})).not.toBeChecked();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1)).toBe(true);
  expect(errors).toEqual([]);
});
