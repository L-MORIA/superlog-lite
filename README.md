# Superlog-lite

Лёгкий мониторинг локального LLM `ik_llama` (порт `8083`) по паттерну **Fingerprint → Memory → Agent Run**.

* **Fingerprint:** `hash(error_type, bucket)` → `fingerprint[:16]` (бакетировано, не точное значение)
* **Memory:** SQLite (`incidents.db`, WAL, `ON CONFLICT`) — `incidents` + `agent_runs`
* **Recurrence:** повтор того же `fingerprint` → `run_count+1`, `prior_findings` подгружаются

## Файлы

| Файл | Назначение |
|------|------------|
| `monitor.py` | Основной мониторинг: `/v1/models` + генерация 100 токенов → `tok/s`/`latency` → `classify` → `store` → `auto_fix` |
| `demo_incident.py` | Демо жизненного цикла (пишет в `demo_incidents.db`, не трогает боевой `incidents.db`) |
| `make_icon.py` | Генерация `superlog_lite_icon.{png,ico}` |
| `monitor_8083.bat` | Лаунчер для Windows (использует `%~dp0`, не хардкод `F:`) |

## Установка

```bat
pip install -r requirements.txt
python monitor.py --help
```

## Запуск

```bat
:: Разовая проверка (дефолт: 10 tok/s, 30s latency, cooldown 600s)
python monitor.py

:: Таймаут пробы генерации (по умолчанию 120s)
python monitor.py --gen-timeout 60

:: Без авто-рестарта
python monitor.py --no-auto-fix

:: Кастомные пороги / БД
python monitor.py --threshold-tok 20 --threshold-latency 25 --db ./my.db

:: Env-переменные (альтернатива флагам)
set SUPERLOG_BASE=http://localhost:8083/v1
set SUPERLOG_RESTART_BAT=F:\barozp-opus-8083\run_barozp_8083_mtp.bat
set SUPERLOG_RESTART_COOLDOWN=600
python monitor.py
```

Через `.bat`:
```bat
monitor_8083.bat
monitor_8083.bat --no-auto-fix
```

Демо:
```bat
python demo_incident.py
python demo_incident.py --db ./custom_demo.db
```

## Пороговые значения

* `TOK_S_THRESHOLD=10` (`SUPERLOG_TOK_S_THRESHOLD`)
* `LATENCY_THRESHOLD=30` (`SUPERLOG_LATENCY_THRESHOLD`)
* `RESTART_COOLDOWN_S=600` (`SUPERLOG_RESTART_COOLDOWN`) — защита от цикла рестартов
* `GEN_TIMEOUT=120` (`SUPERLOG_GEN_TIMEOUT`) — таймаут пробы генерации

## Классификация инцидентов

| Тип | Серьёзность | Смысл |
|-----|-------------|-------|
| `server_unreachable` | critical | `/v1/models` не отвечает |
| `generation_error` | critical | проба генерации упала (таймаут, HTTP-ошибка) |
| `server_busy` | warning | слот занят (`/health`: `no slot available`) — проба пропущена, рестарт НЕ выполняется |
| `low_throughput` | warning | tok/s ниже порога |
| `high_latency` | warning | латентность выше порога |

## Тесты

```bat
ruff check .
python -m py_compile monitor.py demo_incident.py make_icon.py
pytest tests -q
```

## Аудит

* `audit/AUDIT-1.md` — полный аудит 22.08.2026 (15×🔴, 16×🟡)
* `PLAN_FIX.md` — план устранения

## Иконка

```bat
python make_icon.py
```
