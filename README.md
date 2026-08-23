# Superlog-lite

Лёгкий мониторинг локального LLM-сервера (`ik_llama.cpp`, порт `8083`) по паттерну **Fingerprint → Memory → Agent Run**: система распознаёт проблему по «отпечатку», запоминает её в SQLite и при критических сбоях автоматически перезапускает сервер (с защитой от шторма рестартов).

> Назначение: сторожевой таймер для локального инференса. Проверяет доступность сервера, меряет реальную скорость генерации, классифицирует деградации и чинит то, что может починить сам.

---

## Возможности

- **Проверка живости** — `/v1/models`; если сервер молчит → инцидент `server_unreachable` (critical)
- **Реальный тест генерации** — запрос на 100 токенов → `tok/s`, `latency`
- **Классификация деградаций:**
  - ⚠️ `low_throughput` — скорость ниже порога (по умолчанию 10 tok/s)
  - ⚠️ `high_latency` — задержка выше порога (по умолчанию 30 c); срабатывает **независимо** от low_throughput
  - 🛑 `generation_error` — ошибка генерации (critical)
  - 🛑 `server_unreachable` — сервер недоступен (critical)
- **Память инцидентов (SQLite WAL)** — каждый уникальный тип проблемы хранится с счётчиком повторов `run_count`, временем первого/последнего появления
- **Бакетированные отпечатки** — `tok_s=8.2` и `tok_s=8.3` дают **один и тот же** fingerprint (без фрагментации памяти)
- **Авто-перезапуск с cooldown** — при critical-инциденте запускает `.bat` рестарта через `cmd /c`, ждёт подъёма до 120 с, защищён cooldown 600 с от циклов рестартов
- **Демо-режим** — `demo_incident.py` показывает жизненный цикл в отдельной БД, не трогая боевую

## Как это работает

```
python monitor.py
      │
      ▼
check_server()
   ├─ GET /v1/models            → ok?
   └─ POST /chat/completions    → tok_s, latency_s
      │
      ▼
classify_incident(checks)
   ├─ server_unreachable?  → critical
   ├─ generation_error?    → critical
   ├─ tok_s < порог?       → warning: low_throughput
   └─ latency > порог?     → warning: high_latency
      │
      ▼
store_incident(inc)          → INSERT или UPDATE run_count+1 (ON CONFLICT)
      │
      ▼ (только critical)
auto_fix(inc)
   ├─ cooldown не истёк?  → skip
   └─ cmd /c restart.bat   → ждать /models до 120 c
      │
      ▼
INCIDENT MEMORY (SELECT из SQLite)
```

### Паттерн Fingerprint → Memory → Agent Run

1. **Fingerprint** — `sha256(error_type | bucket)[:16]`:
   - `low_throughput` → bucket `"low"`, `high_latency` → bucket `"high"` (числа не дробят отпечаток);
   - `generation_error` / `server_unreachable` → нормализованный текст ошибки (цифры заменяются на `N`, обрезка 200 символов).
2. **Memory** — SQLite (`incidents.db`, WAL, `timeout=5`): таблицы `incidents` (уникальные проблемки) и `agent_runs` (журнал проверок).
3. **Agent Run** — при повторе инцидента `run_count+1`, предыдущие `findings` подгружаются из памяти; для critical вызывается авто-фикс.

## Установка

Требуется Python 3.11+.

```bat
git clone https://github.com/L-MORIA/superlog-lite.git
cd superlog-lite
pip install -r requirements.txt
python monitor.py --help
```

Зависимости: стандартная библиотека для мониторинга; `Pillow` — только для генерации иконки; `pytest`/`ruff` — для разработки.

## Быстрый старт

```bat
:: Разовая проверка
python monitor.py

:: Только мониторинг, без авто-рестарта
python monitor.py --no-auto-fix

:: Кастомные пороги и БД
python monitor.py --threshold-tok 20 --threshold-latency 25 --db .\my.db

:: Через лаунчер Windows
monitor_8083.bat
monitor_8083.bat --no-auto-fix

:: Демо жизненного цикла (отдельная БД)
python demo_incident.py
python demo_incident.py --db .\custom_demo.db
```

