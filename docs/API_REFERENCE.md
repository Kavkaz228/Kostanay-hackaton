# Справочник API Allur twin 2.0

Спецификация этой поставки: [openapi.json](openapi.json). Она получена из `create_app(...).openapi()` актуального кода без запуска lifespan и без подключения к рабочей базе. Версия приложения — 2.0.0; в спецификации 82 пути и 87 операций. Для работающей установки доступны `GET /api/openapi.json` и HTML-указатель `GET /api/docs` после входа.

Этот документ описывает контракт интеграции. Поля входных моделей, перечисления и ограничения находятся в `components.schemas` JSON-спецификации. Некоторые ответы формируются динамически без `response_model`; для них OpenAPI не перечисляет все поля. Авторизация реализована middleware, поэтому одного автогенерируемого OpenAPI недостаточно для настройки клиента: правила cookie, CSRF и Bearer ниже обязательны.

## 1. Адреса, формат и общие правила

Базовый адрес локальной установки: `http://localhost:8088`. Все прикладные методы начинаются с `/api`. JSON передаётся с `Content-Type: application/json`, CSV — multipart-полем `file`; экспорт возвращает файл. Объектные входные схемы обычно запрещают лишние поля и нечисловые значения `NaN`/`Infinity`. Имена полей чувствительны к регистру. Числа передаются числами, не строками. `null` означает неизвестное значение, а не ноль.

Временные метки измерений содержат часовой пояс, например `2026-10-08T08:00:00+05:00`; сервер нормализует наблюдения в UTC. Примеры дат ниже служат форматом: отправляйте фактическое время измерения. Свежесть — отдельное свойство данных; успешный импорт исторического CSV не делает показания текущими.

Ответы получают `X-Request-ID`; сохраните его для разбора ошибки. Основные JSON-ответы имеют `Cache-Control: no-store`. `GET /api/events` — SSE-поток `event: state` примерно каждые две секунды; он повторно проверяет сессию и закрывается после её отзыва. Мониторинг роботов работает на сервере независимо от этого потока и открытого браузера.

## 2. Вход, сессии и роли

Публичны только `GET /api/health` и `POST /api/auth/login`. Вход принимает `{ "username": "admin", "password": "…" }`, устанавливает HttpOnly cookie `allur_session` с `SameSite=Strict` на восемь часов и возвращает `id`, `username`, `role`, `active`, `must_change`, `csrf`. Сохраняйте cookie в cookie jar. Не записывайте пароль, cookie, CSRF и ключи в журналы.

`GET /api/auth/me` восстанавливает сведения о текущем пользователе и CSRF. Для всех изменяющих запросов с cookie, кроме login, нужен заголовок `X-CSRF-Token` с этим значением. Если передан `Origin`, он должен совпасть с `PUBLIC_ORIGIN` либо базовым адресом запроса. Для размещения за HTTPS-прокси настройте `PUBLIC_ORIGIN` и `COOKIE_SECURE=true`.

При первом входе `must_change=true`: до смены временного пароля доступны только `/api/auth/me`, `/api/auth/password`, `/api/auth/logout`. Смена принимает `{ "current_password": "…", "new_password": "…" }`, требует 15–128 символов и минимум шесть различных символов, отзывает **все** сессии пользователя и требует повторного входа. Новый пароль должен отличаться от старого. Logout отзывает текущую сессию.

| Роль/ключ | Чтение | Изменения |
|---|---|---|
| `viewer` | Прикладные GET, кроме `/api/admin/*` | Только собственные auth-действия и read-only `POST /api/emulation/assistant`; анализ `/api/ai/analyze` недоступен |
| `operator` | Прикладные GET, кроме `/api/admin/*` | Симуляция, импорт, настройки роботов/SCADA, подтверждение уведомлений, анализ ИИ, предложения и подтверждения команд с серверными проверками |
| `admin` | Все пользовательские GET | Дополнительно `/api/admin/*`: пользователи, ключи, аудит, шлюзы, сверка неизвестного результата команды |
| Ключ интеграции | Не даёт общего чтения API | Только `POST /api/robots/measurements` и `POST /api/scada/readings` |
| Ключ шлюза | Не даёт общего чтения API | Только `POST /api/gateway/next`, `/api/gateway/result`, `/api/robots/measurements`; показания только своего робота |

