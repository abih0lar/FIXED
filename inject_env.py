"""inject_env.py — embed a copy of .env into app.py's _EMBEDDED_ENV_B64.

Why: ALADIN.exe is meant to be distributed/sold. It must contain the
tuned strategy settings (that IS the product) and, because license
validation is offline/symmetric, the DRM *validation* key. It must NEVER
contain the builder's MT5 credentials or Telegram/Gemini tokens.

Key handling
------------
MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_PATH,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY, LICENSE_KEY
    Always BLANKED. Every buyer enters their own values in the GUI.

ALADIN_DRM_KEY
    Blanked by default (safe for dev builds / public source).
    Pass --embed-drm-key to keep the value from your .env so the built
    exe can validate licenses offline. The key ships inside the exe
    either way (that is inherent to offline Fernet validation) — this
    flag just decides whether it comes from your .env at build time or
    from the (discouraged, legacy) _EMBEDDED_SECRET constant in app.py.
    NEVER commit a real ALADIN_DRM_KEY to source control.

Safe: only replaces the _EMBEDDED_ENV_B64 line in app.py; keeps the rest.
"""
import base64
import os
import sys

ALWAYS_BLANKED = {
    "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY",
    "LICENSE_KEY",
}

def build_sanitized_env(env_path, embed_drm_key: bool = False):
    lines_out = []
    blanked = []
    kept_drm = False
    with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in line:
                key = line.split("=", 1)[0].strip()
                if key in ALWAYS_BLANKED:
                    blanked.append(key)
                    lines_out.append(f"{key}=''\n")
                    continue
                if key == "ALADIN_DRM_KEY":
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    if embed_drm_key and val:
                        kept_drm = True
                        lines_out.append(line)
                    else:
                        blanked.append(key)
                        lines_out.append(f"{key}=''\n")
                    continue
            lines_out.append(line)
    return "".join(lines_out), blanked, kept_drm

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "app.py")
    embed_drm_key = "--embed-drm-key" in sys.argv
    env_path = os.path.join(here, "data", ".env")
    if not os.path.exists(env_path):
        env_path = os.path.join(here, ".env")   # legacy location fallback
    if not os.path.exists(env_path):
        print("ERROR: .env not found next to this script", file=sys.stderr)
        return 2

    sanitized, blanked, kept_drm = build_sanitized_env(env_path, embed_drm_key)
    if embed_drm_key and not kept_drm:
        print("WARNING: --embed-drm-key given but ALADIN_DRM_KEY is empty in "
              ".env — the built exe will NOT be able to validate licenses.",
              file=sys.stderr)
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
    if kept_drm:
        print("    ALADIN_DRM_KEY: EMBEDDED (exe can validate licenses offline)")
        print("    NOTE: app.py now contains the key — do NOT commit it.")
        print("          Run `git checkout -- app.py` after building, or")
        print("          `python inject_env.py` to restore the sanitized blob.")
    else:
        print("    kept keys: settings/defaults (all strategy tuning intact)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
