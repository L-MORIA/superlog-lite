# Архитектура: Superlog-lite

Документ описывает компоненты, паттерн Fingerprint → Memory → Agent Run, схему данных и порядок работы `superlog-lite`.

---

## 1. Общая схема

```
┌──────────────────────────────────────────────────────────────┐
│                     monitor.py (CLI)                          │
│                                                              │
│  ┌─────────┐    ┌────────────┐    ┌───────────────┐         │
│  │ check_  │───▶│ classify_  │───▶│ store_incident│        │
│  │ server()│    │ incident() │    │ (SQLite WAL)  │        │
│  └─────────┘    └────────────┘    └───────┬───────┘         │
│         │                  │             │                 │
│         ▼                  ▼             ▼                   │
│   /models (health)   tok_s/latency   incidents.db          │
│   /chat/completions   error bucket   run_count++            │
│                                                              │
│            ┌────────────────────────────────┐                │
│            │          auto_fix()             │                │
│            │  cooldown → cmd /c restart_bat   │                │
│            └────────────────────────────────┘                │
│                                                              │
│  CLI: --no-auto-fix --db --threshold-tok --threshold-lat    │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────┐     ┌──────────────────────┐
│  demo_incident.py     │     │  monitor_8083.bat     │
│  (demo_incidents.db)  │     │  %~dp0 launcher      │
└──────────────────────┘     └──────────────────────┘

┌──────────────────────┐     ┌──────────────────────┐
│  make_icon.py         │     │  incidents.db         │
│  (main() guard)       │     │  SQLite WAL          │
└──────────────────────┘     └──────────────────────┘
```

---

## 2. Паттерн: Fingerprint → Memory → Agent Run

Это цикл, при котором система помечает проблему, запоминает её и (для критических) пытается починить.

### Шаг 1 — Fingerprint (Отпечаток)
```python
fingerprint(error_type: str, top_frame: str = "") -> str
```
* `error_type`: `low_throughput` | `high_latency` | `generation_error` | `server_unreachable` | `server_busy`
* Бакетизуется (C-02): `low_throughput → bucket="low"`, `high_latency → bucket="high"`, `generation_error → _normalize_frame()` (удаление цифр).
* `sig = f"{error_type}|{bucket}"` → `sha256(sig)[:16]`

> Цель: `tok_s=8.2` и `tok_s=8.3` дают **один и тот же** `fingerprint`. Раньше — два разных (баг).

### Шаг 2 — Memory (Память)
SQLite-база `incidents.db` (WAL, `timeout=5.0`):
- Таблица **`incidents`** — уникальные проблемки (`fingerprint UNIQUE`).
- Таблица **`agent_runs`** — каждая проверка (время, статус, действия в JSON).
- `store_incident()` → `INSERT` / `except IntegrityError → UPDATE run_count+1` (C-03).

### Шаг 3 — Agent Run (работа агента)
* Если проблемка **новая** → `run_count=1`, `prior_findings=None`.
* Если **повтор** → `run_count+1`, `prior_findings` подгружаются из БД.
* Для `critical` → `auto_fix()` (перезапуск сервера с cooldown).

---

## 3. Компоненты проекта

| Файл | Размер | Ответственность | Ключевые функции |
|------|--------|-----------------|------------------|
| `monitor.py` | 17 069 байт, 450 строк | Главный мониторинг | `api`, `fingerprint`, `init_db`, `measure_tok_s`, `check_server`, `classify_incident`, `store_incident`, `auto_fix`, `main` |
| `demo_incident.py` | 7 050 байт | Демо жизненного цикла | `store_incident`, `show_memory`, `main` → пишет в `demo_incidents.db` |
| `make_icon.py` | 2 241 байт | Генерация иконки | `main()` (guard) → `superlog_lite_icon.{png,ico}` |
| `monitor_8083.bat` | 1 064 байт | Windows-лаунчер | `%~dp0` + `where python` + `pause` |
| `requirements.txt` | 119 байт | Зависимости | `Pillow>=10.0`, `pytest`, `ruff` |
| `tests/*.py` | 3 файла | Тесты (36 тестов) | `test_monitor.py`, `test_demo.py`, `test_misc.py` |
| `audit/AUDIT-1.md` | 16 793 байт | Аудит (15🔴 + 16🟡) | — |
| `audit/VERIFY-1.md` | 7 120 байт | Верификация фиксов | 36 passed, ruff 0 |
| `PLAN_FIX.md` | 7 084 байт | План устранения | Phase 0→G |
| `docs/USER_GUIDE.md` | 7 282 байт | Инструкция для пользователей | — |
| `docs/AGENT_INSTRUCTIONS.md` | 7 704 байт | Инструкции для агентов | — |
| `docs/ARCHITECTURE.md` | этот файл | Архитектура | — |
| `README.md` | 2 482 байт | Короткий README | — |

---

## 4. Данные: SQLite-схема

```sql
-- WAL mode, timeout=5.0
PRAGMA journal_mode=WAL;

TABLE incidents (
    id TEXT PRIMARY KEY,           -- == fingerprint (уникальный id проблемки)
    fingerprint TEXT UNIQUE,        -- hash(error_type|bucket)[:16]  ← C-02 bucketing
    error_type TEXT,               -- low_throughput / high_latency / generation_error / server_unreachable / server_busy
    top_frame TEXT,                -- детали / сообщение об ошибке
    first_seen TEXT,               -- ISO timestamp первого появления
    last_seen TEXT,                -- ISO timestamp последнего появления
    run_count INTEGER DEFAULT 0,   -- сколько раз видали
    findings TEXT,                 -- что нашёл агент
    resolution TEXT               -- NULL пока не решено
);

TABLE agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT,              -- FK → incidents.fingerprint
    started_at TEXT,               -- ISO timestamp начала проверки
    ended_at TEXT,                 -- ISO timestamp конца (C-02: теперь заполняется, раньше NULL)
    status TEXT,                   -- 'completed'
    actions_json TEXT              -- {"action":"monitored","data":{...}} ensure_ascii=False
);
```

