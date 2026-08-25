import struct
import tempfile
import unittest
import zlib
from pathlib import Path

import journalr as j
import pdfa


def make_png(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return len(body).to_bytes(4, "big") + kind + body + zlib.crc32(kind + body).to_bytes(4, "big")

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


class SlugTests(unittest.TestCase):
    def test_spaces_and_punctuation_collapse_to_hyphens(self) -> None:
        self.assertEqual(j.slug("My First Entry: A Day!"), "my-first-entry-a-day")

    def test_a_title_with_no_usable_characters_falls_back(self) -> None:
        self.assertEqual(j.slug("!!!"), "entry")


class WrapTests(unittest.TestCase):
    def test_long_text_breaks_into_lines_within_the_width(self) -> None:
        sheet = pdfa.Sheet(pdfa.load_fonts())
        lines = j.wrap_text(sheet, "word " * 60, "sans", 10.5, pdfa.BODY_W)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(sheet.width(line, "sans", 10.5), pdfa.BODY_W)

    def test_an_unbreakable_token_is_split_at_characters(self) -> None:
        sheet = pdfa.Sheet(pdfa.load_fonts())
        token = "x" * 500  # a hash or URL with no spaces
        lines = j.wrap_text(sheet, token, "sans", 10.5, pdfa.BODY_W)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(sheet.width(line, "sans", 10.5), pdfa.BODY_W)
        self.assertEqual("".join(lines), token)  # no characters lost


class GuardTests(unittest.TestCase):
    def test_free_path_never_overwrites_an_existing_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = j.free_path(directory, "2026-08-25_note")
            first.write_bytes(b"x")
            second = j.free_path(directory, "2026-08-25_note")
            second.write_bytes(b"y")
            third = j.free_path(directory, "2026-08-25_note")
        self.assertEqual(first.name, "2026-08-25_note.pdf")
        self.assertEqual(second.name, "2026-08-25_note-2.pdf")
        self.assertEqual(third.name, "2026-08-25_note-3.pdf")

    def test_unrenderable_reports_only_characters_that_become_placeholders(self) -> None:
        lost = j.unrenderable("café ✓", "日本語")
        self.assertIn("✓", lost)
        self.assertIn("日", lost)
        self.assertNotIn("é", lost)  # é is representable in cp1252
        self.assertEqual(j.unrenderable("plain ascii ?"), "")  # a literal ? is not a loss


class BuildPdfTests(unittest.TestCase):
    def test_long_content_flows_onto_several_pages(self) -> None:
        content = [f"Paragraph {i}. " + "sentence here. " * 40 for i in range(30)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.pdf"
            j.build_pdf(path, "2026-08-25", "A Long Day", content, [])
            data = path.read_bytes()
        self.assertTrue(data.startswith(b"%PDF-1.7"))
        self.assertIn(b"<pdfaid:part>2</pdfaid:part>", data)
        count = int(data.split(b"/Count ")[1].split(b" ")[0])
        self.assertGreater(count, 1)

    def test_the_title_and_date_reach_the_document_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.pdf"
            j.build_pdf(path, "2026-08-25", "Seaside", ["a short note"], [])
            data = path.read_bytes()
        self.assertIn(b"Seaside", data)  # plain text in the XMP dc:title
        self.assertIn(b"2026-08-25", data)

    def test_the_file_is_written_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.pdf"
            j.build_pdf(path, "2026-08-25", "Private", ["secret"], [])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_images_are_embedded_and_the_footer_numbers_every_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "p.png"
            png.write_bytes(make_png(2, 1, [b"\x00" + bytes((1, 2, 3, 4, 5, 6))]))
            path = Path(tmp) / "out.pdf"
            content = [f"line {i}" for i in range(120)]
            j.build_pdf(path, "2026-08-25", "With Photos", content, [pdfa.load_image(png)])
            data = path.read_bytes()
        count = int(data.split(b"/Count ")[1].split(b" ")[0])
        self.assertIn(b"/Subtype /Image", data)
        streams = []
        for match in data.split(b"/Filter /FlateDecode /Length")[1:]:
            streams.append(zlib.decompressobj().decompress(match.split(b"stream\n", 1)[1]))
        joined = b"".join(streams)
        self.assertIn(f"({count} / {count})".encode(), joined)
        self.assertIn(b"(1 / %d)" % count, joined)


if __name__ == "__main__":
    unittest.main()
