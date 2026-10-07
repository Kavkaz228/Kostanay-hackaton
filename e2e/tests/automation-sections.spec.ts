import {test,expect,type APIResponse,type Page,type Locator} from '@playwright/test';
import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';

type Operation='welding'|'painting'|'assembly';
type Component={id:string;asset_id:string;name:string;node:string;kind:string;part_id:string;revision:number;last:unknown;thresholds:unknown};
type Scada={assets:{id:string;operation:Operation;revision:number}[];components:Component[];tasks:{id:string;component_id:string}[];parts:{id:string;stock:number}[]};
const operations:Operation[]=['welding','painting','assembly'];
const titles={welding:'Сварка',painting:'Окраска',assembly:'Сборка'};
const headers=()=>({'X-CSRF-Token':readFileSync('/tmp/allur-csrf','utf8')});
async function json<T=unknown>(response:APIResponse):Promise<T>{expect(response.ok(),`${response.status()} ${response.url()}: ${await response.text()}`).toBeTruthy();return response.json();}
async function nav(page:Page,tab:'automation'|'overview'){
  await expect(page.getByTestId(`nav-${tab}`)).toBeAttached();
  const menu=page.getByRole('button',{name:'Открыть меню',exact:true});
  if(await menu.isVisible()&&await menu.getAttribute('aria-expanded')!=='true')await menu.click();
  await page.getByTestId(`nav-${tab}`).click();
}
async function openOperation(page:Page,operation:Operation){
  await nav(page,'automation');
  await expect(page.locator('.operation-card')).toHaveCount(3);
  await page.getByTestId(`automation-operation-${operation}`).click();
  await expect(page.getByTestId(`operation-characteristics-${operation}`)).toBeVisible();
  const scope=page.getByTestId(`scada-scope-${operation}`);
  await expect(scope.getByRole('heading',{name:`Оборудование и ТО · ${titles[operation]}`,exact:true})).toBeVisible();
  return scope;
}

