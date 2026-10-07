"""Server port of scada-stand.zip physics. All values describe a virtual stand.

Declarations in emulation_seed.json were extracted from the user's archive.
Units, thresholds, initial wear, coupling and eleven faults are retained.
State is JSON-only, with a persisted PRNG; the model has no equipment I/O.
"""
import copy
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import emulation_support as support

SEED = json.loads(Path(__file__).with_name('emulation_seed.json').read_text(encoding='utf-8'))
LEVEL = {'ok': 0, 'warn': 1, 'alarm': 2}


def initial():
    start = datetime.now(timezone(timedelta(hours=5))).replace(hour=7, minute=0, second=0, microsecond=0)
    while start.weekday() > 4:
        start += timedelta(days=1)
    state = dict(version=1, t=0.0, runH=0.0, downH=0.0, carsF=0.0, nokF=0.0,
                 paused=True, line='run', speed=60, stops=0, revision=0, seed=20261006,
                 start=start.isoformat(), components=copy.deepcopy(SEED['components']),
                 faults=[], alarms=[], events=[], stock={k: p.get('stock', 0) for k, p in SEED['parts'].items()},
                 receipts=[], sequence=0)
    model = Stand(state)
    model.signals(0, True)
    model.evaluate()
    support.stock_check(model)
    model.sample()
    return state


