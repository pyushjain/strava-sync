"""Run CI Drive ingestion with in-memory Doppler secrets and quiet child output."""

import json
import os
import subprocess
import sys
import urllib.request


SECRET_NAMES = ("SUPABASE_DB_URL", "GOOGLE_SERVICE_ACCOUNT_JSON", "DRIVE_FOLDER_ID")
DOPPLER_URL = "https://api.doppler.com/v3/configs/config/secrets/download?format=json"


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the authorization header to a redirect destination.
        return None


def main():
    token = os.environ.pop("DOPPLER_SERVICE_TOKEN", "")
    if not token:
        print("Doppler service token is missing.", file=sys.stderr)
        return 1
    try:
        request = urllib.request.Request(
            DOPPLER_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        opener = urllib.request.build_opener(NoRedirects())
        with opener.open(request, timeout=30) as response:
            secrets = json.load(response)
        if not all(isinstance(secrets.get(name), str) and secrets[name].strip()
                   for name in SECRET_NAMES):
            print("Doppler configuration is missing required ingestion variables.", file=sys.stderr)
            return 1
        environment = os.environ.copy()
        environment.update({name: secrets[name] for name in SECRET_NAMES})
        # No secret files, shell interpolation, CLI arguments, or CI output exports.
        # Suppress child output entirely, including tracebacks and file identifiers.
        result = subprocess.run(
            [sys.executable, "-m", "server.ingest", "--source", "drive"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=25 * 60,
            check=False,
        )
    except Exception:
        # HTTP/JSON/process exceptions can include credentials or response content.
        print("Doppler-backed ingestion failed; check configuration and service access.", file=sys.stderr)
        return 1
    if result.returncode:
        print("Drive ingestion failed; check configuration and service access.", file=sys.stderr)
        return 1
    print("Drive ingestion completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