Администратор создаёт ключ интеграции через `POST /api/admin/tokens` с `{ "name": "Collector 01", "days": 90 }` (1–365 дней). Ответ содержит `token`, `id`, `expires`; полный token показывается только при создании. Интеграционный клиент передаёт `Authorization: Bearer <token>` и не использует cookie/CSRF. `DELETE /api/admin/tokens/{token_id}` отзывает ключ. Идентификатор в URL — выданный `id`, не открытый token.

Администратор не может менять собственную роль или блокировать себя через административное редактирование: это делает другой администратор. Созданный пользователю или назначенный администратором пароль требует замены при входе.

## 3. Измерения роботов: полный путь данных

1. Collector получает показания контроллера/датчиков, сохраняет исходное время и сопоставляет единицы.
2. Администратор выдаёт отдельный ключ интеграции. После первого принятого измерения робот появляется в системе; оператор задаёт его границы. Для ещё неизвестного `robot_id` GET/PUT конфигурации возвращает `404`.
3. Collector отправляет пакет `POST /api/robots/measurements` с новым `Idempotency-Key` для нового содержимого.
4. Сервер валидирует пакет, сохраняет историю и квитанцию транзакционно. История доступна через `/api/robots/history`, агрегаты — `/api/robots/analytics`, карточка — `/api/robots/detail`.
5. Фоновый монитор каждые десять секунд проверяет заданные границы, коды/статусы и свежесть; события отображаются через `/api/monitoring`. Отдельный worker добавляет гипотезы локального ИИ, если включён и доступен.

### 3.1. Пакет

```json
{
  "measurements": [
    {
      "timestamp": "2026-10-08T08:00:00+05:00",
      "robot_id": "WELD-01",
      "line_section": "Сварка",
      "cycle_status": "running",
      "error_code": "",
      "joint_temperature_c": 67.5,
      "vibration_mm_s": 1.8,
      "hydraulic_pressure_bar": 120.0,
      "operation": "welding",
      "controller_mode": "automatic",
      "safety_state": "normal"
    }
  ]
}
```

Обязательны `measurements` (1–1000 элементов), а внутри каждой записи — `timestamp`, `robot_id`, `cycle_status` **и хотя бы одно непустое показание датчика**. Последнее правило проверяется в обработке измерений, не только в Pydantic-схеме. Метки времени без пояса, повтор `robot_id + timestamp` внутри пакета, управляющие символы в идентификаторах и невалидные диапазоны отклоняются. Идентификатор робота/статус — 1–128 символов. `line_section` и `error_code` необязательны.

| Датчик/группа | Единица и ограничения |
|---|---|
| `joint_temperature_c` | °C; не ниже −273,15 |
| `vibration_mm_s` | мм/с; неотрицательное |
| `hydraulic_pressure_bar`, `pneumatic_pressure_bar` | бар; неотрицательное |
| `pneumatic_pressure` | Совместимое старое поле; используйте явное поле `pneumatic_pressure_bar`, если источник измеряет в барах |
| `paint_volume_l`, `paint_capacity_l` | Литры; объём остатка не больше вместимости |
| `electrode_count` | Целое неотрицательное количество |
| `welding_current_a`, `motor_current_a` | Амперы; неотрицательные |
| `speed_percent`, `cycle_progress_pct` | 0–100% |
| `joint_1_deg` … `joint_6_deg` | Градусы; от −3600 до 3600 |

Метаданные: `operation` = `welding`/`painting`/`assembly`/пусто; `controller_mode` = `automatic`/`manual`/`offline`/`unknown`/пусто; `safety_state` = `normal`/`protective_stop`/`emergency_stop`/`unknown`/пусто. Для статусов цикла используются данные источника; `error`, `fault`, `alarm`, `ошибка`, `авария` считаются критическими, `warning`/`предупреждение` — предупреждением. Код ошибки расшифровывается только заданным пользователем справочником.

### 3.2. Идемпотентность и повтор

`Idempotency-Key` обязателен для Bearer-ключа интеграции/шлюза: 8–128 ASCII-символов. Например `collector01-20261008-batch-00001`. Область ключа привязана к идентичности отправителя. Повтор того же пакета с тем же ключом возвращает сохранённую квитанцию; другой пакет с этим ключом получает `409`. После таймаута или `503` повторяйте исходное тело с исходным ключом. Для исправленного/нового пакета создавайте новый ключ. Не рассчитывайте на этот заголовок для произвольных других методов: у них собственные контракты повторов.