class Stand:
    def __init__(self, state):
        support.migrate(state)
        self.s = state
        self.cs = state['components']
        self.by = {c['id']: c for c in self.cs}
        self.parts, self.causes, self.actions, self.faults = (SEED[k] for k in ('parts','causes','actions','faults'))

    def random(self):
        # Mulberry32, matching the archive (unsigned arithmetic modulo 2**32).
        seed = self.s['seed'] = (self.s['seed'] + 0x6D2B79F5) & 0xffffffff
        t = ((seed ^ (seed >> 15)) * (1 | seed)) & 0xffffffff
        t ^= (t + (((t ^ (t >> 7)) * (61 | t)) & 0xffffffff)) & 0xffffffff
        return ((t ^ (t >> 14)) & 0xffffffff) / 4294967296

    def noise(self):
        return math.sqrt(-2 * math.log(max(self.random(), 1e-12))) * math.cos(2 * math.pi * self.random())

    @staticmethod
    def kind(c):
        return c.get('sub', c['kind'])

    def defs(self, c):
        key = ('cableUp' if c.get('upper') else '') if c['kind'] == 'cable' else self.kind(c)
        return SEED['signals'].get(key, [])

    def cv_load(self):
        r, c = self.by['CV-rollers'], self.by['CV-chain']
        return .6 + .25 * r['wear']**3 + .1 * c['wear']**2 + r['fx'].get('jam', 0)

    def lube_flow(self):
        c = self.by['CV-lube']
        return 0 if c['wear'] >= .97 else max(0, 100 - c['fx'].get('clog', 0))

    def chain_rate(self):
        return (self.cv_load() / .7)**2 / 30000 * (1 + 8 * max(0, 1-self.lube_flow()/100))

    def targets(self, c, run):
        f, w = c['fx'], c['wear']
        L, w3, k = c.get('load', 0)*f.get('load', 1), w**3, self.kind(c)
        if k == 'motor': return {'temp': 30+60*L**1.5+f.get('temp', 0) if run else 27+f.get('temp', 0)*.35}
        if k == 'reducer': return {'vib': .45+4.2*w**6+f.get('vib', 0)+max(0,L-.9)*1.2, 'res': 2+14*w**5+f.get('res', 0)} if run else {}
        if k == 'cable': return {'err': max(0,w-.8)*30+f.get('err', 0)} if c.get('upper') and run else {}
        if k == 'battery': return {'v': 3.65-.75*w3}
        if k == 'fan': return {'ct': 30+12*w3+f.get('ct', 0)-(0 if run else 2)}
        if k == 'cups': return {'vac': -78+18*w3+f.get('vac', 0)}
        if k == 'spindles': return {'spr': 1.2+4*w3+f.get('spr', 0)} if run else {}
        if k == 'calib': return {'drift': .1+2.2*w3}
        if k == 'cyl': return {'stroke': 420+180*w3} if run else {}
        if k == 'clamps': return {'p': 6-1.5*w3}
        if k == 'cvmot': return {'cur': 15.2*self.cv_load(), 'temp': 30+55*self.cv_load()**2} if run else {'temp':25}
        if k == 'cvgbx': return {'vib':.6+3.8*w**6+.4*max(0,self.cv_load()-.8)+f.get('vib',0), 'ot':38+30*self.cv_load()**2+max(0,self.by['CV-cvoil']['wear']-1)*25+f.get('ot',0)} if run else {'ot':26+f.get('ot',0)*.2}
        if k == 'vfd': return {'tr':32+22*self.cv_load() if run else 30}
        if k == 'chain': return {'el':3*w}
        if k == 'lube': return {'lvl':100*max(0,1-w), **({'flow':self.lube_flow()} if run else {})}
        if k == 'take':
            tr = 20+70*self.by['CV-chain']['wear']
            return {'tr':tr, 'tn':12-max(0,tr-88)*.6}
        if k == 'enc': return {'slip':.3+3*w3+f.get('slip',0)} if run else {}
        if k == 'pos': return {'pe':.8+2*w3+f.get('pe',0)} if run else {}
        return {}

    def rate(self, c, forecast=False):
        f, k = c['fx'], c['kind']
        L, m = c.get('load',0)*f.get('load',1), f.get('rate',1)
        if k == 'reducer': return c['speed']*L**(10/3)/6000*m
        if k == 'motor':
            T = self.targets(c,True)['temp'] if forecast else c['raw'].get('temp',70)
            return 2**((T-80)/10)/40000*m
        if k == 'brake': return 6*(1+.5*L)/1e5*m
        if k == 'cable': return c['flex']*60/c['rated']*m
        if k == 'battery': return 1/8000
        if k == 'fan': return m/30000
        if k == 'tool': return m/c['hours'] if c.get('hours') else c['perCar']*60/c['rated']*m
        k, L = self.kind(c), self.cv_load()
        if k == 'cvmot': return (L/.6)**3/30000*m
        if k == 'cvgbx': return (L/.7)**(10/3)/25000*m*(1+3*max(0,self.by['CV-cvoil']['wear']-1))
        if k == 'cvoil': return 1/5000
        if k == 'cvbrk': return 1/60000
        if k == 'vfd': return 2**(((32+22*L if forecast else c['raw'].get('tr',45))-40)/10)/60000*m
        if k == 'sprk': return self.chain_rate()*.6
        if k == 'chain': return self.chain_rate()*m
        if k == 'slats': return 1/60000
        if k == 'rollers': return m/20000
        if k == 'lube': return max(.1,self.lube_flow()/100)/400
        return {'take':1/40000,'enc':1/25000,'pos':1/3000}.get(k,0)

    def signals(self, dt, run):
        for c in self.cs:
            targets = self.targets(c,run)
            for d in self.defs(c):
                k = d['k']
                if k not in targets: continue  # Stopped dynamic sensors retain their last sample.
                r = c['raw'].get(k,targets[k])
                r = r+(targets[k]-r)*(1-math.exp(-dt/d['lag'])) if d.get('lag') else targets[k]
                c['raw'][k] = r
                c['val'][k] = max(d.get('min',-1e100),r+self.noise()*d['sd'])
                c.setdefault('sampled_at',{})[k] = self.s['t']

    @staticmethod
    def level(d, value, previous):
        sign = -1 if d.get('lo') else 1
        x, w, a = sign*value, sign*d['warn'], sign*d['alarm']
        h = abs(a-w)*.15
        if x >= a or previous == 'alarm' and x >= a-h: return 'alarm'
        if x >= w or previous != 'ok' and x >= w-h: return 'warn'
        return 'ok'

    def log(self, level, text):
        self.s['sequence'] += 1
        self.s['events'].insert(0,dict(id=self.s['sequence'],t=self.s['t'],level=level,text=text))
        del self.s['events'][300:]

    def alarm(self, c, key, label):
        identity = c['id']+':'+key
        if any(a['id']==identity for a in self.s['alarms']): return
        self.s['trips'][identity] = self.s['trips'].get(identity,0)+1
        self.s['alarms'].append(dict(id=identity,component_id=c['id'],key=key,label=label,t=self.s['t'],
            value=c['wear']*100 if key=='life' else c['val'].get(key),
            unit='%' if key=='life' else next((d['unit'] for d in self.defs(c) if d['k']==key),'')))
        self.log('alarm',f"{c['robot']} · {c['name']}: {label}")
        support.notify(self,'alarms','alarm',f"{c['robot']} · {c['name']}: {label}. "
                       f"Причина: {self.causes.get(self.kind(c),'требуется диагностика')}.")
        if self.s['line'] == 'run':
            self.s['line'] = 'stop'
            self.s['stops'] += 1
            self.by['CV-cvbrk']['wear'] = min(1.2,self.by['CV-cvbrk']['wear']+1/1500)
            self.log('alarm','Дзидока: виртуальная линия остановлена')

    def evaluate(self):
        for c in self.cs:
            for d in self.defs(c):
                k = d['k']
                if k not in c['val']: continue
                prev = c['lv'].get(k,'ok')
                lv = c['lv'][k] = self.level(d,c['val'][k],prev)
                if lv == 'alarm': self.alarm(c,k,d['label'])
                elif lv == 'warn' and prev == 'ok': self.log('warn',f"{c['robot']} · {c['name']}: {d['label']} — внимание")
            c['lv']['life'] = 'alarm' if c['wear']>=1 else 'warn' if c['wear']>=.8 else 'ok'
            if c['wear'] >= 1: self.alarm(c,'life','Ресурс исчерпан')
            if c['wear'] >= .8 and not c['n80']:
                c['n80'] = True
                self.log('warn',f"{c['robot']} · {c['name']}: износ выше 80%")
                support.notify(self,'maintenance','warn',f"Учебное ТО: {c['robot']} · {c['name']}, "
                               f"износ {c['wear']*100:.1f}%, деталь {c['part']}. Проверьте прогноз замены.")
            c['level'] = max(c['lv'].values(),key=LEVEL.get)

    def sample(self):
        for c in self.cs:
            for k, v in c['val'].items():
                h = c['hist'].setdefault(k,[])
                # Do not present retained readings as fresh measurements.
                at = c.get('sampled_at',{}).get(k,0)
                if not h or h[-1]['t'] != at:
                    h.append(dict(t=at,value=v))
                    del h[:-120]

    def advance(self, seconds):
        # <=15 simulated seconds per integration step, even at x3600. Retain
        # the first threshold crossing instead of skipping it in a large jump.
        left = seconds
        while left > 1e-9:
            dt = min(15,left)
            left -= dt
            h, run = dt/3600, self.s['line']=='run'
            self.s['t'] += h
            if run:
                self.s['runH'] += h
                self.s['carsF'] += dt/60
                spr = self.by['R2-spindles']['val'].get('spr',2)
                self.s['nokF'] += dt/60*min(.3,max(0,(spr-2.6)/12))
            else: self.s['downH'] += h
            for c in self.cs:
                if run or c['kind'] in ('battery','fan') or c.get('sub')=='calib':
                    c['wear'] = min(1.2,c['wear']+self.rate(c)*h)
            self.signals(dt,run)
            self.evaluate()
        self.sample()
        support.receive_due(self)

    def apply_faults(self):
        for c in self.cs: c['fx'] = {}
        for f in SEED['faults']:
            if f['id'] not in self.s['faults']: continue
            for key in f['t']:
                fx = self.by[key]['fx']
                for k,v in f['fx'].items():
                    fx[k] = fx.get(k,1)*v if k in ('load','rate') else fx.get(k,0)+v

    def fault(self, identity, active):
        f = next(f for f in SEED['faults'] if f['id']==identity)
        if (identity in self.s['faults']) == active: return
        if active: self.s['faults'].append(identity)
        else: self.s['faults'].remove(identity)
        self.apply_faults()
        # Explicit fault removal is a virtual repair. Refresh static operational
        # checks even while stopped so position faults can be cleared; thermal
        # sensors still need time to cool (lag is preserved).
        self.signals(0,True)
        self.evaluate()
        self.log('action',('Введена неисправность: ' if active else 'Устранена неисправность: ')+f['name'])

    def replace(self, identity):
        c = self.by[identity]
        p = SEED['parts'][c['part']]
        if not p.get('service'):
            if self.s['stock'][c['part']] < 1: raise ValueError('Нет детали на учебном складе. Сначала пополните запас эмуляции.')
            self.s['stock'][c['part']] -= 1
        c.update(wear=0,n80=False,lv={},raw={},val={},hist={},sampled_at={})
        for f in SEED['faults']:
            if f.get('fix') == identity and f['id'] in self.s['faults']: self.s['faults'].remove(f['id'])
        self.apply_faults()
        self.signals(0,True)
        self.evaluate()
        self.sample()
        # Keep the latch until the operator acknowledges the recovered alarm.
        self.log('action',f"Учебное ТО: {c['robot']} · {c['name']}")
        support.stock_check(self)

    def clock(self, elapsed=None):
        start = datetime.fromisoformat(self.s['start'])
        days, rem = divmod(self.s['t'] if elapsed is None else elapsed,16)
        weeks, rest = divmod(int(days),5)
        start += timedelta(days=weeks*7)
        while rest:
            start += timedelta(days=1)
            if start.weekday()<5: rest -= 1
        return (start+timedelta(hours=rem)).isoformat()

    def view(self, history_for=None):
        components = []
        for c in self.cs:
            rate = self.rate(c,True)
            components.append(dict(id=c['id'],robot=c['robot'],node=c['node'],name=c['name'],part=c['part'],
                wear_pct=c['wear']*100,level=c['level'],remaining_hours=max(0,1-c['wear'])/rate if rate else None,
                kind=self.kind(c),rate_per_hour=rate,speed_ratio=c.get('speed'),
                load_pct=c['load']*c['fx'].get('load',1)*100 if 'load' in c else self.cv_load()*100 if self.kind(c) in ('cvmot','cvgbx','vfd') else None,
                forecast=support.forecast(self,c),
                cause=SEED['causes'].get(self.kind(c),''),action=SEED['actions'].get(self.kind(c),''),
                sensors=[dict(**d,value=c['val'].get(d['k']),level=c['lv'].get(d['k'],'ok'),
                    sampled_at=c.get('sampled_at',{}).get(d['k']),history=c['hist'].get(d['k'],[]) if history_for==c['id'] else []) for d in self.defs(c)]))
        phase = (self.s['runH']*3600%60)/60
        stations = []
        for i, s in enumerate(SEED['stations']):
            joints = [round(a+b*math.sin(phase*math.tau+i*.7+j*.5),2) for j,(a,b) in enumerate([(0,35),(-65,25),(35,30),(0,25),(-20,20),(0,30)])]
            station_components = [c for c in components if c['robot']==s['robot']]
            weakest = min(station_components,key=lambda c:c['remaining_hours'] if c['remaining_hours'] is not None else float('inf'))
            stations.append(dict(id=s['robot'],name=s['name'],operation=s['op'],joints=joints,
                payload=s['payload'],tool=s['tool'],shape=s['shape'],load=s['load'],speed=s['speed'],
                operational_status=self.operational_status(s['robot']),body_number=max(0,int(self.s['carsF'])-i),
                phase=phase,weakest_component_id=weakest['id'],
                level=max((c['level'] for c in self.cs if c['robot']==s['robot']),key=LEVEL.get)))
        alarms = [dict(**a,recovered=self.by[a['component_id']]['lv'].get(a['key'])!='alarm',
                       trips=self.s['trips'].get(a['id'],1),cause=self.causes.get(self.kind(self.by[a['component_id']]),''),
                       action=self.actions.get(self.kind(self.by[a['component_id']]),'')) for a in self.s['alarms']]
        catalog = support.parts_view(self)
        conveyor = dict(load_pct=self.cv_load()*100,roller_resistance_pct=25*self.by['CV-rollers']['wear']**3,
            chain_resistance_pct=10*self.by['CV-chain']['wear']**2,jam_pct=100*self.by['CV-rollers']['fx'].get('jam',0),
            speed_m_min=6 if self.s['line']=='run' and not self.s['paused'] else 0,
            distance_km=self.s['runH']*6*60/1000,current_a=self.by['CV-cvmot']['val'].get('cur'),
            lubrication_pct=self.by['CV-lube']['val'].get('lvl'),lubrication_flow_pct=self.lube_flow(),
            stops=self.s['stops'],operational_status=self.operational_status('CV'))
        return dict(source='emulation',t=self.s['t'],model_time=self.clock(),paused=self.s['paused'],line=self.s['line'],speed=self.s['speed'],
            shift=1 if self.s['t']%16<8 else 2,working_hours_per_day=16,working_days_per_week=5,
            revision=self.s['revision'],run_hours=self.s['runH'],down_hours=self.s['downH'],cars=int(self.s['carsF']),
            rejected=int(self.s['nokF']),stops=self.s['stops'],phase=phase,stations=stations,components=components,
            alarms=alarms,faults=[dict(**f,active=f['id'] in self.s['faults']) for f in SEED['faults']],events=self.s['events'],
            parts=catalog,orders=self.s['orders'],notifications=self.s['notifications'],channels=self.s['channels'],
            conveyor=conveyor,summary=support.summary(self,catalog))

    def operational_status(self, robot):
        if any(self.by[a['component_id']]['robot']==robot for a in self.s['alarms']): return 'stop'
        if self.s['paused']: return 'pause'
        if self.s['line']!='run': return 'idle'
        return 'warn' if any(c['level']!='ok' for c in self.cs if c['robot']==robot) else 'run'
