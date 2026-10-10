"""Ask which API the worker should talk to, before config.py loads anything.

config.py reads its env file at import time, so the choice has to be made first and
handed over through WORKER_ENV_FILE. Kept out of config.py itself because the tests
and api/client.py import it, and a prompt there would block every one of them.
"""

import os
import sys
from pathlib import Path

from dotenv import dotenv_values

_ROOT = Path(__file__).resolve().parent

# Local first, so Enter is the cheap mistake: the worst a wrong local run does is
# scrape into the development database.
_TARGETS = {
    "1": ("Local", ".env"),
    "2": ("Production", ".env.production"),
}


def choose_env_file() -> None:
    """Set WORKER_ENV_FILE from an interactive choice, unless it is already set."""
    # Empty counts as unset: an exported WORKER_ENV_FILE=$UNSET_VAR would otherwise
    # skip the question and leave config.py resolving the directory itself.
    if os.environ.get("WORKER_ENV_FILE"):
        return
    # Without a terminal there is nobody to ask, and guessing could point real
    # scraping at the wrong database. An unattended run names its file explicitly.
    if not sys.stdin.isatty():
        raise SystemExit("No terminal to ask on: set WORKER_ENV_FILE (.env or .env.production)")

    available = {key: target for key, target in _TARGETS.items() if (_ROOT / target[1]).is_file()}
    if not available:
        raise SystemExit("Neither .env nor .env.production exists — copy .env.example first")

    print("Which API should the worker use?")
    for key, (label, file) in available.items():
        # Shown from the file itself, so a production file still holding a local URL
        # is visible before anything starts.
        url = dotenv_values(_ROOT / file).get("API_BASE_URL") or "(no API_BASE_URL)"
        print(f"  {key}) {label:<10} {url}")

    default = next(iter(available))
    while True:
        try:
            answer = input(f"Choose [{default}]: ").strip() or default
        except (KeyboardInterrupt, EOFError):
            print()
            raise SystemExit(0)
        if answer in available:
            os.environ["WORKER_ENV_FILE"] = available[answer][1]
            return
        print("Invalid option.")