Новые наблюдения каждого робота должны иметь timestamp **строго позже последнего сохранённого**. Пакет с более старой/той же меткой без совпавшей квитанции отклоняется как `422`; это не интерфейс произвольной дозагрузки пропущенной истории. Передавайте пакеты в порядке времени и повторяйте недоставленный пакет с исходным ключом до отправки следующего. В серии допускается до 1000 роботов. Квитанция содержит результат импорта, а не подтверждение выполнения действий на оборудовании.

```sh
# ROBOT_API_TOKEN задаётся в окружении процесса, payload сохранён в readings.json.
curl --fail-with-body -X POST http://localhost:8088/api/robots/measurements \
  -H "Authorization: Bearer $ROBOT_API_TOKEN" \
  -H "Idempotency-Key: collector01-20261008-batch-00001" \
  -H "Content-Type: application/json" --data-binary @readings.json
```

### 3.3. Границы робота

`PUT /api/robots/config?robot_id=WELD-01` с пользовательской сессией оператора/администратора:

```json
{
  "expected_interval_seconds": 60,
  "limits": {
    "joint_temperature_c": { "high_warning": 70.0, "high_critical": 80.0 },
    "vibration_mm_s": { "high_warning": 2.0, "high_critical": 3.0 }
  },
  "error_codes": {
    "E101": {
      "description": "Пример описания из справочника оборудования",
      "action": "Проверить состояние по утверждённой инструкции",
      "severity": "warning"
    }
  }
}
```

Числа здесь учебные, не нормативы для реального оборудования. Передайте фактические утверждённые границы. PUT задаёт конфигурацию целиком: не используйте неполный объект как PATCH. Интервал 1–86400 секунд; данные становятся устаревшими после трёх ожидаемых интервалов. До четырёх направленных границ: `low_critical < low_warning < high_warning < high_critical` (пропущенные допустимы). Коды нормального состояния `0`, `none`, `null`, `ok` нельзя описывать как ошибку. Без границ система не придумывает паспортные нормы.

## 4. SCADA: оборудование, узлы, ресурс, ТО и склад

SCADA хранит собственные справочники и измерения узлов. Создайте `AssetBody` (`id`, `name`, `kind`), затем `ComponentBody` (`id`, `asset_id`, `name`), задайте реальные пороги и при необходимости паспортную модель ресурса. `robot_id` актива и `robot_metric_map` узла связывают роботную телеметрию с метриками узла. Без привязки нельзя считать их одним источником.

`POST /api/scada/readings` принимает `{ "readings": [...] }` (1–1000). В каждой записи обязательны `record_id`, `component_id`, `timestamp`; также нужны непустой `metrics` или хотя бы один счётчик `runtime_hours`, `total_cycles`, `distance_km`. Пример после создания узла `WELD-01-reducer`:

```json
{
  "readings": [{
    "record_id": "collector01-reading-00001",
    "component_id": "WELD-01-reducer",
    "timestamp": "2026-10-08T08:00:00+05:00",
    "metrics": { "temperature_c": 67.5, "vibration_mm_s": 1.8 },
    "runtime_hours": 1500.0
  }]
}
```

Повторы SCADA определяются `record_id`: сохраняйте его при повторной доставке одной записи. Изменённое содержимое с занятым идентификатором — конфликт. Timestamp узла не может быть более чем на пять секунд в будущем. Для модели ресурса, отличной от `none`, нужны `rated_life` и `specification` с основанием; календарная дополнительно требует `commissioned_at`. Ресурс — инженерная оценка по выбранной модели и введённым характеристикам.

Изменение конфигурации узла принимает `ComponentUpdate`: полная `config` и `expected_revision`. Изменение операции актива требует `operation`, `expected_revision`, `request_id`. При `409` перечитайте карточку: другой оператор мог изменить её. Не делайте слепой повтор с новой ревизией без проверки.

Запчасти → предложения → заказ → подтверждение → приёмка — локальный учёт. Поставщику ничего не отправляется. Цены в `price_minor` — целое число минимальных денежных единиц, валюта отдельным полем. Складские движения и приёмка используют `record_id`; заказы/задачи — собственный `id`. Завершение ТО требует текстовое `evidence`, сброс счётчиков задаётся явно. Рабочий календарь влияет на планирование дат: `weekdays` 0=понедельник … 6=воскресенье, `holidays` — исключения.