**Отношения:** 1 `incident` ↔ many `agent_runs` (по `incident_id = incidents.fingerprint`).

---

## 5. Параметры конфигурации

Все через env (CLI-флаги переопределяют):

| Переменная env | Константа в `monitor.py` | Дефолт | CLI-флаг |
|---------------|--------------------------|--------|----------|
| `SUPERLOG_BASE` | `BASE` | `http://localhost:8083/v1` | — |
| `SUPERLOG_TOK_S_THRESHOLD` | `TOK_S_THRESHOLD` | `10` | `--threshold-tok` |
| `SUPERLOG_LATENCY_THRESHOLD` | `LATENCY_THRESHOLD` | `30` | `--threshold-latency` |
| `SUPERLOG_RESTART_COOLDOWN` | `RESTART_COOLDOWN_S` | `600` | — |
| `SUPERLOG_RESTART_BAT` | `RESTART_BAT` | `Path(__file__).parent.parent / "barozp-opus-8083/run_barozp_8083_mtp.bat"` | — |
| `SUPERLOG_RESTART_CWD` | `RESTART_CWD` | `parent(RESTART_BAT)` | — |

* `DB_PATH` = `Path(__file__).parent / "incidents.db"` — флагом `--db` не переопределяется для env, а для demo используется `DEMO_DB` env.

---

## 6. Как работает классификация (classify_incident)

Вход: `checks = check_server()` (dict с `checks.models` + `checks.generation`).

```python
incidents = []

# 1. Server unreachable (критическая)
if not models_check["ok"]:
    incidents.append(critical: server_unreachable)

# 1b. Slot busy (warning) — /health на корне сервера (не /v1/health)
slot = checks.slot
if slot and not slot.ok:
    incidents.append(warning: server_busy)   # проба генерации ПРОПУЩЕНА,
else:                                        # рестарт НЕ выполняется
    # 2. Generation error (критическая)
    gen = checks.generation
    if "error" in gen:
        incidents.append(critical: generation_error)
    else:
        # 3. Два НЕЗАВИСИМЫХ if (было elif — C-01)
        if gen.tok_s < TOK_S_THRESHOLD:
            incidents.append(warning: low_throughput)
        if gen.latency_s > LATENCY_THRESHOLD:
            incidents.append(warning: high_latency)
```

**Важно:** `low_throughput` и `high_latency` — оба могут сработать одновременно (было `elif` — один проглотил другой).

---

## 7. Как работает auto_fix (с cooldown)

```python
def auto_fix(incident):
    if incident.severity != "critical": return "skipped"

    # Cooldown (C-05)
    row = SELECT last_seen, run_count FROM incidents WHERE fingerprint=?
    if row and elapsed < RESTART_COOLDOWN_S and run_count > 1:
        return "skipped: cooldown"

    # Popen с cmd /c (C-07)
    proc = Popen(["cmd", "/c", RESTART_BAT], CREATE_NEW_CONSOLE, DEVNULL)

    # Ждём /models (C-06, не /health)
    for i in range(24):  # 120s max
        sleep(5)
        if "error" not in api("/models"):
            return "restarted", pid, waited_s
    return "restarted", pid, "note: server did not come back yet"
```

---

## 8. Безопасность

| Аспект | Статус |
|--------|--------|
| `C:\Users` пути | ❌ не найдено (security scan) |
| `F:\` хардкоды | ⚠️ только fallback по умолчанию (C-08) — переопределяется через `SUPERLOG_RESTART_BAT` |
| `ssl.CERT_NONE` | ⚠️ для `http://` не используется (только `urllib` без ssl-ctx для обычного http) |
| `Popen` | ✅ валидация `Path.exists()`, `cmd /c`, `DEVNULL` |
| Секреты/токены | ❌ не найдено |
| `eval/exec` | ❌ не используется |

> Анализ: `audit/AUDIT-1.md §5 Security scan` — 0 настоящих секретов.

---

## 9. Жизненный цикл (sequence)

```
1. python monitor.py
      │
      ▼
2. check_server()
   ├─ /models            → ok?
   ├─ /health (root)     → slot busy? → skip probe
   └─ /chat/completions  → tok_s, latency
      │
      ▼
3. classify_incident(checks)
   ├─ server_unreachable?  → incidents.append(critical)
   ├─ slot busy?           → incidents.append(warning: server_busy)
   ├─ generation_error?    → incidents.append(critical)
   ├─ tok_s < 10?          → incidents.append(warning: low_throughput)
   └─ latency > 30?        → incidents.append(warning: high_latency)
      │
      ▼
4. for inc in incidents:
   ├─ store_incident(inc)   → INSERT or UPDATE run_count
   └─ if critical and not --no-auto-fix:
         └─ auto_fix(inc)   → cooldown? → cmd /c restart_bat → wait /models
      │
      ▼
5. print INCIDENT MEMORY (sqlite SELECT)
```

---

*Архитектурная документация для `F:\superlog-lite`. Последний обновленный: 22.08.2026. Связано с `AUDIT-1.md`, `VERIFY-1.md`, `PLAN_FIX.md`.*
