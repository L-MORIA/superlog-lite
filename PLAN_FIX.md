# PLAN_FIX — план устранения всех ошибок AUDIT-1 (F:\superlog-lite)

**Источник:** `audit/AUDIT-1.md` (15×🔴 C-01..C-15, 16×🟡 M-01..M-16)  
**Цель:** довести `ruff check`, `py_compile`, `pytest` до зелёного, закрыть все 🔴.

---

## Phase 0 — Pre-Phase (инфра)

| Шаг | Что | Файл | Gate |
|-----|-----|------|------|
| 0.1 | Создать `audit/` папку и этот план | уже | `ls audit/` |
| 0.2 | Зафиксировать текущие ошибки `ruff` | — | `ruff check F:/superlog-lite → 6 errors` (baseline) |

---

## Phase A — Quick wins (15 мин) — 🟢/🟡

| № | Bug | Что делать | Проверка |
|---|-----|------------|----------|
| A1 | M-15 | Добавить `.gitignore` (`__pycache__/`, `*.pyc`, `incidents.db`, `demo_incidents.db`, `*.ico`/`*.png` опционально) | `cat .gitignore` |
| A2 | M-15 | Добавить `requirements.txt` (`Pillow` для `make_icon.py`, иначе только stdlib) | `pip install -r requirements.txt --dry-run` |
| A3 | M-11 | `make_icon.py` — обернуть в `def main()` + `if __name__=="__main__"` | `python -c "import make_icon; print('no side effect')"` |
| A4 | M-03/M-02 | `monitor.py` ruff: убрать `f` без плейсхолдеров, удалить dead `CREATE_NO_WINDOW/DETACHED_PROCESS` | `ruff check` −4 |
| A5 | M-04 | Вынести `import subprocess/time` наверх | `ruff` |
| A6 | — | `monitor_8083.bat` — добавить `setlocal`, проверку `errorlevel`, не хардкод `F:`? (оставить но задокументировать) | `bat` читаем |
| A7 | M-15 | Добавить `README.md` с запуском | `cat README.md` |

**Gate A:** `ruff check F:/superlog-lite` падает с 6 → 2, `python -m py_compile` OK.

---

## Phase B — Критичные в `monitor.py` (P0) — по одному багу → тест

Каждый подшаг — **патч → `py_compile` → `ruff` → `pytest`**.

| Шаг | Bug | Правка (точные строки) | Тест |
|-----|-----|------------------------|------|
| B1 | C-01 | `classify_incident`: заменить `elif tok_s<15` + `elif latency>30` на два `if` (стр.157-174) | `test_classify_both_triggers` |
| B2 | C-02 | `fingerprint`: бакетить — `low_throughput→"low"`, `high_latency→"high"`, `generation_error→normalize(error)` (первые 200 символов без цифр таймаута) | `test_fingerprint_bucketing` — 8.2 и 8.3 дают один fp |
| B3 | C-04 | `measure_tok_s`: если `completion_tokens==0` и нет `error` → вернуть `{"error":"no completion_tokens"}` чтобы `classify` дал `generation_error`, а не `low_throughput` | `test_measure_no_usage` |
| B4 | C-09+C-10 | `api()`: ловить `HTTPError` отдельно с `status`, убрать dead `try` в `check_server` | `test_api_http_error` |
| B5 | C-11 | `measure_tok_s`/`check_server`: брать модель из `api("/models")` (первый `id`) вместо хардкода, fallback на константу | `test_measure_dynamic_model` |
| B6 | C-08 | Вынести `BASE`, `RESTART_BAT`, `RESTART_CWD` в константы вверх + `RESTART_BAT = Path(__file__).parent.parent / "barozp-opus-8083/run_barozp..."` с `os.getenv("SUPERLOG_RESTART_BAT")` фолбэком | `test_hardcoded_paths_removed` |
| B7 | C-12 | `store_incident`: один `now = datetime.now().isoformat()` для `first/last`, `INSERT ... ON CONFLICT` + `timeout=5`, `WAL` | `test_store_race` |
| B8 | C-03 | `init_db`: `PRAGMA journal_mode=WAL`, `connect(timeout=5.0)`, обработка `OperationalError` | `test_db_wal` |
| B9 | C-05+C-06+C-07 | `auto_fix`: cooldown файл/БД (`last_restart`), проверка `/models` вместо `/health`, `["cmd","/c", restart_bat]` | `test_auto_fix_cooldown`, `test_auto_fix_health_endpoint` |
| B10 | M-05..M-10 | Константы `TOK_S_THRESHOLD` etc., `with connect`, `ended_at`, `DEVNULL` | `ruff` |
| B11 | C-08 | `ctx` — убрать для `http`, оставить только если `BASE` `https` | `test_ssl_context` |

