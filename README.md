# journalr

A tiny, fully offline journal-entry archiver. It asks four questions — date,
title, body, up to three photos — and writes one **PDF/A-2b** file per entry:
the ISO archival profile, with fonts embedded, an sRGB output intent, and XMP
metadata, so an entry written today still renders identically decades from now.

It is built for a threat model where the machine is disposable and the archive
is not: it runs on a [Tails](https://tails.net) live session with **no network,
no pip, no uv** — just `python3` and the standard library. The PDF/A writer
(`pdfa.py`) is hand-rolled from scratch; there is no reportlab, no Pillow, no
external dependency at all.

## Features

- **Standard library only** — no third-party packages, nothing to install.
- **PDF/A-2b output** — embedded TrueType fonts, embedded sRGB ICC profile,
  XMP metadata, deterministic file ID. Archival-grade and self-contained.
- **PNG and JPEG photos** — JPEGs embedded verbatim via `DCTDecode` (no slow
  pure-Python decode); PNGs decoded and flattened over white. EXIF orientation
  is honoured so phone photos stay upright.
- **Multi-page flow** — body text and images reflow across as many pages as the
  entry needs, with running footers.
- **Privacy-minded** — every file is written `0600`, the `entries/` directory is
  created `0700`, and the tool keeps nothing else on disk.

## Requirements

- Python 3.9 or newer (standard library only).
- A DejaVu or Liberation TrueType font on the system for embedding
  (`fonts-dejavu-core` or `fonts-liberation` on Debian; both ship on Tails).

## Usage

```
python3 journalr.py
```

Answer the prompts. The entry is written to `entries/<date>_<slug>.pdf`. A
same-day, same-title entry is never overwritten — it gets a numeric suffix.

After printing, clear the print queue and keep the PDF on an encrypted volume.

## Tests

```
python3 -m unittest discover -s tests -t .
```

## License

[MIT](LICENSE).
