"""inject_env.py — embed a SANITIZED copy of .env into app.py's _EMBEDDED_ENV_B64.

Why sanitized: ALADIN.exe is meant to be distributed/sold. It must contain the
tuned strategy settings (that IS the product), but it must NEVER contain the
builder's MT5 credentials, Telegram token, Gemini key, or the DRM secret.
Each buyer enters their OWN MT5 account in the GUI Broker tab.

Sensitive keys are blanked here:
  MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_PATH,
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY,
  ALADIN_DRM_KEY, LICENSE_KEY

Safe: only replaces the placeholder line in app.py; keeps everything else."""
import base64
import os
import sys

SENSITIVE = {
    "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY",
    "ALADIN_DRM_KEY", "LICENSE_KEY",
}

def build_sanitized_env(env_path):
    lines_out = []
    blanked = []
    with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in line:
                key = line.split("=", 1)[0].strip()
                if key in SENSITIVE:
                    blanked.append(key)
                    lines_out.append(f"{key}=''\n")
                    continue
            lines_out.append(line)
    return "".join(lines_out), blanked

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "app.py")
    env_path = os.path.join(here, "data", ".env")
    if not os.path.exists(env_path):
        env_path = os.path.join(here, ".env")   # legacy location fallback
    if not os.path.exists(env_path):
        print("ERROR: .env not found next to this script", file=sys.stderr)
        return 2

    sanitized, blanked = build_sanitized_env(env_path)
    b64 = base64.b64encode(sanitized.encode("utf-8")).decode()

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    # replace the existing _EMBEDDED_ENV_B64 value (empty placeholder OR a
    # previously injected blob) with the fresh sanitized one
    import re
    pattern = re.compile(r'_EMBEDDED_ENV_B64 = "[^"]*"')
    if not pattern.search(src):
        print("ERROR: _EMBEDDED_ENV_B64 marker not found in app.py", file=sys.stderr)
        return 3
    src = pattern.sub(f'_EMBEDDED_ENV_B64 = "{b64}"', src, count=1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    print(f"OK: sanitized .env embedded into app.py ({len(b64)} base64 chars)")
    print(f"    blanked keys ({len(blanked)}): {', '.join(sorted(blanked))}")
    print(f"    kept keys: settings/defaults (all strategy tuning intact)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
