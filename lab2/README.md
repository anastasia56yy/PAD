# Лабораторная работа №2. Микросервисное приложение «UTM Assistant»

## 1. Описание проекта

RAG-ассистент из лабораторной №1, разделённый на **7 независимых backend-сервисов** + API Gateway.
Студент регистрируется, задаёт вопросы об UTM/FCIM, получает ответы со ссылками на документы, видит историю своих
вопросов и уведомления. Администратор запускает сбор документов с сайтов, после чего поиск переиндексирует базу.

## 2. Архитектура

```text
                    Браузер (веб-интерфейс)
                              │  HTTP :8080
                       ┌──────▼───────┐
                       │ API Gateway  │  маршрутизация, JWT, роли, rate limit, request-id, агрегация /api/status
                       └──────┬───────┘
  ┌───────────┬───────────┬───┴────────┬────────────┬─────────────┬───────────────┐
  ▼           ▼           ▼            ▼            ▼             ▼               ▼
auth        user       answer  REST  search      history      ingestion     notification
service     service    service ────▶ service     service      service       service
  │           │           │           │            │             │               │
auth_db    user_db    (stateless)  Chroma       history_db   manifest +     notification_db
  │           │           │        (индекс)        ▲         документы           ▲
  │           │           ▼                        │             │               │
  │           │        Ollama (LLM на хосте)       │             │               │
  └──publish──┴───────────┴──────── RabbitMQ (exchange utm.events, topic) ───────┘
```

| Сервис | Ответственность | Данные | Публикует | Слушает |
| --- | --- | --- | --- | --- |
| **auth-service** | регистрация, вход, выдача JWT, роли | auth_db (логины, хеши паролей) | `user.registered` | — |
| **user-service** | профили: имя, факультет, язык ответов | user_db | — | `user.registered` |
| **search-service** | поиск: векторы, фильтры, reranker, соседние фрагменты | Chroma-индекс | — | `documents.updated` |
| **answer-service** | генерация ответа через Ollama, промпт, перевод, проверка ссылок | нет (stateless) | `question.answered` | — |
| **history-service** | история вопросов студента, статистика | history_db | — | `question.answered` |
| **ingestion-service** | сбор документов с utm.md / fcim.utm.md, очистка, экспорт корпуса | manifest (SQLite) + документы | `documents.updated` | — |
| **notification-service** | уведомления в приложении | notification_db | — | все три события |
| API Gateway | единая точка входа (не бизнес-сервис) | — | — | — |

### Почему именно так разделено

* **auth отдельно от user**: учётные данные (хеши паролей) изолированы от профиля; сервис профилей не может их прочитать,
  а утечка профилей не раскрывает пароли.
* **search отдельно от answer**: поиск держит в памяти embedding-модель и reranker и работает за доли секунды, генерация
  ждёт LLM секунды. Их можно масштабировать независимо (в ЛР3 — больше реплик answer-service); answer не хранит
  состояния, поэтому реплики взаимозаменяемы.
* **ingestion отдельно от search**: сбор документов — долгая фоновая задача с сетью и PDF; она не должна мешать поиску.
  Индекс принадлежит только search-service: ingestion лишь сообщает событием, что корпус изменился, и отдаёт его по API.
* **history и notification на событиях**: ответ пользователю не ждёт записи истории и уведомлений. Если history-service
  упал, вопросы продолжают обслуживаться, а события ждут в его очереди RabbitMQ и обработаются после перезапуска.

### Базы данных

Database-per-service: у каждого сервиса **своя база и свой логин**, который имеет доступ только к ней
(`infrastructure/postgres/init-databases.sh`). Физически четыре базы находятся на одном сервере PostgreSQL — это
осознанный компромисс для ноутбука (один процесс СУБД вместо четырёх); изоляция обеспечивается правами, и перенос
любой базы на отдельный сервер меняет только `DATABASE_URL`. search-service и ingestion-service хранят данные
в своих томах (`data/search`, `data/ingestion`) и не используют PostgreSQL: векторному индексу нужна Chroma,
манифесту grabber'а — SQLite из ЛР1.

### Взаимодействие

* **Синхронно (REST)**: gateway → сервисы; answer-service → search-service (`POST /search`); search-service →
  ingestion-service (`GET /documents/export`). Таймауты, повтор при 502/503/504, понятные ошибки 503/504.
* **Асинхронно (RabbitMQ, topic exchange `utm.events`)**: у каждого потребителя своя durable-очередь,
  сообщения persistent, подтверждение после обработки (at-least-once). Обработчики идемпотентны: повторная доставка
  события не создаёт дубликатов (проверка `event_id` / первичного ключа).

| Событие | Кто публикует | Кто реагирует и как |
| --- | --- | --- |
| `user.registered` | auth | user — создаёт профиль; notification — приветствие |
| `question.answered` | answer | history — сохраняет вопрос; notification — если ответа не нашлось, советует уточнить |
| `documents.updated` | ingestion | search — скачивает корпус и переиндексирует; notification — оповещает всех |

