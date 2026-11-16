# Лабораторная работа №3. Docker и Kubernetes для «Libraria»

## 1. Описание

Книжный интернет-магазин из **лабораторной №2** (8 микросервисов + API Gateway) контейнеризирован и развёрнут в
**Kubernetes (Minikube)**. Код сервисов не копируется: он остаётся в `lab2/`, а эта работа содержит всё, что нужно
для упаковки и развёртывания — Dockerfile каждого сервиса, Docker Compose, манифесты Kubernetes и скрипты.

```text
lab3/
├── docker/<service>/Dockerfile            — 9 Dockerfile (по одному на сервис)
├── docker/<service>/Dockerfile.dockerignore — свой .dockerignore у каждого
├── docker-compose.yml, config.env         — запуск собранных образов одной командой (порт 8090)
├── k8s/
│   ├── 00-namespace.yaml
│   ├── configmaps/shop-config.yaml        — настройки вне образов
│   ├── secrets/shop-secrets.example.yaml  — только шаблон; настоящий Secret создаётся из .env
│   ├── infrastructure/postgres.yaml       — StatefulSet + PVC + init-скрипт баз
│   ├── infrastructure/rabbitmq.yaml       — StatefulSet + PVC
│   ├── services/<service>/deployment.yaml + service.yaml   — 9 сервисов
│   ├── autoscaling/gateway-hpa.yaml       — автомасштабирование gateway по CPU
│   └── kustomization.yaml                 — kubectl apply -k
├── scripts/build-images.ps1               — сборка 9 образов (+ загрузка в Minikube)
└── scripts/deploy.ps1                     — Secret из .env + kubectl apply + ожидание готовности
```

## 2. Архитектура в Kubernetes

```text
 браузер ──► NodePort 30088 / port-forward ──► Service gateway ──► Deployment gateway (2 пода, HPA 2–5)
                                                     │  DNS-имена сервисов: http://order-service:8000 и т. д.
   ┌──────────────┬──────────────┬──────────────────┼─────────────┬──────────────┬─────────────┐
 auth (2)      user (1)      catalog (2)      inventory (1)    cart (1)     order (2)   payment (1)  notification (1)
   │              │              │                 │              │             │            │            │
   └──────────────┴──── Service postgres ─► StatefulSet postgres-0 (PVC 2Gi, 8 баз) ────────┴────────────┘
                        Service rabbitmq ─► StatefulSet rabbitmq-0 (PVC 1Gi)
            ConfigMap shop-config (envFrom) · Secret shop-secrets (secretKeyRef) · namespace libraria
```

* **Deployment** — у каждого из 9 сервисов: образ `libraria/<service>:1.0`, `replicas`, порт 8000, переменные из
  ConfigMap и Secret, `resources`, три пробы, стратегия RollingUpdate с `maxUnavailable: 0`
  (новый под должен стать готовым раньше, чем остановится старый).
* **Service** — ClusterIP у внутренних сервисов, **NodePort 30088** у gateway (единственная точка входа).
  Сервисы находят друг друга по DNS-имени Service, а не через `localhost`.
* **Stateless-сервисы — Deployment, хранилища — StatefulSet** с PersistentVolumeClaim: данные PostgreSQL и очереди
  RabbitMQ переживают перезапуск и пересоздание пода.
* Реплики: по 2 у gateway, auth, catalog, order (нагруженные и stateless), по 1 у остальных.

## 3. Docker

Каждый Dockerfile (`docker/order-service/Dockerfile` и т. д.):

| Требование | Как сделано |
| --- | --- |
| небольшой образ | `python:3.12-slim`, **multi-stage**: зависимости ставятся в отдельном этапе, в итоговый образ копируется только результат, без pip-кэша; итог — 292 MB |
| `.dockerignore` | у каждого сервиса свой `Dockerfile.dockerignore`: в сборку уходит только его код и общая библиотека — **14 KB контекста** вместо всего репозитория |
| кэширование слоёв | порядок: зависимости → общая библиотека → код сервиса; правка кода пересобирает только последний слой |
| нет секретов в образе | пароли и ключи только из окружения (Secret / `.env`), в образе их нет |
| не root | `USER 10001` (appuser); в Kubernetes дополнительно `runAsNonRoot`, `readOnlyRootFilesystem`, `drop: ALL` |
| CMD / ENTRYPOINT | `ENTRYPOINT ["uvicorn", "app.main:app"]`, параметры в `CMD` |
| проверка здоровья | `HEALTHCHECK` на `/health` |

```bash
docker build -f lab3/docker/order-service/Dockerfile -t libraria/order-service:1.0 .   # из корня репозитория
```

## 4. Docker Compose

`docker-compose.yml` поднимает все 9 образов + PostgreSQL + RabbitMQ одной командой, с отдельным именем проекта и
портом (8090), чтобы не пересекаться с ЛР2. Несекретные настройки — `config.env` (те же ключи, что в ConfigMap),
секреты — `.env`.

