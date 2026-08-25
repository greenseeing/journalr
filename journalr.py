#!/usr/bin/env python3
# Journal-entry archival writer.
# Standard library only: it runs with plain python3 on a Tails live session, no
# network, no pip, no uv. Output is multi-page PDF/A-2b — see pdfa.py.
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from pdfa import (
    BLACK,
    BODY_W,
    GRAY,
    MARGIN,
    PAGE_H,
    JpegImage,
    RasterImage,
    Sheet,
    load_fonts,
    load_image,
    write_private,
)

PRODUCER = "journalr.py"
ENTRIES_DIR = Path(__file__).parent / "entries"

DATE_SIZE = 8.5
TITLE_SIZE = 20.0
TITLE_MIN_SIZE = 12.0
TITLE_HEIGHT_BUDGET = 132.0  # space the header title may occupy before it must shrink
BODY_SIZE = 10.5
BODY_LEADING = 15.0

CONTENT_TOP = 86.0  # first body baseline on a continuation page
CONTENT_BOTTOM = PAGE_H - 52  # lowest baseline body may use before a new page
FOOTER_RULE = PAGE_H - 40
FOOTER_BASE = PAGE_H - 28


def break_word(sheet: Sheet, word: str, font: str, size: float, max_w: float) -> list[str]:
    """Split a single token too wide for the column (a long URL or hash) at
    character boundaries so it never runs off the page edge."""
    pieces: list[str] = []
    current = ""
    for char in word:
        if current and sheet.width(current + char, font, size) > max_w:
            pieces.append(current)
            current = char
        else:
            current += char
    if current:
        pieces.append(current)
    return pieces


