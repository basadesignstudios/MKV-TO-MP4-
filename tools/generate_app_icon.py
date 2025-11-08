"""Generate the application icon from the embedded base64 data."""
from __future__ import annotations

from pathlib import Path

from app_icon_data import write_app_icon


def main() -> None:
    target = Path(__file__).resolve().parents[1] / "app_icon.ico"
    write_app_icon(target)
    print(f"Wrote icon to {target}")


if __name__ == "__main__":
    main()
