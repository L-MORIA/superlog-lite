#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Честное сравнение двух локальных LLM-серверов (OpenAI-compatible /v1/completions).
ОДИН сервер за раз (16GB VRAM не держит оба). Запуск:
  python compare_models.py --url http://127.0.0.1:8080 --name qwen38
  python compare_models.py --url http://127.0.0.1:8082 --name falcon-h1
  python compare_models.py --report        # сводит оба JSON бок о бок
Результаты пишутся в compare_<name>.json (UTF-8).
3 одинаковых RU-промпта: общий, код, математика.
"""
import argparse, json, sys, time, urllib.request, urllib.error, glob, os

PROMPTS = {
    "general": (
        "Объясни простыми словами, чем отличаются рекуррентные нейронные сети "
        "от трансформеров с механизмом внимания. Приведи по одному конкретному "
        "примеру применения каждой архитектуры. Ответ дай на русском, структурированно."
    ),
    "code": (
        "Напиши на Python функцию, которая находит длину самой длинной "
        "непрерывной возрастающей подпоследовательности в списке целых чисел. "
        "Добавь короткий пример вызова и объяснение на русском."
    ),
    "math": (
        "Решите задачу и кратко поясните ход решения на русском: "
        "В бассейн проведены две трубы. Первая наполняет его за 6 часов, "
        "вторая — за 4 часа. За сколько часов бассейн наполнятся, если открыть обе трубы одновременно?"
    ),
}
MAX_TOKENS = 400
TEMPERATURE = 0.0

def post_completion(url, prompt):
    body = {
        "model": "x",
        "prompt": prompt,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "stream": False,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180) as resp:
        raw = resp.read().decode("utf-8")
    wall = time.time() - t0
    return json.loads(raw), wall

def run_one(url, name):
    print(f"\n>>> [{name}] POST {url}/v1/completions (3 RU-промпта) ...", flush=True)
    tasks = {}
    agg_gen, agg_prompt, agg_wall, n_ok = 0.0, 0.0, 0.0, 0
    for key, prompt in PROMPTS.items():
        try:
            j, wall = post_completion(url, prompt)
        except urllib.error.URLError as e:
            print(f"!!! [{name}/{key}] SERVER NOT REACHABLE: {e}", flush=True)
            tasks[key] = {"error": str(e)}
            continue
        except Exception as e:
            print(f"!!! [{name}/{key}] ERROR: {e}", flush=True)
            tasks[key] = {"error": str(e)}
            continue
        text = j["choices"][0]["text"]
        tim = j.get("timings", {}) or {}
        gen_tps = tim.get("predicted_per_second", 0) or 0
        prompt_tps = tim.get("prompt_per_second", 0) or 0
        gen_tok = tim.get("predicted", 0) or 0
        prompt_tok = tim.get("prompt", 0) or 0
        tasks[key] = {
            "prompt": prompt,
            "generated_text": text,
            "gen_tps": round(gen_tps, 2),
            "prompt_tps": round(prompt_tps, 2),
            "gen_tokens": gen_tok,
            "prompt_tokens": prompt_tok,
            "wall_seconds": round(wall, 2),
        }
        if gen_tps:
            agg_gen += gen_tps; agg_prompt += prompt_tps; agg_wall += wall; n_ok += 1
        print(f"    [{key}] gen_tps={gen_tps}  prompt_tps={prompt_tps}  "
              f"gen_tok={gen_tok}  wall={wall:.1f}s", flush=True)

    if n_ok:
        summary = {
            "avg_gen_tps": round(agg_gen / n_ok, 2),
            "avg_prompt_tps": round(agg_prompt / n_ok, 2),
            "avg_wall_seconds": round(agg_wall / n_ok, 2),
        }
    else:
        summary = {"avg_gen_tps": 0, "avg_prompt_tps": 0, "avg_wall_seconds": 0}

    result = {
        "name": name,
        "url": url,
        "max_tokens": MAX_TOKENS,
        "summary": summary,
        "tasks": tasks,
    }
    out = f"compare_{name}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"    avg_gen_tps={summary['avg_gen_tps']}  saved -> {out}", flush=True)
    return result

def report():
    files = sorted(glob.glob("compare_*.json"))
    if not files:
        print("Нет файлов compare_*.json для свода.")
        return
    data = {}
    for fn in files:
        with open(fn, encoding="utf-8") as f:
            d = json.load(f)
        data[d["name"]] = d
    names = list(data.keys())
    print("\n" + "=" * 90)
    print("СВОДНОЕ СРАВНЕНИЕ (честный замер: 3 RU-промпта, один и тот же текст)")
    print("=" * 90)
    print(f"{'метрика':<20}" + "  ".join(f"{n:>16}" for n in names))
    def row(label, fn_key, fmt="{:.2f}"):
        cells = "  ".join(fmt.format(d[fn_key]) if d.get(fn_key) is not None else "  -   " for d in [data[n] for n in names])
        print(f"{label:<20}{cells}")
    print("\n-- СРЕДНИЕ ПО 3 ПРОМПТАМ --")
    row("avg_gen_tps", "summary.avg_gen_tps")
    row("avg_prompt_tps", "summary.avg_prompt_tps")
    row("avg_wall (s)", "summary.avg_wall_seconds", "{:.1f}")
    print("\n-- ПО ПРОМПТАМ (gen_tps) --")
    for pk in PROMPTS:
        label = {"general": "gen_tps general", "code": "gen_tps code", "math": "gen_tps math"}[pk]
        cells = []
        for n in names:
            t = data[n]["tasks"].get(pk, {})
            cells.append(f"{t.get('gen_tps', 0):>16}")
        print(f"{label:<20}" + "  ".join(cells))
    # тексты
    print("\n--- ТЕКСТЫ (первые 350 символов каждого промпта) ---")
    for n in names:
        print(f"\n########## {n} (avg_gen_tps={data[n]['summary']['avg_gen_tps']}) ##########")
        for pk in PROMPTS:
            t = data[n]["tasks"].get(pk, {})
            txt = t.get("generated_text", t.get("error", "<<нет данных>>"))
            print(f"\n### [{pk}] (gen_tps={t.get('gen_tps','-')})")
            print(txt[:350])
    # вердикт
    if len(names) == 2:
        a, b = names
        ga, gb = data[a]["summary"]["avg_gen_tps"], data[b]["summary"]["avg_gen_tps"]
        if ga and gb:
            faster, slow = (a, b) if ga > gb else (b, a)
            print(f"\n>>> БЫСТРЕЕ: {faster} ({max(ga,gb)/min(ga,gb):.2f}x vs {slow})")
    print("=" * 90)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="server base URL")
    ap.add_argument("--name", help="label: qwen38 / falcon-h1")
    ap.add_argument("--report", action="store_true", help="свести compare_*.json")
    args = ap.parse_args()
    if args.report:
        report(); return
    if not args.url or not args.name:
        ap.error("нужны --url и --name (или --report)")
    run_one(args.url, args.name)

if __name__ == "__main__":
    main()