## 5. Мониторинг и локальный ИИ

`GET /api/monitoring` возвращает состояние процесса (`enabled`, `interval_seconds`, `last_scan_at`, `last_error`), число роботов, `active_count`, `unread_count`, `ai_enabled` и `alerts`. В истории выдаётся до 200 событий с приоритетом активных; счётчики учитывают все активные события.

Событие содержит `id`, `robot_id`, `type`, `severity`, `status`, `title`, `description`, `observed_at`, `first_seen_at`, `last_seen_at`, `resolved_at`, `acknowledged`, `recommendation`; ИИ дополняет `ai_status`, `ai_analysis`, `ai_actions`, `ai_error`, при наличии — `ai_observed_at`. Уровни: `warning`/`critical`; состояния: `active`/`resolved`. Подтверждение `POST /api/monitoring/{identifier}/acknowledge` доступно оператору/администратору и общее для сотрудников. Оно не закрывает проблему. Для восстановления нужны свежие нормальные показания соответствующего датчика; частичный пакет без него не считается восстановлением.

`GET /api/ai/status` сообщает `ready`, `model`, `local_only`, `message`, иногда `digest`. `GET /api/ai/context?data_source=emulation&robot_id=R2` возвращает детерминированный отчёт без обращения к модели: `has_data`, `readings`, `metrics`, `findings`, `missing_data`, `next_steps`, источник и время.

```json
{
  "question": "Какие отклонения есть у R2 и что проверить сотруднику?",
  "data_source": "emulation",
  "robot_id": "R2",
  "allow_command": false
}
```

Это тело `POST /api/ai/analyze`. Вопрос 3–2000 символов. `data_source`: `plant` — фактические данные, `emulation` — учебный стенд (`R1`–`R4`, `CV` или вся установка), `simulation` — модель всей производственной линии без `robot_id`. Ответ включает `facts`, `analysis`, `recommendations`, `answer_kind`, `model`, `proposal`. `facts.readings` содержит значения/единицы/время, `facts.findings` — вычисленные отклонения. Предложения модели имеют `title`, `reason`, `steps`, `priority`, `evidence_ids`, `origin`. Сервер допускает ссылки только на переданные факты; это проверка происхождения, а не гарантия правильности диагноза.

Пустой источник возвращает `answer_kind=data_required` и действия по подготовке данных, без выдуманных значений и вызова модели. Учебные источники не смешиваются с цеховыми данными. Ответы локальной модели проходят ограниченную JSON-схему; при невалидном выводе — `502`. Данные и советы по правилам остаются доступны через context. Одновременно работает один модельный запрос, включая фоновый монитор: при занятости `429`.

## 6. Команды и граница управления

По умолчанию `PHYSICAL_CONTROL_ENABLED=false`. Монитор сам не создаёт и не отправляет команды. Обычный AI-запрос использует `allow_command=false`. Для явно разрешённого plant-запроса ИИ способен создать **предложение**, которое всё равно требует ручного подтверждения и отдельного шлюза.

Путь команды: `proposed` → ручное `approve` → `approved` → выдача авторизованному шлюзу через `/api/gateway/next` → `dispatched` → `/api/gateway/result` → `succeeded`/`failed`/`uncertain`. До выдачи возможен `cancelled`; истечение предложений/одобрений даёт `expired`, истечение уже выданной команды — `uncertain`. Выданная команда не повторяется автоматически. Только администратор может выполнить `reconcile` после сверки с контроллером и ввода основания.

Подтверждение и выдача снова проверяют флаг физического управления, показания не старше 15 секунд, автоматический режим контроллера, допустимый safety/status, блокировки SCADA и действующий ключ шлюза. Предложение живёт пять минут, одобрение и lease выдачи — по 30 секунд. Действия: `hold`, `resume`, `set_speed_percent` (1–100). `speed_percent` требуется только последнему действию. Поставка не заменяет наладку драйвера конкретного PLC и межблокировки оборудования.

## 7. Импорт, качество, сценарии и учебный стенд

`POST /api/import/preview` проверяет файл без применения, `/api/import` сохраняет поддержанный импорт. CSV до 5 МиБ и до 20 000 строк; UTF-8, включая BOM. Шаблоны отдаёт сама установка: `/api/import/template`, `/api/import/robots-template`, `/api/quality/template`, `/api/scada/template`. Используйте соответствующий шаблон и предварительную проверку, чтобы не перепутать типы данных.

