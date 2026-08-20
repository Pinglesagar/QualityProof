"""Verify the browser-job image contains every workflow runtime tool."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright


def verify_runtime(*, launch_browser: bool) -> None:
    missing = [tool for tool in ("pytest", "ruff") if shutil.which(tool) is None]
    if missing:
        raise RuntimeError(f"missing browser-job tools: {', '.join(missing)}")
    with sync_playwright() as playwright:
        executable = Path(playwright.chromium.executable_path)
        if not executable.is_file():
            raise RuntimeError(f"Chromium executable is missing: {executable}")
        if launch_browser:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content("<title>QualityProof smoke</title>")
            if page.title() != "QualityProof smoke":
                raise RuntimeError("Chromium page smoke failed")
            browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-browser", action="store_true")
    args = parser.parse_args()
    verify_runtime(launch_browser=args.launch_browser)
    print("browser-job runtime smoke passed")


if __name__ == "__main__":
    main()