Пример вывода:

```
============================================================
SUPERLOG-LITE: ik_llama:8083 Monitoring Demo
============================================================

Timestamp: 2026-08-23T12:43:00+00:00

CHECKS:
  models: {'ok': True, 'count': 1, 'ids': ['Qwen3_8-Opus-4_7-MTP-Q3KM-hybrid']}
  generation: tok_s=25.9, latency=3.92s, tokens=100

NO INCIDENTS — server healthy
============================================================
```

## Конфигурация

Все параметры задаются переменными окружения; CLI-флаги переопределяют их:

| Env | CLI-флаг | По умолчанию | Смысл |
|-----|----------|--------------|-------|
| `SUPERLOG_BASE` | — | `http://localhost:8083/v1` | Адрес API сервера |
| `SUPERLOG_TOK_S_THRESHOLD` | `--threshold-tok` | `10` | Порог tok/s для `low_throughput` |
| `SUPERLOG_LATENCY_THRESHOLD` | `--threshold-latency` | `30` | Порог секунд для `high_latency` |
| `SUPERLOG_RESTART_COOLDOWN` | — | `600` | Cooldown между рестартами, c |
| `SUPERLOG_RESTART_BAT` | — | `<родитель>/barozp-opus-8083/run_barozp_8083_mtp.bat` | Скрипт рестарта |
| `SUPERLOG_RESTART_CWD` | — | каталог `RESTART_BAT` | Рабочий каталог рестарта |

Пример:

```bat
set SUPERLOG_BASE=http://localhost:8083/v1
set SUPERLOG_TOK_S_THRESHOLD=20
set SUPERLOG_RESTART_BAT=D:\servers\restart_llm.bat
python monitor.py
```

## История инцидентов

Всё пишется в `incidents.db` (SQLite). Посмотреть:

```sql
-- Последние проблемки
SELECT fingerprint, error_type, run_count, first_seen, last_seen
FROM incidents ORDER BY last_seen DESC LIMIT 10;

-- Все проверки одной проблемки
SELECT started_at, ended_at, status, actions_json
FROM agent_runs WHERE incident_id=? ORDER BY started_at;
```

Схема: `incidents(id=fingerprint UNIQUE, error_type, top_frame, first_seen, last_seen, run_count, findings, resolution)`; `agent_runs(incident_id → incidents.fingerprint, started_at, ended_at, status, actions_json)`.

## Тесты и качество

```bat
ruff check .
python -m py_compile monitor.py demo_incident.py make_icon.py
pytest tests -q
```

Текущее состояние: **42 passed**, ruff — 0 ошибок.

## Структура проекта

| Файл | Назначение |
|------|------------|
| `monitor.py` | Основной мониторинг: проверка, классификация, память, авто-фикс |
| `demo_incident.py` | Демо жизненного цикла в `demo_incidents.db` |
| `make_icon.py` | Генерация `superlog_lite_icon.{png,ico}` |
| `monitor_8083.bat` | Windows-лаунчер (`%~dp0`, без хардкода диска) |
| `tests/` | 42 теста (классификация, fingerprint, БД, auto-fix, security-scan) |
| `docs/USER_GUIDE.md` | Руководство пользователя |
| `docs/AGENT_INSTRUCTIONS.md` | Инструкции для ИИ-агентов |
| `docs/ARCHITECTURE.md` | Архитектура и схема данных |
| `audit/` | Аудиты: AUDIT-1 (15🔴), AUDIT-2, VERIFY-1 |

## Известные ограничения

- Авто-фикс Windows-only (`cmd /c`, `CREATE_NEW_CONSOLE`); на Linux вернёт `failed`
- Нет веб-интерфейса — только CLI + SQLite
- `usage.completion_tokens==0` → оценка токенов по длине текста (`len // 4`)
- `/models` используется вместо `/health` (у ik_llama.cpp нет `/v1/health`)

## Лицензия

Личное использование. См. профиль [L-MORIA](https://github.com/L-MORIA).