test('automation sections isolate real equipment, maintenance and alarms; assignment moves the existing asset',async({page},info)=>{
  test.setTimeout(180_000);
  const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));
  const suffix=randomUUID().slice(0,8),part=`SECTIONS-PART-${suffix}`;
  const fixture=operations.map((operation,i)=>({operation,asset:`SECTIONS-${operation}-${suffix}`,component:`SECTIONS-NODE-${operation}-${suffix}`,name:`Тестовый узел ${titles[operation]} ${suffix}`,task:`SECTIONS-TASK-${operation}-${suffix}`,temperature:[110,90,40][i]}));
  await json(await page.request.post('/api/source',{headers:headers(),data:{source:'simulation'}}));
  await json(await page.request.post('/api/scada/parts',{headers:headers(),data:{id:part,name:`Общая деталь ${suffix}`,minimum:1}}));
  await json(await page.request.post('/api/scada/stock',{headers:headers(),data:{record_id:randomUUID(),part_id:part,quantity:9,reason:'Тестовая инвентаризация общего склада'}}));
  for(const f of fixture){
    await json(await page.request.post('/api/scada/assets',{headers:headers(),data:{id:f.asset,name:`Тестовое оборудование ${titles[f.operation]} ${suffix}`,kind:'equipment',operation:f.operation}}));
    await json(await page.request.post('/api/scada/components',{headers:headers(),data:{id:f.component,asset_id:f.asset,name:f.name,node:'drive',kind:'motor',part_id:part,life_model:'none',expected_interval_seconds:86400,thresholds:[{metric:'temperature_c',label:'Температура тестового узла',unit:'°C',direction:'high',warning:80,alarm:100,hysteresis:5}]}}));
    await json(await page.request.post('/api/scada/readings',{headers:headers(),data:{readings:[{record_id:randomUUID(),component_id:f.component,timestamp:new Date(Date.now()-1000).toISOString(),metrics:{temperature_c:f.temperature}}]}}));
    await json(await page.request.post('/api/scada/tasks',{headers:headers(),data:{id:f.task,component_id:f.component,due_date:new Date(Date.now()+5*3600000).toISOString().slice(0,10),note:`Проверить тестовый узел ${f.component}`}}));
  }
  await page.goto('/');
  for(const f of fixture){
    const scope=await openOperation(page,f.operation);
    await scope.getByRole('button').filter({hasText:f.asset}).click();
    await expect(scope.getByRole('button',{name:f.name,exact:true})).toBeVisible();
    for(const other of fixture.filter(row=>row!==f))await expect(scope.getByText(other.component,{exact:true})).toHaveCount(0);
    const equipmentRow=scope.getByRole('row').filter({hasText:f.component});
    await expect(equipmentRow.getByRole('cell').nth(2)).toHaveText(f.temperature>=100?'Авария':f.temperature>=80?'Внимание':'Норма');

    await scope.getByRole('button',{name:'ТО и ресурс',exact:true}).click();
    await expect(scope.locator('article.scada-entry').getByText(`Проверить тестовый узел ${f.component}`,{exact:false})).toBeVisible();
    for(const other of fixture.filter(row=>row!==f))await expect(scope.getByText(`Проверить тестовый узел ${other.component}`,{exact:false})).toHaveCount(0);
    await scope.getByRole('button',{name:'События узлов',exact:true}).click();
    const alarm=scope.getByRole('row').filter({hasText:f.component});
    if(f.temperature>=80){await expect(alarm).toHaveCount(1);await expect(alarm.getByRole('cell').nth(3)).toHaveText(String(f.temperature));}
    else await expect(alarm).toHaveCount(0);
    for(const other of fixture.filter(row=>row!==f))await expect(scope.getByRole('row').filter({hasText:other.component})).toHaveCount(0);

    await scope.getByRole('button',{name:'Запчасти участка',exact:true}).click();
    await expect(scope.getByText(/Склад общий для всей установки/)).toBeVisible();
    const stock=scope.getByRole('row').filter({hasText:part});
    await expect(stock.getByRole('cell').nth(1)).toHaveText('9 шт.');
    // Planned work does not itself imply replacement: only the alarmed node
    // needs a part in this fixture, which has no resource-life model.
    await expect(stock.getByRole('cell').nth(2)).toHaveText(f.operation==='welding'?'1':'0');
    await expect(stock.getByRole('cell').nth(3)).toHaveText('1');
    await expect(scope.getByRole('button',{name:'Сохранить движение',exact:true})).toHaveCount(0);
  }

  const before=await json<Scada>(await page.request.get('/api/scada'));
  const weld=fixture[0],scope=await openOperation(page,'welding');
  await scope.getByRole('button').filter({hasText:weld.asset}).click();
  await scope.locator('summary').filter({hasText:/^Привязать оборудование к автоматизации$/}).click();
  const assignment=scope.getByRole('form',{name:`Участок оборудования ${weld.asset}`,exact:true});
  await assignment.getByLabel('Привязка к автоматизации',{exact:true}).selectOption('assembly');
  const save=page.waitForResponse(response=>response.request().method()==='PUT'&&response.url().endsWith(`/api/scada/assets/${weld.asset}/operation`));
  await assignment.getByRole('button',{name:'Сохранить привязку участка',exact:true}).click();
  await json(await save);
  await expect.poll(async()=>{
    const next=await json<Scada>(await page.request.get('/api/scada?operation=welding'));
    return next.assets.some(asset=>asset.id===weld.asset);
  }).toBe(false);
  await expect(scope.getByRole('button').filter({hasText:weld.asset})).toHaveCount(0);

  await page.reload();
  const assembly=await openOperation(page,'assembly');
  await assembly.getByRole('button').filter({hasText:weld.asset}).click();
  await expect(assembly.getByRole('button',{name:weld.name,exact:true})).toBeVisible();
  await assembly.getByRole('button',{name:'ТО и ресурс',exact:true}).click();
  await expect(assembly.locator('article.scada-entry').getByText(`Проверить тестовый узел ${weld.component}`,{exact:false})).toBeVisible();
  await expect(assembly.locator('article.scada-entry').getByText(`Проверить тестовый узел ${fixture[2].component}`,{exact:false})).toBeVisible();
  await assembly.getByRole('button',{name:'События узлов',exact:true}).click();
  await expect(assembly.getByRole('row').filter({hasText:weld.component})).toHaveCount(1);
  const after=await json<Scada>(await page.request.get('/api/scada'));
  expect(after.assets.filter(asset=>asset.id===weld.asset)).toHaveLength(1);
  expect(after.assets.find(asset=>asset.id===weld.asset)!.operation).toBe('assembly');
  const fingerprint=(c:Component)=>({id:c.id,asset_id:c.asset_id,name:c.name,node:c.node,kind:c.kind,part_id:c.part_id,revision:c.revision,last:c.last,thresholds:c.thresholds});
  for(const f of fixture){
    const stored=after.components.filter(component=>component.id===f.component);
    expect(stored).toHaveLength(1);
    expect(fingerprint(stored[0])).toEqual(fingerprint(before.components.find(component=>component.id===f.component)!));
    expect(after.tasks.filter(task=>task.id===f.task)).toHaveLength(1);
  }
  expect(after.parts.find(item=>item.id===part)!.stock).toBe(9);
  await assembly.screenshot({path:info.outputPath('scoped-assembly-after-assignment.png'),animations:'disabled'});
  expect(errors).toEqual([]);
});