Качество через `/api/quality/records`: `records` 1–1000, каждая запись `timestamp`, `record_id`, `brand`, `model`, `color`, `quantity` (1–1 000 000), `rejected` (0 … quantity). Это отдельные записи контроля качества, не датчики и не автоматически синхронизированный производственный план.

Модель линии: `/api/control`, `/api/advance`, `/api/stations/{station_id}`, `/api/scenarios`. Выбор главного источника `/api/source` принимает `source=simulation` либо `source=telemetry` с `telemetry_kind=production|robots`. Анализ ИИ отдельно принимает свой `data_source`; выбранная панель не подменяет источник AI-запроса.

Учебный стенд `/api/emulation` имеет независимый snapshot и ревизию. Команда `/api/emulation/commands` требует `request_id` (16–100 допустимых символов) и `expected_revision`. Сервер проверяет повторы по request_id и конфликты по revision. Возврат `409` означает необходимость перечитать стенд. `pause`, `play`, `advance`, учебные faults, ТО, склад и уведомления меняют только стенд. `/api/emulation/assistant` — встроенный помощник стенда по правилам; это отдельный endpoint от генеративного `/api/ai/analyze`.

## 8. Ошибки и ограничения

| HTTP | Значение и действие клиента |
|---|---|
| 400 | Неподдержанное/некорректное действие конкретного обработчика; прочитайте `detail` |
| 401 | Нет действительной сессии/ключа; повторный вход или замена ключа |
| 403 | Недостаточная роль, CSRF/Origin, временный пароль, область ключа; не повторять бесконечно |
| 404 | Нет робота/записи/узла/команды; проверьте идентификатор и источник |
| 409 | Конфликт версии/идемпотентности/состояния, запрет управления; перечитайте состояние |
| 422 | Ошибка полей/CSV/бизнес-валидации; исправьте данные |
| 429 | Rate limit/заняты слоты; выдержите `Retry-After`, если есть |
| 500 | Внутренняя ошибка; сохраните `X-Request-ID`/`request_id`, проверьте логи |
| 502 | Ответ модели не соответствует схеме; показывайте факты и советы по правилам |
| 503 | Недоступна БД/локальная модель; проверяйте health/logs; пакет измерений повторяйте с прежним ключом |

Ошибки обычно имеют `{ "detail": "…" }`. Валидация Pydantic возвращает `detail` со списком `loc`, `msg`, `type`; CSV — список ошибок строк. Авторизация/лимиты срабатывают до обработчика, поэтому часть этих ответов не перечислена автоматически у каждой операции в OpenAPI.

Login ограничен по пользователю и IP; пользовательские записи — 600 за окно 60 секунд; AI — 10 за 300 секунд на пользователя плюс один модельный слот. Четыре слота тяжёлых импортов/расчётов предотвращают параллельную перегрузку. Это встроенные ограничения одной API-установки, не распределённый лимитер. Сервер рассчитан на один процесс-владелец состояния; горизонтальное размножение API требует отдельного проектирования.

## 9. Полный список операций

Таблица ниже построена из поставляемого `openapi.json`; `summary` — имя операции из кода. Модель тела указана ссылкой на `components.schemas`. Query/path-параметры, обязательность и точные типы смотрите в JSON для конкретного пути. Отдельно доступны защищённые `GET /api/docs` и `GET /api/openapi.json`, не включённые в список прикладных операций.

