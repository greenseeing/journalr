import struct
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pdfa as m

MOMENT = datetime(2026, 8, 25, 9, 0, 0, tzinfo=timezone(timedelta(hours=2)))


def baseline_jpeg(width: int, height: int, components: int, exif: bytes = b"") -> bytes:
    """Enough of a JPEG for the marker parser: SOI, APP0, optional Exif, an SOF0 frame."""
    sof = struct.pack(">HBHHB", 8 + 3 * components, 8, height, width, components)
    sof += bytes([1, 0x11, 0]) * components
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    return b"\xff\xd8" + app0 + exif + b"\xff\xc0" + sof


def exif_app1(orientation: int) -> bytes:
    tiff = (
        b"II"
        + struct.pack("<H", 42)
        + struct.pack("<I", 8)
        + struct.pack("<H", 1)
        + struct.pack("<H", 0x0112)
        + struct.pack("<H", 3)
        + struct.pack("<I", 1)
        + struct.pack("<H", orientation)
        + b"\x00\x00"
        + struct.pack("<I", 0)
    )
    payload = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


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


class JpegTests(unittest.TestCase):
    def written(self, data: bytes) -> Path:
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(data)
        tmp.close()
        return Path(tmp.name)

    def test_frame_marker_gives_width_height_and_colorspace(self) -> None:
        jpeg = m.JpegImage(self.written(baseline_jpeg(640, 480, 3)))
        self.assertEqual((jpeg.width, jpeg.height), (640, 480))
        self.assertEqual(jpeg.colorspace, "/DeviceRGB")

    def test_single_component_jpeg_is_grayscale(self) -> None:
        jpeg = m.JpegImage(self.written(baseline_jpeg(10, 20, 1)))
        self.assertEqual(jpeg.colorspace, "/DeviceGray")

    def test_cmyk_jpeg_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            m.JpegImage(self.written(baseline_jpeg(10, 10, 4)))

    def test_non_jpeg_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            m.JpegImage(self.written(b"not a jpeg"))

    def test_payload_is_the_untouched_bytes_for_dctdecode(self) -> None:
        data = baseline_jpeg(4, 4, 3)
        jpeg = m.JpegImage(self.written(data))
        self.assertEqual(jpeg.pdf_payload(), data)
        self.assertIn("/DCTDecode", jpeg.pdf_dict())

    def test_load_image_picks_the_decoder_by_signature(self) -> None:
        jpeg = self.written(baseline_jpeg(4, 4, 3))
        png = self.written(make_png(1, 1, [b"\x00" + bytes((1, 2, 3))]))
        self.assertIsInstance(m.load_image(jpeg), m.JpegImage)
        self.assertIsInstance(m.load_image(png), m.RasterImage)

    def test_zero_dimension_jpeg_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            m.JpegImage(self.written(baseline_jpeg(0, 10, 3)))

    def test_load_image_turns_a_corrupt_file_into_a_clean_systemexit(self) -> None:
        truncated_jpeg = self.written(b"\xff\xd8\xff\xc0\x00")  # SOF marker, then nothing
        with self.assertRaises(SystemExit):
            m.load_image(truncated_jpeg)
        broken_png = self.written(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)  # header, garbage body
        with self.assertRaises(SystemExit):
            m.load_image(broken_png)

    def test_exif_orientation_is_read_and_swaps_display_dimensions(self) -> None:
        jpeg = m.JpegImage(self.written(baseline_jpeg(400, 200, 3, exif_app1(6))))
        self.assertEqual(jpeg.orientation, 6)
        self.assertEqual((jpeg.display_width, jpeg.display_height), (200, 400))

    def test_missing_exif_defaults_to_upright(self) -> None:
        jpeg = m.JpegImage(self.written(baseline_jpeg(400, 200, 3)))
        self.assertEqual(jpeg.orientation, 1)
        self.assertEqual((jpeg.display_width, jpeg.display_height), (400, 200))