async function setTheme(page:Page,theme:'dark'|'light'){
  await expect(page.getByTestId('theme-toggle')).toBeVisible();
  if(await page.locator('html').getAttribute('data-theme')!==theme)await page.getByTestId('theme-toggle').click();
  await expect(page.locator('html')).toHaveAttribute('data-theme',theme);
  await expect(page.getByTestId('theme-toggle')).toHaveAttribute('aria-pressed',theme==='light'?'true':'false');
}
async function readableText(locator:Locator){
  const ratio=await locator.evaluate(element=>{
    const rgba=(value:string)=>{const v=value.match(/[\d.]+/g)?.map(Number)??[0,0,0];return [v[0],v[1],v[2],v[3]??1];};
    const blend=(front:number[],back:number[])=>front.slice(0,3).map((v,i)=>v*front[3]+back[i]*(1-front[3]));
    const layers:number[][]=[];let node:Element|null=element;
    while(node){layers.push(rgba(getComputedStyle(node).backgroundColor));node=node.parentElement;}
    let background=[255,255,255];for(const layer of layers.reverse())background=blend(layer,background);
    const foreground=blend(rgba(getComputedStyle(element).color),background);
    const luminance=(rgb:number[])=>rgb.map(v=>{const s=v/255;return s<=.04045?s/12.92:((s+.055)/1.055)**2.4;}).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
    const a=luminance(foreground),b=luminance(background);return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
  });
  expect(ratio,'Visible primary text must have at least 4.5:1 contrast').toBeGreaterThanOrEqual(4.5);
}
async function checkLegend(page:Page){
  const colors=await page.evaluate(()=>['legend-running','legend-stop','legend-wait'].map(name=>{
    const element=document.querySelector(`.${name}`)!;return getComputedStyle(element).backgroundColor.match(/[\d.]+/g)!.slice(0,3).map(Number);
  }));
  const [green,red,yellow]=colors;
  expect(green[1]).toBeGreaterThan(green[0]+15);expect(green[1]).toBeGreaterThan(green[2]+5);
  expect(red[0]).toBeGreaterThan(red[1]+20);expect(red[0]).toBeGreaterThan(red[2]+10);
  // Light mode deliberately uses dark amber for accessible text contrast.
  expect(yellow[0]).toBeGreaterThan(yellow[1]);expect(yellow[1]).toBeGreaterThan(yellow[2]+50);expect(yellow[2]).toBeLessThan(Math.min(yellow[0],yellow[1])*.8);
}

