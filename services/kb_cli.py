"""Entry point that runs the kleinanzeigen-bot CLI with local runtime patches.

Used instead of `python -m kleinanzeigen_bot` so that downloads survive the
redesigned ad-page layout (see services/kb_patches.py).
"""
import sys

from services.kb_patches import apply_patches

if __name__ == "__main__":
    apply_patches()
    # Importing the bot's __main__ runs its CLI loop (incl. captcha restart handling).
    sys.argv[0] = "kleinanzeigen-bot"
    import kleinanzeigen_bot.__main__  # noqa: F401