```bash
cd lab3
cp .env.example .env          # заменить значения
docker compose --env-file .env up -d --build      # http://localhost:8090
```

Проверено: все 8 сервисов `ready`, 30 книг с остатками.

## 5. ConfigMap и Secret

* **ConfigMap `shop-config`** — 19 ключей: уровень логов, адреса сервисов, хост и порт БД и брокера, администраторы,
  срок токена, лимиты частоты, задержка платёжного провайдера. Подключается целиком через `envFrom`.
* **Secret `shop-secrets`** — 14 ключей: пароли 8 баз и суперпользователя, логин/пароль RabbitMQ, `JWT_SECRET`.
  **В Git нет ни одного настоящего значения**: в репозитории только шаблон с `change-me`, а Secret создаётся из
  `lab3/.env` (в `.gitignore`):

```bash
kubectl -n libraria create secret generic shop-secrets --from-env-file=lab3/.env
```

* Строки подключения собираются в манифесте из обоих источников через подстановку Kubernetes, без хранения пароля в тексте:

```yaml
- {name: DB_PASSWORD, valueFrom: {secretKeyRef: {name: shop-secrets, key: ORDER_DB_PASSWORD}}}
- {name: DATABASE_URL, value: "postgresql+psycopg://order_svc:$(DB_PASSWORD)@$(DATABASE_HOST):$(DATABASE_PORT)/order_db"}
```

## 6. Health checks и ресурсы

| Проба | Путь | Зачем |
| --- | --- | --- |
| `startupProbe` | `/health`, до 120 с | даёт сервису время подняться, пока стартуют база и брокер; liveness не мешает запуску |
| `livenessProbe` | `/health` | процесс завис — Kubernetes перезапускает контейнер |
| `readinessProbe` | `/ready` (проверяет БД / брокер) | пока зависимость недоступна, Service не шлёт трафик на под |

У PostgreSQL — `pg_isready`, у RabbitMQ — `rabbitmq-diagnostics check_port_connectivity`.

Ресурсы сервисов: `requests: 100m CPU / 96Mi`, `limits: 500m CPU / 256Mi`; PostgreSQL и RabbitMQ —
`100m / 256Mi` и `1 CPU / 768Mi`. Значения взяты из измерения `kubectl top pods` на работающем кластере:

| Под | CPU в простое | Память |
| --- | ---: | ---: |
| бизнес-сервисы | 73–109m | 65–68 Mi |
| gateway | 68–72m | 41–42 Mi |
| postgres-0 | 54m | 64 Mi |
| rabbitmq-0 | 431m (старт) | 151 Mi |

Сначала `requests` был 50m — меньше реального потребления: в простое CPU тратят relay outbox (опрос раз в 0,3 с) и
потребители RabbitMQ. Kubernetes планирует поды по `requests`, поэтому значение поднято до 100m.
Память с запасом укладывается в лимит 256Mi.

## 7. Запуск в Minikube

```powershell
minikube start --driver=docker --cpus=4 --memory=5000 --addons=metrics-server
.\lab3\scripts\build-images.ps1 -Minikube     # 9 образов собираются и загружаются в Minikube (minikube image load)
.\lab3\scripts\deploy.ps1                     # namespace, Secret из .env, kubectl apply -k, ожидание готовности
kubectl -n libraria port-forward svc/gateway 8090:8000   # витрина: http://localhost:8090
```

Minikube здесь работает с runtime `containerd`, поэтому образы не собираются «внутри» через `docker-env`, а
собираются локально и загружаются командой `minikube image load` — реестр образов не нужен (`imagePullPolicy: IfNotPresent`).

## 8. Демонстрация (выполнено, 16.11.2026)

**Поды, сервисы, деплойменты** — 17 подов `Running`:

```text
$ kubectl -n libraria get deployments
NAME                   READY   UP-TO-DATE   AVAILABLE
auth-service           2/2     2            2
cart-service           1/1     1            1
catalog-service        2/2     2            2
gateway                2/2     2            2
inventory-service      1/1     1            1
notification-service   1/1     1            1
order-service          2/2     2            2
payment-service        1/1     1            1
user-service           1/1     1            1

$ kubectl -n libraria get services
gateway       NodePort    8000:30088/TCP        (остальные — ClusterIP 8000; postgres 5432; rabbitmq 5672, 15672)
```

**Работа API через Kubernetes** (port-forward к Service gateway):

| Запрос | Результат |
| --- | --- |
| `GET /api/status` | все 8 сервисов `ready` |
| `GET /api/books` | **ровно 30 книг**, у всех остатки — при **двух** репликах catalog каталог создан один раз |
| заказ, карта «успешно» | `paid`, остаток 22 → 21 |
| заказ, карта «отказ» | `cancelled · Платёж отклонён` |
| заказ, оплата при получении | `confirmed` |