test('light and dark themes persist and synchronize; overview opens operation details on desktop and mobile',async({page,context},info)=>{
  test.setTimeout(180_000);
  const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));
  await json(await page.request.post('/api/source',{headers:headers(),data:{source:'simulation'}}));
  await page.goto('/');
  const backgrounds:string[]=[];
  for(const theme of ['dark','light'] as const){
    await setTheme(page,theme);await page.reload();
    await expect(page.locator('html')).toHaveAttribute('data-theme',theme);
    await nav(page,'overview');
    await expect(page.locator('.production-legend')).toBeVisible();
    await checkLegend(page);await readableText(page.getByRole('heading',{name:'Обзор производства',exact:true}));
    backgrounds.push(await page.locator('body').evaluate(element=>getComputedStyle(element).backgroundColor));
    await page.screenshot({path:info.outputPath(`dashboard-${theme}.png`),fullPage:true,animations:'disabled'});
    await page.getByTestId('station-painting').click();
    await expect(page.getByTestId('operation-characteristics-painting')).toBeVisible();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await page.getByTestId('operation-characteristics-painting').screenshot({path:info.outputPath(`characteristics-${theme}.png`),animations:'disabled'});
    await page.getByTestId('scada-scope-painting').screenshot({path:info.outputPath(`equipment-scope-${theme}.png`),animations:'disabled'});
    await nav(page,'automation');
    await expect(page.locator('.operation-card')).toHaveCount(3);
    await expect(page.getByTestId('automation-back')).toHaveCount(0);
    await readableText(page.locator('.operation-card-title').first());
    await page.screenshot({path:info.outputPath(`automation-landing-${theme}.png`),fullPage:true,animations:'disabled'});
  }
  expect(backgrounds[0]).not.toBe(backgrounds[1]);
  const other=await context.newPage();other.on('pageerror',error=>errors.push(error.message));
  try{
    await other.goto('/');await expect(other.locator('html')).toHaveAttribute('data-theme','light');
    await page.bringToFront();await setTheme(page,'dark');
    await expect(other.locator('html')).toHaveAttribute('data-theme','dark');
  }finally{await other.close();}

  await page.setViewportSize({width:390,height:844});
  for(const theme of ['light','dark'] as const){
    await setTheme(page,theme);await nav(page,'automation');
    await expect(page.locator('.operation-card')).toHaveCount(3);
    await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1)).toBe(true);
    await page.screenshot({path:info.outputPath(`automation-mobile-${theme}.png`),fullPage:true,animations:'disabled'});
    await page.getByTestId('automation-operation-assembly').click();
    await expect(page.getByTestId('operation-characteristics-assembly')).toBeVisible();
    await expect(page.getByTestId('emulation')).toHaveCount(0);
    await page.locator('summary').filter({hasText:/^Учебный стенд сборки$/}).click();
    const stand=page.getByTestId('emulation');
    await expect(stand).toBeVisible();
    const robot=stand.getByRole('group',{name:'Кинематическая схема робота R1',exact:true});
    await expect(robot).toBeVisible();
    await robot.scrollIntoViewIfNeeded();
    await robot.screenshot({path:info.outputPath(`assembly-schematic-mobile-${theme}.png`),animations:'disabled'});
    await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1)).toBe(true);
    await page.locator('summary').filter({hasText:/^Учебный стенд сборки$/}).click();
    await expect(page.getByTestId('emulation')).toHaveCount(0);
    await page.reload();await expect(page.locator('html')).toHaveAttribute('data-theme',theme);
  }
  expect(errors).toEqual([]);
});
