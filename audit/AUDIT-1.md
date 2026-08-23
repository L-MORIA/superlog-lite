# AUDIT-1 — Superlog-lite (F:\superlog-lite) — 22.08.2026

**Аудитор:** Hermes Agent (code-audit v1.1.0) | **Профиль:** old-laptop | **Комиты:** не git-репозиторий (`fatal: not a git repository`)  
**Аудируемый коммит:** N/A (working directory) | **OS:** Windows 11 | **Python:** 3.11.15  
**Размер:** 4 исходника — `monitor.py` (327 строк, 10810 байт), `demo_incident.py` (186 строк), `make_icon.py` (69 строк), `monitor_8083.bat` (22 строки), БД `incidents.db` (24576 байт, 3 incidents / 4 agent_runs)

> Паттерн проекта: Fingerprint → Memory → Agent Run — мониторинг локального LLM `ik_llama:8083` (`/v1/models` + генерация → классификация инцидентов → SQLite → auto-fix рестартом). Идея верна, но реализация содержит системные ошибки дедупликации и авто-фикса.

---

## §0 Meta-observations

* Приоритетных аудитов в репо не найдено (поиск `audut/`, `audit/`, `review/` — пусто кроме созданного `audit/`).
* `git log` отсутствует — нет истории, нет возможности `git ls-remote` сверки. Рекомендация — инициализировать `git`.
* Опечаток-папок (`audut`) нет.
* `ruff check` — 6 ошибок (см. §2.1).
* `incidents.db` уже содержит 2 типа `low_throughput` с разными `fingerprint` (`dc461d4d6a2def0a` = `tok_s=8.2`, `e2122a2a31a38a6e` = `10.4 tok/s`) — живое доказательство Bug #2 (fragmented fingerprints).

---

## §1 Summary table