**Gate B:** `ruff check` 0 ошибок, `pytest tests/test_monitor.py -q` зелёный.

---

## Phase C — `demo_incident.py` (P1)

| Шаг | Bug | Правка |
|-----|-----|--------|
| C1 | C-13 | Удалить копипаст `fingerprint/init_db` — `from monitor import fingerprint, init_db` |
| C2 | C-14 | Убрать второй `UPDATE findings` (оставить только INSERT) |
| C3 | C-15 | Отдельная БД `demo_incidents.db` по умолчанию, или флаг `--db` + cleanup (`Path(DB_PATH).unlink(missing_ok=True)` в начале) |
| C4 | — | Добавить `if __name__=="__main__"` guard (уже есть — проверить) |

**Gate C:** `python demo_incident.py` не трогает `incidents.db`, `pytest tests/test_demo.py`.

---

## Phase D — Структура и доки (P1)

| Шаг | Что |
|-----|-----|
| D1 | `config.json` (опционально) с `base_url`, `thresholds`, `restart_bat` |
| D2 | `README.md` — как запускать `monitor.py`, `demo_incident.py`, что такое fingerprint |
| D3 | Smoke: `python monitor.py --help` → 0, `python monitor.py --no-auto-fix --help` |

**Gate D:** `python monitor.py --help` проходит.

---

## Phase E — Качество (P2)

| Шаг | Что |
|-----|-----|
| E1 | `argparse` для `monitor.py` (`--once`, `--no-auto-fix`, `--threshold-tok`, `--db`) |
| E2 | `ruff --fix` финальный прогон |
| E3 | `with sqlite3.connect` везде |

**Gate E:** `ruff check .` 0, `python -m py_compile` OK.

---

## Phase G — Тесты и верификация (финал)

**Файлы тестов:**
* `tests/test_monitor.py` — 12 тестов (см. B1..B11)
* `tests/test_demo.py` — 2 теста
* `tests/test_security.py` — скан хардкодов
* `pytest.ini` / `pyproject` минимально

**Команды gate:**
```bat
ruff check F:\superlog-lite
python -m py_compile F:\superlog-lite\monitor.py F:\superlog-lite\demo_incident.py F:\superlog-lite\make_icon.py
pytest F:\superlog-lite\tests -q
python F:\superlog-lite\monitor.py --help
python F:\superlog-lite\demo_incident.py
```

Ожидаемый результат: `ruff: All checks passed`, `pytest: 15 passed`, `monitor --help: 0`.

---

## Порядок коммитов

1. `fix: Phase A quick wins — gitignore, make_icon guard, ruff F541/F841`
2. `fix: Phase B P0 monitor.py — classify elif, fingerprint bucket, api status, model, store race, auto-fix`
3. `fix: Phase C demo_incident — dedup import, demo db`
4. `docs: README + requirements + PLAN_FIX`
5. `test: add pytest suite — verify no regressions`

---

*После выполнения — удалить `__pycache__` и пересоздать `incidents.db` с нуля (тестовая БД).*