## 3. Технологии

| Что | Выбор | Почему |
| --- | --- | --- |
| Сервисы | Python 3.12 + FastAPI | тот же язык, что и RAG из ЛР1 (код переиспользуется без переписывания); OpenAPI/Swagger генерируется автоматически |
| БД | PostgreSQL 16 | надёжная реляционная СУБД, отдельная база на сервис |
| ORM | SQLAlchemy 2 | одна модель данных для PostgreSQL в работе и SQLite в тестах |
| Брокер | RabbitMQ 3.13 | очереди на каждого потребителя, подтверждения, переживает перезапуск потребителя; Kafka избыточна для такого объёма событий |
| Аутентификация | JWT (HS256), пароли — scrypt | stateless-токены проверяются в gateway без похода в auth-service |
| Векторный поиск, LLM | Chroma, sentence-transformers, Ollama (qwen2.5:3b) | из ЛР1 |
| Запуск | Docker Compose | вся система одной командой |

## 4. Установка и запуск

Нужны Docker Desktop и Ollama с моделью `qwen2.5:3b` (Ollama работает на хосте и использует GPU).

```bash
cd lab2
cp .env.example .env            # и заменить все значения (пароли, JWT_SECRET)
# начальные данные из ЛР1, чтобы не ждать первой индексации (иначе запустить сбор через API):
#   lab1/data/index + processed/documents.jsonl  ->  lab2/data/search/
#   lab1/data/raw, processed, manifest.sqlite    ->  lab2/data/ingestion/
docker compose --env-file .env up --build
```

* веб-интерфейс: <http://localhost:8080>
* Swagger UI gateway: <http://localhost:8080/docs>
* RabbitMQ (очереди, события): <http://localhost:15672>
* состояние всех сервисов: <http://localhost:8080/api/status>

## 5. Configuration

Всё через переменные окружения (12-factor), секреты — только в `.env` (в `.gitignore`), шаблон — `.env.example`.

| Переменная | Где | Назначение |
| --- | --- | --- |
| `DATABASE_URL` | auth, user, history, notification | своя база и логин сервиса |
| `RABBITMQ_URL` | все | брокер событий |
| `JWT_SECRET`, `JWT_TTL_SECONDS` | auth, gateway | подпись и срок жизни токенов |
| `ADMIN_EMAILS` | auth | кто получает роль admin при регистрации |
| `SEARCH_URL`, `INGESTION_URL`, `*_URL` | answer, search, gateway | адреса сервисов |
| `OLLAMA_HOST`, `OLLAMA_MODEL`, `LLM_TIMEOUT_SECONDS` | answer | LLM |
| `RATE_LIMIT_ASK_PER_MIN`, `RATE_LIMIT_AUTH_PER_MIN` | gateway | ограничение частоты |
| `LOG_LEVEL` | все | уровень логов |

Настройки RAG-pipeline (размер chunk, пороги, промпт) — те же, что подобраны экспериментами в ЛР1
(`lab1/configs/default.yaml` копируется в образ).

## 6. API

Полная документация — Swagger UI: gateway `/docs`, у каждого сервиса свой `/docs` внутри сети.

| Метод и путь (gateway) | Доступ | Сервис |
| --- | --- | --- |
| `POST /api/auth/register` | все | auth |
| `POST /api/auth/login` | все | auth |
| `GET /api/auth/me` | вошедшие | auth |
| `GET /api/users/me`, `PATCH /api/users/me` | вошедшие | user |
| `POST /api/ask` | все (вошедшим сохраняется история) | answer → search |
| `GET /api/info` | все | search |
| `GET /api/history`, `GET /api/history/stats`, `DELETE /api/history/{id}` | вошедшие | history |
| `GET /api/notifications`, `POST /api/notifications/{id}/read` | вошедшие | notification |
| `POST /api/ingest/run`, `GET /api/ingest/status` | admin | ingestion |
| `GET /api/status` | все | агрегирует `/ready` всех сервисов |
| `GET /health`, `GET /ready` | у каждого сервиса | liveness / readiness |

## 7. Авторизация

1. auth-service выдаёт JWT (`sub`, `email`, `role`, `exp`); пароль хранится как scrypt-хеш с солью.
2. Gateway проверяет подпись и срок токена **один раз** и передаёт сервисам `X-User-Id` / `X-User-Role`.
3. Заголовки `X-User-*` от клиента **всегда отбрасываются** — подделать личность нельзя (покрыто тестом).
4. Сервисы доступны только во внутренней сети compose (наружу открыт лишь gateway :8080 и консоль RabbitMQ).
5. Роли: `student` и `admin` (сбор документов). Доступ к чужому профилю, истории, уведомлениям — 403/404.