| Категория | Оценка | Деталь |
|-----------|--------|--------|
| Архитектура | 6/10 | Superlog-паттерн верный, но дублирование `fingerprint/init_db/store_incident` между `monitor.py`↔`demo_incident.py`, хардкоды `F:\barozp-opus-8083`, магические числа, нет конфига/CLI |
| Обработка ошибок | 4/10 | `api()` теряет `status`, `measure_tok_s` даёт ложный `low_throughput` при отсутствии `usage`, `elif` скрывает `high_latency`, `sqlite` без `IntegrityError`/`database is locked` |
| Чистота кода | 5/10 | 6× `ruff` (`F841`×2, `F541`×4), dead-константы, `import` внутри функций, top-level side effect в `make_icon.py`, `f-string` без плейсхолдеров |
| Тестирование | 0/10 | Тестов нет |
| Документация | 2/10 | Нет `README.md`, `requirements.txt`, `.gitignore`, `AGENTS.md` |
| Git-дисциплина | 0/10 | Не git-репозиторий |
| Безопасность | 7/10 | Секретов нет, но `ssl.CERT_NONE`, `Popen` без валидации пути, хардкоды `F:\` |

---

## §2 Критичные баги (🔴 must fix)

### C-01 `classify_incident` — `elif` скрывает `high_latency`
**Файл:** `monitor.py:146-174` | **Severity:** 🔴  
```python
if "error" in gen: ...                # generation_error
elif gen.get("tok_s",0) < 15: ...     # low_throughput
elif gen.get("latency_s",0) > 30: ... # high_latency — недостижимо если tok_s<15
```
Если `tok_s=8` и `latency=45s` — оба истинны, но второй не зарегистрируется. Потеря инцидента.  
**Фикс:** два независимых `if` для `tok_s` и `latency`.

---

### C-02 `fingerprint()` — фрагментированная дедупликация
**Файл:** `monitor.py:41-44`, `157-174` | **Severity:** 🔴  
```python
fingerprint("low_throughput", f"tok_s={gen['tok_s']:.1f}") # 8.2 vs 8.3 → разные fp
fingerprint("high_latency", f"{gen['latency_s']:.1f}s")    # 45.2s vs 45.3s → разные
fingerprint("generation_error", gen["error"])              # строки ошибки отличаются на мс
```
Подтверждено в БД: два `low_throughput` с разными `fingerprint`. `run_count` не инкрементируется, Superlog-memory не работает.  
**Фикс:** бакетить — `fingerprint("low_throughput", "low")`, `fingerprint("high_latency","high")`, для `generation_error` — нормализовать ошибку (первые 50 символов без чисел/таймаутов).

---

### C-03 `store_incident()` — race `UNIQUE constraint` → краш
**Файл:** `monitor.py:184-206`, `demo_incident.py:54-77` | **Severity:** 🔴  
`SELECT` → `INSERT` без `try/except IntegrityError`. Два параллельных `monitor.py` (cron + ручной) → один падает с `sqlite3.IntegrityError: UNIQUE constraint failed`.  
**Фикс:** `INSERT ... ON CONFLICT(fingerprint) DO UPDATE SET last_seen=..., run_count=run_count+1` или `try/except IntegrityError` с повторным `UPDATE`.

---

### C-04 `measure_tok_s()` — ложный `low_throughput` при отсутствии `usage`
**Файл:** `monitor.py:94-97` | **Severity:** 🔴  
`comp_tokens = usage.get("completion_tokens",0)` → `tok_s=0` → `classify_incident` → `low_throughput` даже если сервер здоров (некоторые `llama.cpp` не возвращают `usage`).  
**Фикс:** если `comp_tokens==0` → `return {"error": "no completion_tokens"}`, либо фолбэк `len(text.split())`.

---

### C-05 `auto_fix()` — бесконечный цикл рестартов без backoff/cooldown
**Файл:** `monitor.py:226-268` | **Severity:** 🔴  
При каждой проверке `generation_error`/`server_unreachable` дёргается `Popen(restart_bat)` → `CREATE_NEW_CONSOLE` спамит окнами. Нет `last_restart`, нет `max_retries`. Битый сервер → рестарт каждые N минут вечно.  
**Фикс:** хранить `last_restart` в БД/файле, `if now - last_restart < 600: skip`, `if run_count>3 за час: требовать ручного вмешательства`.

---

### C-06 `auto_fix()` — проверка `/health` может не существовать
**Файл:** `monitor.py:261` | **Severity:** 🔴  
`api("/health")` — у `ik_llama.cpp` часто нет `/health`, есть `/v1/models`. При 404 всегда `error` → ждёт 120с зря и возвращает `"server did not come back yet"` хотя сервер уже up.  
**Фикс:** проверять `api("/models")`.

---

### C-07 `auto_fix()` — `.bat` без `shell=True`/`cmd /c` — флаки на Windows
**Файл:** `monitor.py:249` | **Severity:** 🔴  
`Popen([restart_bat], ...)` с `.bat` без `shell=True` зависит от ассоциаций. Надёжно: `Popen(["cmd","/c", restart_bat], ...)`.

---

### C-08 Хардкоды `F:\` — непереносимо
**Файл:** `monitor.py:229,252`, `monitor_8083.bat:14,20` | **Severity:** 🔴  
`F:\barozp-opus-8083\run_barozp_8083_mtp.bat`, `F:\superlog-lite` — на `D:\` / Linux — краш. Нарушает правило аудита *Hardcoded PIDs/IPs/paths*.  
**Фикс:** `Path(__file__).parent / "../barozp-opus-8083"` или `config.json`/`env RESTART_BAT`.

---

### C-09 `api()` теряет HTTP-статус
**Файл:** `monitor.py:27-38` | **Severity:** 🔴  
`except Exception: return {"error": str(e)}` — `HTTPError 500` → строка `"HTTP Error 500: ..."`, статус потерян. `check_server` не различает `500`/`timeout`/`DNS`.  
**Фикс:** `except urllib.error.HTTPError as e: return {"error": str(e), "status": e.code, "body": e.read()[:500]}`.

---

### C-10 `check_server` — мёртвый `try/except`
**Файл:** `monitor.py:115-123` | **Severity:** 🔴  
`api()` никогда не бросает — возвращает `{"error":...}`. Ветка `except` недостижима, маскирует будущую регрессию.

---

### C-11 `measure_tok_s` — хардкод модели
**Файл:** `monitor.py:81` | **Severity:** 🔴  
`"model": "Qwen3_8-Opus-4_7-MTP-Q3KM-hybrid"` — если в `/v1/models` другой `id` → `model not found` → ложный `generation_error`. Должен брать `models["data"][0]["id"]` динамически.

---

### C-12 `store_incident` — два `datetime.now()` и дубль `id==fingerprint`
**Файл:** `monitor.py:201-206` | **Severity:** 🔴  
`first_seen = now`, `last_seen = now` делаются двумя вызовами (разница мкс). `id` и `fingerprint` оба = `fp` — семантическая ошибка схемы (`id` должен быть UUID/autoincrement, `fingerprint UNIQUE` отдельно).

---

### C-13 Дубль-код `fingerprint/init_db/store_incident`
**Файл:** `demo_incident.py:15-46,49-99` | **Severity:** 🔴  
Полный копипаст из `monitor.py`. Уже рассинхрон (в `demo` другой `findings` flow). При изменении сигнатуры в одном — второй сломается.

---

### C-14 Двойная запись `findings` в `demo_incident.py`
**Файл:** `demo_incident.py:70-96` | **Severity:** 🔴  
`INSERT` уже с `findings_init`, затем `if not is_recurrence: UPDATE findings=...та же строка...` — лишняя транзакция.

---

### C-15 Загрязнение боевой БД тестовыми данными
**Файл:** `demo_incident.py` + `incidents.db` | **Severity:** 🔴  
`demo` пишет в ту же `incidents.db` без очистки → мусор для `monitor.py`. Сейчас в БД 2 демо-инцидента + 1 реальный.

---

## §3 Moderate (🟡 should fix)

| № | Файл:строки | Проблема | Фикс |
|---|-------------|----------|------|
| M-01 | `monitor.py:22-24` | `ssl.CERT_NONE` + `check_hostname=False` для `http://` бессмысленно | Убрать `ctx` для `http`, оставить только для `https` или удалить совсем |
| M-02 | `monitor.py:244-245` | `CREATE_NO_WINDOW`, `DETACHED_PROCESS` объявлены но не используются (`ruff F841`) | Удалить |
| M-03 | `monitor.py:236,258,282,292` | `f"..."` без плейсхолдеров (`ruff F541`) | Убрать `f` |
| M-04 | `monitor.py:228,257` | `import subprocess/time` внутри функций | Вынести вверх |
| M-05 | `monitor.py:49` | `sqlite3.connect(DB_PATH)` без `timeout=5`, без `WAL` | `timeout=5.0` + `PRAGMA journal_mode=WAL` |
| M-06 | `monitor.py:50-74` | Нет обработки `database is locked` | `try/except OperationalError` с ретраем |
| M-07 | `monitor.py:18,157,167` | Магические `15`, `30`, `120`, `24*5` | `TOK_S_THRESHOLD=15`, `LATENCY_THRESHOLD=30` |
| M-08 | `monitor.py:271` | Нет CLI (`argparse`), нет `--help`/`--no-auto-fix` | Добавить `argparse` |
| M-09 | `monitor.py:314-317` | Ручное `close()` без `finally` | `with sqlite3.connect(...) as conn:` |
| M-10 | `monitor.py:64-72` | `agent_runs.ended_at` всегда `NULL` | Заполнять или убрать колонку |
| M-11 | `monitor.py:249` | `Popen` без `stdout/stderr` | `stdout=DEVNULL, stderr=DEVNULL` |
| M-12 | `make_icon.py:9-66` | Исполняется на `import` (нет `if __name__=="__main__"`) | Обернуть в `def main()` |
| M-13 | `make_icon.py:52` | Хардкод `arial.ttf` | Оставить fallback (уже есть) |
| M-14 | `monitor_8083.bat:16` | `python` без проверки venv | `python -c "import sys;..."` или `py -3` |
| M-15 | — | Нет `requirements.txt`, `.gitignore` | Добавить |
| M-16 | `monitor.py:29` | `json.dumps(body).encode()` без `ensure_ascii=False` | Добавить |