| Метод | Путь | Операция | Тело | Параметры |
|---|---|---|---|---|
| GET | `/api/admin/audit` | Audit Log | — | `before` (query) |
| POST | `/api/admin/commands/{command_id}/reconcile` | Reconcile | `Reconciliation` | `command_id` (path, обязательно) |
| GET | `/api/admin/gateways` | Gateways | — | — |
| POST | `/api/admin/gateways` | Create Gateway | `GatewayCreate` | — |
| DELETE | `/api/admin/gateways/{key}` | Revoke Gateway | — | `key` (path, обязательно) |
| GET | `/api/admin/system` | System Status | — | — |
| GET | `/api/admin/tokens` | Tokens | — | — |
| POST | `/api/admin/tokens` | New Token | `NewToken` | — |
| DELETE | `/api/admin/tokens/{token_id}` | Revoke Token | — | `token_id` (path, обязательно) |
| GET | `/api/admin/users` | Users | — | — |
| POST | `/api/admin/users` | New User | `NewUser` | — |
| PATCH | `/api/admin/users/{user_id}` | Edit User | `EditUser` | `user_id` (path, обязательно) |
| POST | `/api/advance` | Advance | `Advance` | — |
| POST | `/api/ai/analyze` | Analyze | `AIQuestion` | — |
| GET | `/api/ai/context` | Context Preview | — | `data_source` (query), `robot_id` (query) |
| GET | `/api/ai/status` | Status | — | — |
| POST | `/api/auth/login` | Login | `Login` | — |
| POST | `/api/auth/logout` | Logout | — | — |
| GET | `/api/auth/me` | Me | — | — |
| POST | `/api/auth/password` | Change Password | `PasswordChange` | — |
| GET | `/api/automation` | Automation | — | — |
| POST | `/api/automation/commands` | Command | `CommandBody` | — |
| POST | `/api/automation/commands/{command_id}/approve` | Approve | `Approval` | `command_id` (path, обязательно) |
| POST | `/api/automation/commands/{command_id}/cancel` | Cancel | — | `command_id` (path, обязательно) |
| POST | `/api/control` | Control | `Control` | — |
| GET | `/api/dashboard` | Dashboard | — | — |
| GET | `/api/emulation` | Snapshot | — | — |
| POST | `/api/emulation/assistant` | Assistant | `Question` | — |
| POST | `/api/emulation/commands` | Command | `Command` | — |
| GET | `/api/emulation/components/{identity}` | Component | — | `identity` (path, обязательно) |
| GET | `/api/events` | Events | — | — |
| GET | `/api/export` | Export | — | — |
| POST | `/api/gateway/next` | Next Command | — | — |
| POST | `/api/gateway/result` | Command Result | `CommandResult` | — |
| GET | `/api/health` | Health | — | — |
| GET | `/api/history` | History | — | `limit` (query), `run_id` (query) |
| POST | `/api/import` | Import Csv | `Body_import_csv_api_import_post` | — |
| POST | `/api/import/preview` | Preview Csv | `Body_preview_csv_api_import_preview_post` | — |
| GET | `/api/import/robots-template` | Robot Template | — | — |
| GET | `/api/import/template` | Import Template | — | — |
| GET | `/api/imports` | Imports | — | — |
| GET | `/api/incidents` | Incidents | — | `include_archived` (query) |
| POST | `/api/incidents/{incident_id}/acknowledge` | Acknowledge | — | `incident_id` (path, обязательно) |
| GET | `/api/incidents/export` | Incident Export | — | — |
| GET | `/api/manufacturing` | Manufacturing | — | `search` (query), `offset` (query) |
| GET | `/api/monitoring` | Monitoring | — | — |
| POST | `/api/monitoring/{identifier}/acknowledge` | Acknowledge | — | `identifier` (path, обязательно) |
| GET | `/api/quality/export` | Quality Export | — | — |
| POST | `/api/quality/records` | Quality Records | `QualityBatch` | — |
| GET | `/api/quality/template` | Quality Template | — | — |
| POST | `/api/reset` | Reset | `Reset` | — |
| GET | `/api/robots/analytics` | Robot Analytics | — | `robot_id` (query, обязательно), `start` (query), `end` (query) |
| GET | `/api/robots/config` | Robot Config | — | `robot_id` (query, обязательно) |
| PUT | `/api/robots/config` | Configure Robot | `RobotConfig` | `robot_id` (query, обязательно) |
| GET | `/api/robots/detail` | Robot Detail | — | `robot_id` (query, обязательно), `start` (query), `end` (query), `offset` (query) |
| GET | `/api/robots/export` | Robot Export | — | `robot_id` (query), `start` (query), `end` (query) |
| GET | `/api/robots/history` | Robot History | — | `robot_id` (query, обязательно), `limit` (query), `start` (query), `end` (query), `offset` (query) |
| POST | `/api/robots/measurements` | Ingest Measurements | `MeasurementBatch` | — |
| GET | `/api/scada` | Overview | — | `operation` (query) |
| POST | `/api/scada/alarms/{alarm_id}/acknowledge` | Acknowledge | — | `alarm_id` (path, обязательно) |
| POST | `/api/scada/assets` | Create Asset | `AssetBody` | — |
| PUT | `/api/scada/assets/{asset_id}/operation` | Update Asset Operation | `AssetOperationUpdate` | `asset_id` (path, обязательно) |
| POST | `/api/scada/assets/{asset_id}/template` | Asset Template | — | `asset_id` (path, обязательно) |
| PUT | `/api/scada/calendar` | Configure Calendar | `CalendarBody` | — |
| POST | `/api/scada/components` | Create Component | `ComponentBody` | — |
| PUT | `/api/scada/components/{component_id}` | Update Component | `ComponentUpdate` | `component_id` (path, обязательно) |
| GET | `/api/scada/export` | Export Readings | — | `component_id` (query, обязательно) |
| GET | `/api/scada/history` | History | — | `component_id` (query, обязательно), `offset` (query), `limit` (query) |
| POST | `/api/scada/import` | Import Csv | `Body_import_csv_api_scada_import_post` | — |
| GET | `/api/scada/journal` | Journal | — | `kind` (query), `offset` (query), `operation` (query) |
| POST | `/api/scada/offers` | Create Offer | `OfferBody` | — |
| POST | `/api/scada/orders` | Create Order | `OrderBody` | — |
| POST | `/api/scada/orders/{order_id}/cancel` | Cancel Order | — | `order_id` (path, обязательно) |
| POST | `/api/scada/orders/{order_id}/confirm` | Confirm Order | `OrderConfirm` | `order_id` (path, обязательно) |
| POST | `/api/scada/orders/{order_id}/receive` | Receive Order | `ReceiveBody` | `order_id` (path, обязательно) |
| POST | `/api/scada/parts` | Create Part | `PartBody` | — |
| POST | `/api/scada/readings` | Ingest | `ReadingBatch` | — |
| POST | `/api/scada/stock` | Adjust Stock | `StockBody` | — |
| POST | `/api/scada/tasks` | Create Task | `TaskBody` | — |
| POST | `/api/scada/tasks/{task_id}/cancel` | Cancel Task | — | `task_id` (path, обязательно) |
| POST | `/api/scada/tasks/{task_id}/complete` | Complete Task | `TaskComplete` | `task_id` (path, обязательно) |
| GET | `/api/scada/template` | Csv Template | — | — |
| GET | `/api/scenarios` | Scenarios | — | — |
| POST | `/api/scenarios` | Scenario | `ScenarioRequest` | — |
| POST | `/api/source` | Source | `Source` | — |
| GET | `/api/state` | State | — | — |
| PATCH | `/api/stations/{station_id}` | Station | `StationChange` | `station_id` (path, обязательно) |