**Масштабирование**:

```text
$ kubectl -n libraria scale deployment order-service --replicas=3
deployment "order-service" successfully rolled out
order-service-...-gqlth   1/1  Running
order-service-...-rpk7l   1/1  Running   7s     ← новая реплика готова через 7 секунд
order-service-...-rx2bx   1/1  Running
```

Три реплики order-service разбирают одну очередь саги RabbitMQ как конкурирующие потребители.
Для gateway настроен **HorizontalPodAutoscaler** (2–5 реплик при загрузке CPU > 70 %, metrics-server).

**Самовосстановление**:

```text
$ kubectl -n libraria delete pod order-service-...-gqlth
order-service-...-zjrsx   0/1  Running   2s     ← Kubernetes сразу создал замену
...через 7 секунд:
order-service-...-zjrsx   1/1  Running   7s     ← готова, снова 3 из 3
```

Ещё один реальный пример: при первом развёртывании сервисы стартовали раньше PostgreSQL и RabbitMQ, не прошли
startup-пробу и были **автоматически перезапущены** (RESTARTS = 1), после чего подключились. После этого startup-проба
увеличена до 120 секунд.

**Логи и трассировка** — один заказ по `request_id` во всех подах:

```text
$ kubectl -n libraria logs -l app.kubernetes.io/part-of=libraria --prefix | grep b45121cb966f4967
[pod/gateway-...-6d8md]               POST /api/orders
[pod/order-service-...-rx2bx]         POST /orders · Order created · Order status changed
[pod/cart-service-...-8klbb]          GET /cart · Cart emptied after order
[pod/catalog-service-...-kgj9b]       GET /books-batch
[pod/inventory-service-...-lgh5t]     Order reserved · Reservation sold
[pod/payment-service-...-ckmxr]       Payment processed
[pod/notification-service-...-bf8m4]  Notification created ×2
```

## 9. Что пришлось изменить в коде ЛР2 для нескольких реплик

В ЛР2 каждый сервис работал в одном экземпляре. В Kubernetes с двумя репликами обнаружились две проблемы:

* **Двойное заполнение каталога**: обе реплики catalog видят пустую базу и создают по 30 книг. Решение —
  `pg_advisory_xact_lock` в транзакции заполнения: вторая реплика ждёт и видит, что каталог уже есть.
* **Двойная отправка событий из outbox**: обе реплики читают одни и те же неотправленные строки. Решение —
  `SELECT … FOR UPDATE SKIP LOCKED`: каждую строку забирает одна реплика.

Логика ЛР2 не изменилась, все 22 теста ЛР2 проходят.

## 10. Ответы на вопросы защиты

* **Docker vs виртуальная машина.** VM виртуализирует железо и запускает целую ОС со своим ядром; контейнер —
  изолированный процесс на общем ядре хоста (namespaces + cgroups). Поэтому контейнер стартует за секунды и весит
  сотни мегабайт, а не гигабайты.
* **Зачем Kubernetes.** Он поддерживает заданное состояние: нужное число копий, перезапуск упавших, распределение
  трафика только на готовые поды, обновление без простоя, масштабирование. Docker Compose просто запускает контейнеры.
* **Deployment** описывает желаемое состояние stateless-приложения (образ, число реплик, пробы, ресурсы); его
  контроллер через ReplicaSet создаёт и поддерживает поды, выполняет rolling update.
* **Service** — постоянное DNS-имя и виртуальный IP для меняющегося набора подов; балансирует между готовыми репликами.
* **ConfigMap / Secret** — конфигурация и секреты отделены от образа: один образ работает в любой среде, а пароли
  не попадают в Git и в образ.
* **Удаление пода** — контроллер Deployment видит расхождение (2 из 3) и создаёт новый под; Service начинает слать на
  него трафик, когда проходит readiness-проба.
* **Как Kubernetes понимает, что сервис работает** — liveness (`/health`: процесс жив) и readiness (`/ready`:
  зависимости доступны); startup-проба защищает медленный старт.

## 11. Выводы

1. Подготовка в ЛР2 окупилась: health-эндпоинты, конфигурация через окружение и stateless-сервисы перенеслись в
   пробы, ConfigMap/Secret и Deployment без изменения логики.
2. Несколько реплик выявили то, чего не видно на одном экземпляре: гонку при начальном заполнении данных и двойную
   обработку outbox. Масштабирование требует, чтобы код был готов к конкурентной работе копий.
3. Kubernetes действительно сам восстанавливает систему — это видно и на ручном удалении пода, и на реальном
   перезапуске сервисов, стартовавших раньше базы.
4. Ограничения: одна нода Minikube; PostgreSQL и RabbitMQ в одном экземпляре (в production — управляемые сервисы
   или операторы с репликацией); rate limit gateway считается в каждой реплике отдельно.
