"""embed_engine.py — embed the bot engine .py files into app.py's _EMBEDDED_ENGINE
so the built ALADIN.exe carries the WHOLE bot in one file. The engine files are
materialized next to the exe on first launch (_ensure_engine).

Safe: only replaces the _EMBEDDED_ENGINE line; keeps the rest of app.py intact."""
import base64
import os
import re
import sys

ENGINE_FILES = [
    "main.py", "bot_watchdog.py", "orchestrator.py", "config.py",
    "data_engine.py", "database.py", "models.py", "news_filter.py",
    "notifier.py", "risk_manager.py", "scholar.py", "scout.py",
]

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "app.py")
    bundle = {}
    missing = []
    for name in ENGINE_FILES:
        fp = os.path.join(here, name)
        if not os.path.exists(fp):
            missing.append(name)
            continue
        with open(fp, "rb") as f:
            bundle[name] = base64.b64encode(f.read()).decode()

    lit = "{" + ", ".join(f'"{k}": "{v}"' for k, v in bundle.items()) + "}"
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    pattern = re.compile(r"_EMBEDDED_ENGINE = \{[^}]*\}")
    if not pattern.search(src):
        print("ERROR: _EMBEDDED_ENGINE marker not found in app.py", file=sys.stderr)
        return 3
    src = pattern.sub(f"_EMBEDDED_ENGINE = {lit}", src, count=1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    print(f"OK: embedded {len(bundle)} engine files into app.py "
          f"({len(lit) // 1024} KB base64)")
    if missing:
        print(f"    WARNING missing (not embedded): {', '.join(missing)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