## 10. Обновление спецификации и проверка поставки

Для разработчика: после изменения маршрутов/схем переснимите OpenAPI из текущего кода. В уже подготовленном окружении backend достаточно следующего Python-кода; lifespan не запускается и база не создаётся:

```python
import json
from pathlib import Path
from app.main import create_app
spec = create_app(database_url="sqlite:///:memory:", runner_enabled=False).openapi()
Path("openapi.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
```

`ops/release_check.py` предназначен только для специально выделенной тестовой установки `allur-release-check`, на `http://127.0.0.1:8094`, с marker-файлом `.release-check-only`, содержащим `ALLUR_RELEASE_CHECK_ONLY`. Он меняет временный пароль и записывает два тестовых измерения, поэтому не используется на рабочей базе. Требует Python 3.12+ со стандартной библиотекой. Пароль читает из файла и не печатает. Новый тестовый пароль сохраняется в `.release-check-password` внутри QA-копии.

```sh
python ops/release_check.py --url http://127.0.0.1:8094 \
  --installation-root /path/to/isolated-copy \
  --password-file /path/to/isolated-copy/.secrets/bootstrap_password --require-ai
```

Проверка включает реальную авторизацию, пустоту цехового источника, данные двух учебных источников, приём через ограниченный интеграционный ключ, идемпотентный повтор, фоновое критическое событие, принятие сотрудником и восстановление по свежим данным. `--inference` дополнительно вызывает модель. После перезапуска или восстановления тестовой базы `--verify-existing` с `--password-file .../.release-check-password` проверяет сохранность двух записей, границ и истории, не добавляя измерений. Скрипт не делает backup/restore сам; передавайте это штатным скриптам отдельной тестовой Compose-установки.
