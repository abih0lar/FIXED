# --- START OF FILE keygen.py ---

import os
import base64
from cryptography.fernet import Fernet
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

_kg_dir = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(_kg_dir, "data", ".env")):
    load_dotenv(os.path.join(_kg_dir, "data", ".env"))
else:
    load_dotenv()

# Secure injection of the master secret
raw_secret = os.getenv("ALADIN_DRM_KEY", "").replace("'", "").replace('"', '').strip()
if not raw_secret:
    print("CRITICAL ERROR: 'ALADIN_DRM_KEY' missing from .env file.")
    exit(1)

MASTER_SECRET = raw_secret.encode('utf-8')
CRYPTO_KEY = base64.urlsafe_b64encode(MASTER_SECRET.ljust(32)[:32])

print("Encryption engine armed. License key material loaded.")


def generate_license(days_valid: int):
    f = Fernet(CRYPTO_KEY)
    expiry_date = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=days_valid)
    expiry_str = expiry_date.strftime("%Y-%m-%d %H:%M:%S")

    payload = f"ALADIN_PRO|{expiry_str}".encode('utf-8')
    license_key = f.encrypt(payload).decode('utf-8')

    print("\n==================================================")
    print(f"  SUCCESS: license generated for {days_valid} days")
    print(f"  Expiration (UTC): {expiry_str}")
    print("==================================================")

    with open("CLIENT_LICENSE_KEY.txt", "w") as text_file:
        text_file.write(license_key)

    print("\n  License key saved to: CLIENT_LICENSE_KEY.txt\n")


if __name__ == "__main__":
    print("ALADIN PRO - SECURE LICENSE GENERATOR")
    try:
        days = int(input("How many days should this license be valid for? (e.g. 7 or 30): "))
        generate_license(days)
    except ValueError:
        print("Please enter a valid number.")
