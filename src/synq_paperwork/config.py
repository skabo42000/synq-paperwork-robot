"""Load settings from .env - in ONE place, so every part of the project uses the same key.

This project uses a SEPARATE Google account's Gemini key (GOOGLE_API_KEY_DEV), so test runs never
compete for the free daily quota with GOOGLE_API_KEY, which backs live client chat widgets.
The Gemini libraries read GOOGLE_API_KEY from the environment, so we point it at the dev key here,
for this Python process only - nothing else on the machine is affected.
"""

import os

from dotenv import load_dotenv


def load_settings() -> None:
    load_dotenv()
    if os.getenv("GOOGLE_API_KEY_DEV"):
        os.environ["GOOGLE_API_KEY"] = os.environ["GOOGLE_API_KEY_DEV"]