class Stub:
    def __init__(self, orientation: int, width: int, height: int) -> None:
        self.orientation = orientation
        self.width = width
        self.height = height

    @property
    def display_width(self) -> int:
        return self.height if self.orientation >= 5 else self.width

    @property
    def display_height(self) -> int:
        return self.width if self.orientation >= 5 else self.height


class PlacementTests(unittest.TestCase):
    def last_op(self, orientation: int, width: int, height: int) -> str:
        sheet = m.Sheet(m.load_fonts())
        sheet.draw_image(Stub(orientation, width, height), 10, 20, 100)
        return sheet.ops[-1]

    def test_upright_uses_the_plain_scale_matrix(self) -> None:
        # 200x100 landscape at height 100 -> width 200; bottom = 841.89-20-100
        self.assertEqual(self.last_op(1, 200, 100), "q 200.00 0.00 0.00 100.00 10.00 721.89 cm /Im1 Do Q")

    def test_ninety_clockwise_rotates_and_swaps(self) -> None:
        # native 400x200, orientation 6 -> box 50x100, matrix (0,-H,W,0,left,bottom+H)
        self.assertEqual(self.last_op(6, 400, 200), "q 0.00 -100.00 50.00 0.00 10.00 821.89 cm /Im1 Do Q")

    def test_one_eighty_flips_both_axes(self) -> None:
        self.assertEqual(self.last_op(3, 200, 100), "q -200.00 0.00 0.00 -100.00 210.00 821.89 cm /Im1 Do Q")


class MultiPageTests(unittest.TestCase):
    def three_pages(self) -> bytes:
        sheet = m.Sheet(m.load_fonts())
        sheet.text(m.MARGIN, 100, "page one")
        sheet.new_page()
        sheet.text(m.MARGIN, 100, "page two")
        sheet.new_page()
        sheet.text(m.MARGIN, 100, "page three")
        return sheet.to_pdf("Journal", MOMENT, "journalr.py")

    def test_pages_object_counts_every_page(self) -> None:
        data = self.three_pages()
        self.assertIn(b"/Count 3", data)
        self.assertEqual(data.count(b"/Type /Page /Parent"), 3)
        self.assertEqual(data.split(b"/Kids [")[1].split(b"]")[0].count(b"0 R"), 3)

    def test_each_page_carries_its_own_content_stream(self) -> None:
        data = self.three_pages()
        streams = []
        for match in data.split(b"/Filter /FlateDecode /Length")[1:]:
            body = match.split(b"stream\n", 1)[1]
            streams.append(zlib.decompressobj().decompress(body))
        joined = b"".join(streams)
        for text in (b"(page one)", b"(page two)", b"(page three)"):
            self.assertIn(text, joined)

    def test_xref_offsets_point_at_their_objects(self) -> None:
        data = self.three_pages()
        start = int(data.rsplit(b"startxref", 1)[1].split()[0])
        for number, row in enumerate(data[start:].split(b"\n")[3:], 1):
            if not row.strip().endswith(b"n"):
                break
            self.assertTrue(data[int(row.split()[0]):].startswith(b"%d 0 obj" % number))

    def test_only_the_page_that_uses_an_image_declares_the_xobject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "x.png"
            png.write_bytes(make_png(1, 1, [b"\x00" + bytes((9, 9, 9))]))
            sheet = m.Sheet(m.load_fonts())
            sheet.text(m.MARGIN, 100, "text only")
            sheet.new_page()
            sheet.draw_image(m.load_image(png), m.MARGIN, 100, 30)
            data = sheet.to_pdf("Journal", MOMENT, "journalr.py")
        import re

        pages = re.findall(rb"<< /Type /Page /Parent.*?/Contents \d+ 0 R >>", data, re.S)
        self.assertEqual(len(pages), 2)
        self.assertNotIn(b"/XObject", pages[0])
        self.assertIn(b"/XObject", pages[1])


if __name__ == "__main__":
    unittest.main()