def wrap_text(sheet: Sheet, text: str, font: str, size: float, max_w: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for token in text.split():
        for word in (
            break_word(sheet, token, font, size, max_w)
            if sheet.width(token, font, size) > max_w
            else [token]
        ):
            candidate = f"{current} {word}".strip()
            if current and sheet.width(candidate, font, size) > max_w:
                lines.append(current)
                current = word
            else:
                current = candidate
    if current:
        lines.append(current)
    return lines


class Entry:
    """A journal entry laid out as flowing text and photos across as many pages
    as it needs. The cursor `y` walks down the page; each writer breaks to a new
    page the moment the next line or image would cross the content margin."""

    def __init__(self, sheet: Sheet, title: str, date_label: str) -> None:
        self.sheet = sheet
        self.title = title
        self.date_label = date_label
        self.y = self.open_header()

    def open_header(self) -> float:
        self.sheet.text(MARGIN, 56, self.date_label.upper(), "sans-bold", DATE_SIZE, GRAY)
        size = TITLE_SIZE
        lines = wrap_text(self.sheet, self.title, "sans-bold", size, BODY_W)
        while size > TITLE_MIN_SIZE and len(lines) * size * 1.3 > TITLE_HEIGHT_BUDGET:
            size -= 1
            lines = wrap_text(self.sheet, self.title, "sans-bold", size, BODY_W)
        leading = size * 1.3
        y = 84.0
        for line in lines:
            self.sheet.text(MARGIN, y, line, "sans-bold", size, BLACK)
            y += leading
        y += 4
        self.sheet.rule(MARGIN, y, BODY_W, 1.0)
        return y + 22

    def continue_page(self) -> None:
        self.sheet.new_page()
        label = f"{self.title} · continued".upper()
        size = self.sheet.fit(label, "sans-bold", BODY_W, DATE_SIZE)
        self.sheet.text(MARGIN, 56, label, "sans-bold", size, GRAY)
        self.sheet.rule(MARGIN, 64, BODY_W, 0.7, GRAY)
        self.y = CONTENT_TOP

    def reserve(self, space: float) -> None:
        if self.y + space > CONTENT_BOTTOM:
            self.continue_page()

    def body(self, lines: list[str]) -> None:
        for raw in lines:
            if not raw.strip():
                self.y += BODY_LEADING * 0.55
                continue
            for segment in wrap_text(self.sheet, raw, "sans", BODY_SIZE, BODY_W):
                self.reserve(BODY_LEADING)
                self.sheet.text(MARGIN, self.y, segment, "sans", BODY_SIZE, BLACK)
                self.y += BODY_LEADING

    def photos(self, pictures: list[RasterImage | JpegImage]) -> None:
        if not pictures:
            return
        self.y += 8
        self.reserve(30)
        self.sheet.rule(MARGIN, self.y, BODY_W, 0.7, GRAY)
        self.y += 16
        label = "1 IMAGE" if len(pictures) == 1 else f"{len(pictures)} IMAGES"
        self.sheet.text(MARGIN, self.y, label, "sans-bold", DATE_SIZE, GRAY)
        self.y += 16
        page_height = CONTENT_BOTTOM - CONTENT_TOP
        for picture in pictures:
            width = BODY_W
            height = width * picture.height / picture.width
            if height > page_height:
                height = page_height
                width = height * picture.width / picture.height
            if self.y + height > CONTENT_BOTTOM:
                self.continue_page()
            x = MARGIN + (BODY_W - width) / 2
            self.sheet.draw_image(picture, x, self.y, height)
            self.y += height + 12


def add_footers(sheet: Sheet, iso_date: str) -> None:
    pages = sheet.finished_pages + [sheet.ops]
    total = len(pages)
    saved = sheet.ops
    for number, page in enumerate(pages, 1):
        sheet.ops = page
        sheet.rule(MARGIN, FOOTER_RULE, BODY_W, 0.5)
        sheet.text(MARGIN, FOOTER_BASE, iso_date, "sans", 8, GRAY)
        marker = f"{number} / {total}"
        sheet.text(MARGIN + BODY_W - sheet.width(marker, "sans", 8), FOOTER_BASE, marker, "sans", 8, GRAY)
    sheet.ops = saved


def build_pdf(
    path: Path,
    iso_date: str,
    title: str,
    content: list[str],
    pictures: list[RasterImage | JpegImage],
) -> None:
    weekday = datetime.strptime(iso_date, "%Y-%m-%d").strftime("%A")
    sheet = Sheet(load_fonts())
    entry = Entry(sheet, title, f"{weekday} · {iso_date}")
    entry.body(content)
    entry.photos(pictures)
    add_footers(sheet, iso_date)
    write_private(
        path,
        sheet.to_pdf(f"{title} — {iso_date}", datetime.now(timezone.utc).astimezone(), PRODUCER),
    )


def ask_date() -> str:
    default = datetime.now(timezone.utc).astimezone().date().isoformat()
    while True:
        raw = input(f"[1] Date (YYYY-MM-DD) [{default}]: ").strip() or default
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
        except ValueError:
            print("    Use the format YYYY-MM-DD, e.g. 2026-08-25.")


def ask_title() -> str:
    while True:
        value = input("[2] Title: ").strip()
        if value:
            return value
        print("    Cannot be empty.")


def ask_content() -> list[str]:
    print("[3] Content — write your entry; finish with a single '.' on its own line (or Ctrl-D):")
    while True:
        lines: list[str] = []
        while True:
            try:
                line = input()
            except EOFError:
                print()
                break
            if line.strip() == ".":
                break
            lines.append(line)
        while lines and not lines[-1].strip():
            lines.pop()
        if any(line.strip() for line in lines):
            return lines
        print("    Content cannot be empty, please write something.")


def ask_images() -> list[RasterImage | JpegImage]:
    print("[4] Images — up to 3 PNG/JPEG paths (press Enter to skip or stop):")
    pictures: list[RasterImage | JpegImage] = []
    while len(pictures) < 3:
        raw = input(f"    Image {len(pictures) + 1} path: ").strip()
        if not raw:
            break
        path = Path(raw).expanduser()
        if not path.is_file():
            print("    No such file, try again.")
            continue
        try:
            print(f"    reading {path.name} ...")
            pictures.append(load_image(path))
        except SystemExit as error:  # load_image normalises every bad-file failure here
            print(f"    could not read {path.name}: {error}")
    return pictures


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "entry"


def free_path(directory: Path, stem: str) -> Path:
    """A path that does not exist yet, so a same-day, same-title entry never
    silently overwrites an earlier one."""
    path = directory / f"{stem}.pdf"
    counter = 2
    while path.exists():
        path = directory / f"{stem}-{counter}.pdf"
        counter += 1
    return path


def unrenderable(*texts: str) -> str:
    """Characters the WinAnsi/cp1252 font path cannot draw — they become '?' in
    the PDF, so warn before committing them to an archive."""
    missing = {ch for text in texts for ch in text if ch.encode("cp1252", "replace") == b"?" and ch != "?"}
    return "".join(sorted(missing))


def main() -> int:
    print(
        "Journal archival — runs fully offline, writes one PDF/A-2b file per entry, keeps nothing else.\n"
    )
    iso_date = ask_date()
    title = ask_title()
    content = ask_content()
    pictures = ask_images()
    words = sum(len(line.split()) for line in content)
    print(f"\n    {words} word(s), {len(pictures)} image(s) captured.")
    lost = unrenderable(title, *content)
    if lost:
        print(f"    Note: these characters cannot be rendered and will show as '?': {lost}")
    if input("    Generate PDF? [Y/n] ").strip().lower() not in ("", "y", "yes"):
        print("Aborted, nothing written.")
        return 1
    ENTRIES_DIR.mkdir(mode=0o700, exist_ok=True)
    os.chmod(ENTRIES_DIR, 0o700)  # mkdir's mode is ignored when the dir already exists
    path = free_path(ENTRIES_DIR, f"{iso_date}_{slug(title)}")
    build_pdf(path, iso_date, title, content, pictures)
    print(f"\nWrote {path} (PDF/A-2b, permissions 600).")
    print("Keep it on an encrypted volume, and clear the print queue after printing.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nAborted, nothing written.")
        sys.exit(130)