## 8. Ошибки и логи

* Единый формат ошибки: `{"error": {"code", "message", "request_id", "details?"}}`; ошибки валидации —
  по полям; stack trace никогда не уходит клиенту, только в лог.
* Недоступный или медленный сервис → 503 `upstream_unavailable` / 504 `upstream_timeout`; Ollama — 503 `llm_unavailable`.
* Логи — JSON в stdout (`docker compose logs <service>`):

```json
{"timestamp": "2026-11-06T10:15:02.114+00:00", "service": "answer-service", "level": "INFO",
 "request_id": "9f2c41d0a7b3e811", "logger": "answer", "message": "Question answered",
 "refused": false, "context": 4, "latency_s": 5.21}
```

`request_id` создаётся в gateway, передаётся в заголовке `X-Request-ID` во все сервисы и **внутри событий RabbitMQ**,
поэтому один вопрос пользователя прослеживается через gateway → answer → search → history → notification:

```bash
docker compose logs | grep 9f2c41d0a7b3e811
```

## 9. Тестирование

```bash
cd lab2
pip install -r common/requirements.txt pytest
pytest tests
```

13 тестов без Docker (SQLite в памяти, RabbitMQ подменён): регистрация/вход/дубликат e-mail, формат ошибок валидации,
создание профиля из события и идемпотентность, история по пользователям и без дубликатов, уведомления «всем» с отдельной
отметкой прочтения для каждого, health/ready, gateway — подстановка личности из токена (а не из заголовков клиента),
401/403, rate limit 429, недоступный сервис → 503, хеширование паролей, формат JSON-логов.

## 10. Проверка всей системы (docker compose)

Сценарий, выполненный на запущенной системе через gateway (06.11.2026):

| Шаг | Результат |
| --- | --- |
| `GET /api/status` | все 7 сервисов `ready` |
| регистрация студента | 201, JWT; профиль появился в user-service через `user.registered` (без прямого вызова) |
| вопрос «Câte ore de muncă corespund unui credit ECTS?» | ответ «30 de ore» со ссылкой на Regulament ECTS, 2 источника |
| вопрос вне базы | отказ + похожие документы; notification-service создал уведомление «нет ответа» |
| история и статистика | 2 вопроса, 1 без ответа, среднее время 7,97 с — записано из `question.answered` |
| без токена / студент → `/api/ingest/run` / поддельный `X-User-Id` | 401 / 403 / 401 |
| регистрация с неверными полями | 422 `validation_error` по каждому полю |
| трассировка одного `request_id` | найден в логах gateway, answer, search, history, notification |
| сбор документов (admin) | 83 с: 888 документов (+12 новых на сайтах); событие `documents.updated` |
| переиндексация | search-service построил новый индекс (8 043 chunk, 591 с на CPU) **рядом со старым**; во время сборки вопросы обслуживались старым индексом, затем мгновенное переключение; уведомление «База обновлена» получили все |

### Найденные и исправленные проблемы

* **Пути в манифесте grabber'а.** Манифест, созданный на Windows, хранил `fcim\page\10104.json`; в Linux-контейнере
  это одно имя файла → сбор падал. Grabber теперь пишет POSIX-пути, а чтение нормализует старые записи (исправлено в коде ЛР1).
* **Переиндексация ломала поиск.** Пересборка в той же коллекции Chroma делала поиск недоступным на ~10 минут.
  Сделана blue-green-схема: новый индекс в отдельной папке, указатель `CURRENT_INDEX`, переключение под блокировкой.
* **Отметка «прочитано» у общих уведомлений** сначала была одна на всех; вынесена в таблицу `read_marks` (пользователь × уведомление).

## 11. Выводы

1. Разделение по **данным и нагрузке** оказалось естественнее разделения «по слоям»: поиск (модели в памяти, миллисекунды),
   генерация (ждёт LLM, секунды) и сбор документов (минуты, сеть) имеют разный профиль и масштабируются по-разному.
2. **События развязывают сервисы**: ответ пользователю не зависит от истории и уведомлений, а регистрация — от профилей.
   Цена — eventual consistency (профиль появляется через долю секунды после регистрации) и необходимость
   идемпотентных обработчиков, потому что RabbitMQ гарантирует доставку «хотя бы один раз».
3. **Сквозной request_id** окупился сразу: ошибку пути в манифесте и порядок обработки событий было видно по одной строке `grep`.
4. **Gateway — точка доверия**: личность проверяется один раз, а запрет клиентских `X-User-*` закрывает подмену пользователя.
   Ограничение: rate limit хранится в памяти gateway; при нескольких репликах (ЛР3) для общего лимита нужен Redis.
5. Ограничения: ingestion-service и search-service хранят данные в томах и не масштабируются горизонтально без
   общего хранилища; answer-service, gateway и сервисы на PostgreSQL — stateless и масштабируются свободно (это используется в ЛР3).
