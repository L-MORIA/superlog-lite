# Инструкции для ИИ-агентов: Superlog-lite

Техническая справка для программных агентов, модернизирующих и поддерживающих проект `superlog-lite`.

---

## 1. Как запускать и проверять

```bat
cd F:\superlog-lite

:: Lint + типизация
ruff check monitor.py demo_incident.py make_icon.py

:: Компиляция
python -m py_compile monitor.py demo_incident.py make_icon.py

:: Тесты (36 штук в tests/)
pytest tests -v

:: Smoke
python monitor.py --help
python monitor.py --no-auto-fix --db tests/smoke.db
python demo_incident.py --db tests/demo_smoke.db
```

**Gate:** `ruff: 0 errors`, `pytest: 36 passed`, `py_compile: OK`.

---

## 2. Как вносить изменения безопасно

1. **Бэкапить перед правкой** (правило пользователя Ирины):
   ```bat
   cp monitor.py monitor.py.bak_%DATE%_%TIME%
   cp incidents.db incidents.db.pre_fix_bak
   ```
2. **Не удалять старые секции/провайдеры** без явного разрешения. Синхронизация — добавление/обновление, не вычитание.
3. **Никогда не перезаписывать `incidents.db` без backup.**
4. Перед `git push` — `grep -r "C:\\Users" / "secrets" / "tokens" / 24+ chars` в diff.
5. Если `context_length` в `config.yaml` — не трогать (Юзер запретил).

### Добавление нового типа инцидента

В `monitor.py`:

1. В `classify_incident` добавить `if`-блок (обычно **не `elif`** — чтобы не скрывать другие).
2. В маппинге `fingerprint()` добавить bucketing для нового типа:
   ```python
   if error_type == "my_new":
       bucket = "my_bucket"
   ```
3. Добавить тест в `tests/test_monitor.py`:
   ```python
   def test_classify_my_new():
       checks = {"checks": {"models": {"ok": True}, "generation": {"tok_s": 5, "latency_s": 40, "error": "specific msg"}}}
       # assert
   ```
4. `pytest tests/test_monitor.py::test_classify_my_new` — должен пассовать.

---

## 3. Как работает система порогов

Все пороги — константы в `monitor.py`, переопределяемые через env или `--threshold-*`:

| Константа | Env | CLI | Дефолт | Применение |
|----------|-----|-----|--------|------------|
| `TOK_S_THRESHOLD` | `SUPERLOG_TOK_S_THRESHOLD` | `--threshold-tok` | `10` | `tok_s < X → low_throughput` |
| `LATENCY_THRESHOLD` | `SUPERLOG_LATENCY_THRESHOLD` | `--threshold-latency` | `30` | `latency_s > X → high_latency` |
| `RESTART_COOLDOWN_S` | `SUPERLOG_RESTART_COOLDOWN` | — | `600` | Задержка между рестартами |
| `GEN_TIMEOUT` | `SUPERLOG_GEN_TIMEOUT` | `--gen-timeout` | `120` | Таймаут пробы генерации, сек |

CLI-флаги переопределяют env (если переданы).

---

## 4. Как работает авто-исправление (auto_fix)

Файл: `monitor.py → auto_fix(incident)`.

**Логика:**

1. Если `severity != "critical"` → `skipped` (только `server_unreachable`, `generation_error` имеют `critical`; `low_throughput`/`high_latency`/`server_busy` — `warning`).
2. **Cooldown** (C-05): читает `last_seen` + `run_count` для `fingerprint` из БД. Если `elapsed < RESTART_COOLDOWN_S (600)` **и** `run_count > 1` → `skipped: cooldown`.
3. Проверяет существование `RESTART_BAT` и `RESTART_CWD` (хардкод `F:\barozp-opus-8083` → заменён на `Path(__file__).parent.parent/...`).
4. `Popen(["cmd", "/c", RESTART_BAT], CREATE_NEW_CONSOLE, DEVNULL)` — `cmd /c` гарантирует запуск `.bat`.
5. Ждёт до 120 сек, проверяя `/models` (не `/health` — C-06) каждые 5 сек.
6. Возвращает `{"action": "restarted", "pid": N, "waited_s": M}`.

**C-07:** Раньше `Popen([restart_bat])` без `shell=True` мог не запуститься на Windows. Теперь `["cmd","/c", bat]`.

---

## 5. Где находятся резервные копии и БД

| Путь | Назначение |
|------|------------|
| `F:\superlog-lite\incidents.db` | Боевая БД (SQLite WAL) |
| `F:\superlog-lite\incidents.db.pre_fix_bak` | Backup перед правками |
| `F:\superlog-lite\demo_incidents.db` | Демо-БД (не трогает боевую) |
| `F:\superlog-lite\monitor.py.bak_*` | Бэкапы `.py` перед right fixes |
| `F:\superlog-lite\audit/` | AUDIT-1.md, VERIFY-1.md |

---

## 6. База данных: схема

SQLite (`PRAGMA journal_mode=WAL;`, `timeout=5.0`):

```sql
TABLE incidents (
    id TEXT PRIMARY KEY,           -- == fingerprint (семантика: уникальный id проблемки)
    fingerprint TEXT UNIQUE,        -- hash(error_type|bucket)[:16]
    error_type TEXT,               -- low_throughput / high_latency / generation_error / server_unreachable
    top_frame TEXT,                -- сообщение / детали
    first_seen TEXT,               -- ISO timestamp первого появления
    last_seen TEXT,                -- ISO timestamp последнего появления
    run_count INTEGER DEFAULT 0,   -- сколько раз видали
    findings TEXT,                 -- что нашёл агент
    resolution TEXT                -- NULL пока не решено
);

TABLE agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT,              -- FK → incidents.fingerprint
    started_at TEXT,               -- ISO timestamp начала проверки
    ended_at TEXT,                 -- ISO timestamp конца (заполняется — C-02 в AUDIT-1)
    status TEXT,                   -- 'completed'
    actions_json TEXT              -- {"action":"monitored","data":{...}} ensure_ascii=False
);
```

**INSERT-UPDATE race (C-03):** `store_incident` сначала пытается `INSERT`, ловит `sqlite3.IntegrityError` → `UPDATE run_count+1`.

Запросы для агентов:
```sql
-- Последние 10 проблемок
SELECT fingerprint, error_type, run_count, last_seen FROM incidents ORDER BY last_seen DESC LIMIT 10;

-- Все runs для одной проблемки
SELECT started_at, ended_at, status, actions_json FROM agent_runs WHERE incident_id=? ORDER BY started_at;

-- Сбросить БД (удалить все):
DELETE FROM incidents; DELETE FROM agent_runs;
```

---

## 7. Известные ограничения

| Ограничение | Комментарий |
|-------------|-------------|
| Нет `/health`-endpoint у ik_llama.cpp | Используется `/models` вместо `/health` (C-06). Некоторые сборки `/v1/health` 404. |
| `usage` может быть пустым | `measure_tok_s` оценивает по длине текста (`len // 4`) если `usage.completion_tokens == 0`. |
| Auto-fix работает только с одним `.bat` | `RESTART_BAT` через env — можно переопределить. |
| Нет веб-интерфейса | Только CLI + БД. |
| Python 3.11+ | Требуется для `walrus`-подобных фич и `Path`. |
| Windows-only auto-fix | `CREATE_NEW_CONSOLE` и `cmd /c` Windows-specific; на Linux auto_fix вернёт `failed`. |

---

*Инструкции для ИИ-агентов. Связанные файлы: `AUDIT-1.md`, `VERIFY-1.md` (в `audit/`), `ARCHITECTURE.md` (рядом).*