---

## §4 Low (🟢 nice-to-have)

* Нет `README.md` (как запускать, что такое Fingerprint → Memory).
* Русский промпт `Отвечай кратко...` хардкод (`monitor.py:83`) — лучше константа.
* `DB_PATH` без проверки прав, нет ротации логов.
* `monitor_8083.bat` — `chcp 65001` ок, но нет `errorlevel` проверки.

---

## §5 Security scan

| Паттерн | Файлов проверено | Хитов | Severity |
|---------|------------------|-------|----------|
| `C:\Users` | 6 (включая `__pycache__`) | 0 | — |
| `F:\` хардкод | 4 | 4 (`monitor.py:229,252`, `monitor_8083.bat:14,20`) | 🟡 (не секрет, но непереносимо) |
| Токены `[A-Za-z0-9_\-]{20,}` | 6 | 0 реальных (только `incidents.db` бинарник) | — |
| `password/passwd/secret/token` | 6 | 0 (`token` только в `completion_tokens`) | — |
| `eval/exec(` | 6 | 0 | — |
| `Popen/subprocess` | 6 | 1 (`monitor.py:249`) | 🟡 — нужен валидатор пути |
| `ssl.CERT_NONE` | 6 | 1 (`monitor.py:24`) | 🟡 |

**Вывод:** секретов нет. Единственные находки — хардкоды путей и `CERT_NONE`.

---

## §6 Приоритизированный fix-list

| Приоритет | # | Файл | Что делать |
|-----------|---|------|------------|
| 🔴 P0 | 1 | `monitor.py:157-174` | Разделить `elif` на независимые `if` |
| 🔴 P0 | 2 | `monitor.py:41-44` | Бакетить fingerprint (не точное значение) |
| 🔴 P0 | 3 | `monitor.py:184-206` | `ON CONFLICT` / `IntegrityError` handling + `timeout` + `WAL` + один `now` |
| 🔴 P0 | 4 | `monitor.py:94-97` | Обработка `completion_tokens==0` |
| 🔴 P0 | 5 | `monitor.py:226-268` | Backoff/cooldown + `cmd /c` + проверка `/models` вместо `/health` |
| 🔴 P0 | 6 | `monitor.py:27-38` | `HTTPError` с `status` |
| 🔴 P0 | 7 | `monitor.py:81` | Модель брать из `/models` |
| 🔴 P0 | 8 | `monitor.py:229,252` | Вынести пути в конфиг/`Path` от `__file__` |
| 🟠 P1 | 9 | `demo_incident.py` | Импорт из `monitor.py`, отдельная демо-БД, убрать двойной `UPDATE` |
| 🟠 P1 | 10 | `monitor.py` | Ruff-фиксы (`F541`, `F841`), импорты наверх, константы |
| 🟠 P1 | 11 | `make_icon.py` | `if __name__` guard |
| 🟠 P1 | 12 | проект | `.gitignore`, `requirements.txt`, `README.md`, `config.json` |
| 🟡 P2 | 13 | `monitor.py` | `argparse --help/--no-auto-fix`, `with connect`, `ended_at` |

---

## §7 План реконструкции с верификацией

| Phase | Scope | Gate |
|-------|-------|------|
| **Pre-Phase** | Тест-инфра, `security_scan` скрипт лог | `ruff check` + `python -m py_compile` зелёные |
| **Phase A** | Quick wins: `.gitignore`, `requirements.txt`, `make_icon.py` guard, `bat` — пути | `ruff` зелёный |
| **Phase B** | Критичные в `monitor.py` (C-01→C-12) по одному багу → тест | `pytest -q` + `ruff` + `python -m py_compile monitor.py` |
| **Phase C** | `demo_incident.py` — импорт + демо-БД | `pytest -q` |
| **Phase D** | Структура: `config.json`, `README.md`, smoke `python monitor.py --help` | `python monitor.py --help` → 0 |
| **Phase E** | Качество: `ruff --fix`, `with` connect, константы | `ruff check` чист |
| **Phase G** | CI: `pytest` + `ruff` в `test` | `pytest` green |

---

## §8 Overall verdict

| Категория | Балл | Комментарий |
|-----------|------|-------------|
| Архитектура | 6/10 | Идея сильная, реализация требует рефактора дедупликации |
| Надёжность | 4/10 | `elif`, `usage=0`, race, health-check — ложные/потерянные инциденты |
| Авто-фикс | 3/10 | Без backoff — опасен в проде |
| Чистота | 5/10 | Ruff-ошибки, дубли |
| Безопасность | 7/10 | Секретов нет |
| **Итого** | **4.2/10** | **Не готово к прод-мониторингу без Phase B** |

Рекомендация — выполнить `PLAN_FIX.md` (создаётся следом) и прогнать `pytest` + `ruff` до зелёного.

---
*Сгенерировано автоматически, проверено `ruff 0.x`, `python -m py_compile`.*
