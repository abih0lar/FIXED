"""inject_env.py — embed a SANITIZED copy of .env into app.py's _EMBEDDED_ENV_B64.

Why sanitized: ALADIN.exe is meant to be distributed/sold. It must contain the
tuned strategy settings (that IS the product), but it must NEVER contain the
builder's MT5 credentials, Telegram token, Gemini key, or the DRM secret.
Each buyer enters their OWN MT5 account in the GUI Broker tab.

Sensitive keys are blanked here:
  MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_PATH,
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY,
  ALADIN_DRM_KEY, LICENSE_KEY

EXCEPTION — the DRM validation secret: the exe must be able to VALIDATE
licenses offline, which (with today's symmetric Fernet scheme) requires the
same secret keygen.py signs with. So ALADIN_DRM_KEY stays blanked in the
embedded .env blob (no duplication; a live .env still overrides it), but the
builder's local value is stamped into app.py's _EMBEDDED_SECRET line here.
The repo source only carries a placeholder — the real secret is never
committed, and rotating it = edit data/.env + re-run build.bat + reissue
keys with keygen.py.

Safe: only replaces the placeholder lines in app.py; keeps everything else."""
import base64
import os
import re
import sys

# The secret that was accidentally committed to git history (app.py).
# If the builder is STILL using it, licenses minted from the public repo
# source would validate — refuse to silently embed it.
BURNED_SECRETS = {"LORDfrank2020$"}

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

def get_raw_env_value(env_path, key):
    """Read one KEY=VALUE from the builder's real .env (before sanitization)."""
    with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip("'\"")
    return ""

def inject_drm_secret(app_path, secret):
    """Stamp the builder's ALADIN_DRM_KEY into app.py's _EMBEDDED_SECRET.

    Uses a function replacement so special regex/backslash chars in the
    secret can't corrupt the substitution."""
    if not secret:
        return False, "ALADIN_DRM_KEY not found in builder .env — _EMBEDDED_SECRET left as placeholder (exe will report 'DRM secret missing')."
    if secret in BURNED_SECRETS:
        return False, ("ALADIN_DRM_KEY is the OLD LEAKED secret (present in git "
                       "history). Rotate it in data/.env before building, then "
                       "re-mint licenses with keygen.py. Build ABORTED so no exe "
                       "ships with a forgeable secret.")
    with open(app_path, "r", encoding="utf-8") as f:
        src = f.read()
    pattern = re.compile(r'_EMBEDDED_SECRET = "[^"]*"')
    if not pattern.search(src):
        return False, "_EMBEDDED_SECRET marker not found in app.py"
    # function-replacement: no backslash/escape interpretation of the secret
    src = pattern.sub(lambda m: f'_EMBEDDED_SECRET = "{secret}"', src, count=1)
    with open(app_path, "w", encoding="utf-8") as f:
        f.write(src)
    return True, "OK"

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
    pattern = re.compile(r'_EMBEDDED_ENV_B64 = "[^"]*"')
    if not pattern.search(src):
        print("ERROR: _EMBEDDED_ENV_B64 marker not found in app.py", file=sys.stderr)
        return 3
    src = pattern.sub(lambda m: f'_EMBEDDED_ENV_B64 = "{b64}"', src, count=1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    print(f"OK: sanitized .env embedded into app.py ({len(b64)} base64 chars)")
    print(f"    blanked keys ({len(blanked)}): {', '.join(sorted(blanked))}")
    print(f"    kept keys: settings/defaults (all strategy tuning intact)")

    # DRM validation secret: stamped separately (see module docstring).
    secret = get_raw_env_value(env_path, "ALADIN_DRM_KEY")
    ok, msg = inject_drm_secret(path, secret)
    if ok:
        print(f"OK: DRM validation secret injected into _EMBEDDED_SECRET "
              f"({len(secret)} chars, from builder .env)")
    else:
        print(f"ERROR: {msg}", file=sys.stderr)
        return 4
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
