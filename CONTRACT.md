# Allur Twin — shared implementation contract

Russian-language functional industrial digital-twin app. Entire runtime, dependency installation, builds and tests via Docker. Host file edits allowed. No static fake metrics, no pretend AI, no placeholder buttons. Simulation clearly labeled. Postgres persistent state. No real factory connection claimed.

## Ownership
backend agent owns backend/**. frontend agent owns frontend/**. Root owns compose/Dockerfiles/docs/e2e and integration. Discuss contract changes before implementing.

## API (same origin /api, JSON)
GET /api/health -> {status:"ok",database:"ok"}
GET /api/state -> State below
GET /api/history?limit=120 -> array {sim_time,produced,rejected,throughput,wip,availability,quality}
GET /api/incidents -> array {id,station_id,station_name,severity:"warning"|"critical"|"info",kind,title,description,created_at,sim_time,status:"open"|"acknowledged"|"resolved",acknowledged_at?,resolved_at?}
POST /api/incidents/{id}/acknowledge -> incident
POST /api/control {running?:bool,speed?:1|10|30|60|120,shift_plan?:positive int} -> State
POST /api/advance {seconds:1..3600} -> State (explicit simulated-time advance)
PATCH /api/stations/{id} {cycle_seconds?:number>=5<=600,capacity?:int>=1<=10,buffer_capacity?:int>=1<=200,defect_rate?:number>=0<=0.5,speed_factor?:number>=0.1<=2,manual_stop?:bool} -> State
POST /api/reset {confirm:true} -> State (new shift only, incidents/history retained separated by run id; station configuration retained)
POST /api/scenarios {name:string,horizon_minutes:15..480,changes:[{station_id,cycle_seconds?,capacity?,buffer_capacity?,defect_rate?,speed_factor?,manual_stop?}]} -> Scenario
GET /api/scenarios -> array Scenario, newest first
GET /api/export -> CSV download of actual history
GET /api/import/template -> CSV template download
POST /api/import (multipart file) -> {imported:number,source:"telemetry",message:string}; atomic CSV validation, row errors via HTTP 422 detail; header timestamp,station_id,status,produced,rejected,queue,cycle_seconds. produced/rejected cumulative counters with rejected <= produced, statuses running/stopped/starved/blocked. Actual telemetry mode disables simulation mutating operations except explicit source switch.
POST /api/source {source:"simulation"|"telemetry"} -> State. Preserve distinct simulation and telemetry state; telemetry unavailable until imported.
SSE GET /api/events -> event `state`, JSON State every ~2 wall seconds; client may use polling fallback. 

State = {
 run_id:string, source:"simulation"|"telemetry", updated_at:ISO string, running:bool,speed:number,
 sim_time:number (elapsed modeled seconds), shift_duration:number, shift_plan:number,
 stations:Station[],
 metrics:{produced:number,good:number,rejected:number,wip:number,throughput:number,availability:number,performance:number,quality:number,oee:number,plan_progress:number,forecast_good:number},
 predictions:Prediction[],
 model:{name:string,description:string},
 telemetry_updated_at:ISO|null
}
Station = {id:string,name:string,order:number,cycle_seconds:number,capacity:number,buffer_capacity:number,defect_rate:number,speed_factor:number,manual_stop:bool,status:"running"|"stopped"|"starved"|"blocked",queue:number,in_process:number,completed:number,rejected:number,utilization:number,downtime_seconds:number,throughput:number,progress:number}
Percent-like metrics availability/performance/quality/oee/plan_progress/utilization/progress are 0..100 (plan_progress may exceed100). Throughput = good units per simulated hour. produced = finished final station total, good = final station accepted, rejected = all line rejects. quality = good/(good+all rejects); document this line-wide definition; performance based on nominal line bottleneck capacity. Use finite numbers. Telemetry unknown metrics are null: in_process/progress/utilization, wip/performance/oee/forecast_good; availability/throughput/quality may be null without sufficient observations. UI must display an em dash and never fabricate zero measurements.
Prediction = {station_id:string,station_name:string,type:string,severity:"info"|"warning"|"critical",title:string,description:string,eta_minutes:number|null}
Scenario = {id:string,name:string,created_at:ISO,horizon_minutes:number,changes:array,baseline:{good:number,rejected:number,wip:number,downtime_minutes:number},variant:{good:number,rejected:number,wip:number,downtime_minutes:number},delta:{good:number,rejected:number,wip:number,downtime_minutes:number},timeline:[{minute:number,baseline_good:number,variant_good:number}],explanation:string}
Scenario counts are additional outputs over horizon from same starting state with same random seed, clone isolated; never alter live simulation. Telemetry scenarios allowed only if model can be initialized faithfully; otherwise reject with explicit explanation.

## Interface expectations
Polished responsive industrial command center, Russian text, readable typography, dark/light themes with blue accents, green normal states, yellow warnings and red critical risks. Theme choice persists locally and synchronizes across browser tabs. Interactive production diagram with queues and clickable stations, editor panel, metric cards, live chart, prediction evidence, source and clock shown. Automation opens three operation cards (welding, painting, assembly); each displays its characteristics, robot readings and scoped equipment/maintenance. Overview station links open the same operation details. Scenario configurable inputs, saved comparisons and outcome chart. CSV import/export and template, informative validation/errors. No remote fonts/images required. Accessibility labels and clear error/success feedback. Destructive resets and physical-command approval retain their explicit confirmation flows.

## Backend expectations
FastAPI + SQLAlchemy + Postgres. requirements.txt pinned. Backend package app with main:app. Single uvicorn worker, transactional persistence and lock for runner/requests, stable restart. Real discrete-event or deterministic one-second simulation with queue capacity/backpressure, rejects, manual stops and resume. All counts flow from engine. Persist engine and RNG state. Meaningful automated tests for conservation, stop/backpressure, paired scenario isolation, CSV validation and restart persistence. Explain formulas and limits. No LLM API dependency. Forecast/alerts computed from actual model and observations.

## Расширение: роботная телеметрия

POST /api/import автоматически определяет роботный формат по robot_id/node_id. Канонические колонки: timestamp, robot_id, line_section, joint_temperature_c, vibration_mm_s, hydraulic_pressure_bar, cycle_status, error_code. Также принимаются pneumatic_pressure_bar и pneumatic_pressure; последняя единица неизвестна. Обязательны время с поясом, ID, статус и хотя бы одно измерение датчика. Пустые датчики возвращаются null. Новые строки дополняют серию при строго возрастающем времени для каждого робота. Сортировка внутри файла разрешена; дубли и любая ошибка отменяют весь импорт.

Роботный /api/state содержит telemetry_kind="robots", robots (последнее наблюдение каждого ID), observation_count, observation_start. stations=[]; все производственные metrics=null. /api/history остаётся производственной историей. /api/robots/history?robot_id=...&limit=2000 возвращает {total,rows}, лимит 1..20000. /api/robots/export выгружает всю роботную серию, /api/import/robots-template возвращает заголовок. Каждый датчик хранится независимо. Инциденты создаются из статусов/кодов с временем наблюдения, учитываются промежуточные переходы внутри файла. Без справочника кодов и норм производителя диагностика отказов не выполняется.
POST /api/source принимает необязательный telemetry_kind: production или robots только вместе с source: telemetry. Недоступная серия даёт 409. /api/state.telemetry_available сообщает наличие каждой серии; старый запрос только с source сохраняет прежнее поведение.

## Аналитика, правила, предварительная проверка и JSON

- GET/PUT /api/robots/config?robot_id=...: expected_interval_seconds (целое 1..86400), limits (объект по именам датчиков с nullable low_critical/low_warning/high_warning/high_critical), error_codes (до 100 кодов с description, action, severity). Порядок границ строго возрастает. PUT полностью заменяет настройки робота и пересчитывает текущие события, сохраняя выбранный источник.
- GET /api/robots/analytics?robot_id=...&start=...&end=...: статистика за период, длительности состояний, неизвестные интервалы и тренды датчиков. Формулы и минимальная достаточность данных описаны в README. Недостаточные данные дают trend=null с объяснением, а не выдуманные числа.
- POST /api/robots/measurements: {measurements:[...]} до 1000 объектов с каноническим robot_id. Общая атомарная проверка и сохранение с CSV. На успехе возвращается imported/source/message, ошибка 422 не сохраняет часть пакета.
- POST /api/import/preview: multipart file, валидирует весь файл, существующие временные границы, счётчики и лимиты, ничего не сохраняет. Ответ kind/count/identifiers/start/end/preview (первые 10 строк).
- GET /api/imports: последние 200 успешных загрузок с filename, kind, count, start, end, imported_at.
- История роботов поддерживает start/end в ISO 8601 с поясом, offset от конца периода и limit. Экспорт принимает необязательные robot_id/start/end. Границы включительны.

При пропуске датчика активное нарушение его границы остаётся открытым до нормального измерения или изменения правила. Статус Error/Fault/Alarm остаётся критическим независимо от настройки кода. Массовые переходы в CSV обрабатываются в одной транзакции с сохранением каждого события. Для составных ключей длиннее 128 символов используется SHA-256; идентификатор и текст события сохраняются полностью в данных.
GET /api/incidents сохраняет все активные события и дополняет выдачу последними завершёнными до 2000 записей. GET /api/incidents/export возвращает полный CSV-журнал всех источников/серий. Никакое активное событие не скрывается лимитом последних записей.


## Version 2 access and storage
All endpoints except GET /api/health and POST /api/auth/login require authentication. User mutations require X-CSRF-Token. Roles: viewer, operator, admin. An expiring Bearer integration key authorizes only POST /api/robots/measurements and requires Idempotency-Key. Replays with identical payload return the original receipt; conflicting content returns 409.
GET /api/dashboard returns encoded {state,history,incidents} for polling.
GET /api/robots/detail?robot_id=...&start=...&end=...&offset=0 returns {history,analytics,config,warning,computed_at}; history pages contain at most 500 rows and selected-period analytics at most 100000 rows. Cached detail results have a maximum five-second lifetime after measurement imports. Configuration changes invalidate them immediately. Direct history/export reads use committed observations.
Robot observations use an indexed table; snapshot storage_version=2 contains only compact current readings and robot_count. No 200000-row total cap applies to robot history. One PostgreSQL lease owner per database; loss of its connection terminates the process for restart and durable reload.

## Version 2.1 extension

GET /api/manufacturing exposes separate immutable vehicle-quality records and latest imported case report. POST /api/quality/records accepts idempotent record_id batches. DOCX case import does not change the active observation/simulation source.

GET /api/automation exposes measured robot state, latest commands and all unresolved physical commands. POST /api/automation/commands only proposes; approve requires operator confirmation and fresh controller data, plus PHYSICAL_CONTROL_ENABLED. A scoped gateway key claims each command once and acknowledges its lease. Ambiguous results block the robot until audited reconciliation.

GET /api/ai/status and POST /api/ai/analyze call a real local Ollama model. No outbound cloud provider or canned answer fallback. A model can create only a proposal with explicit allow_command; it never approves or writes to a controller. Full fields and controller contract are documented in docs/MANUFACTURING_AND_AI.md and gateway/README.md.

