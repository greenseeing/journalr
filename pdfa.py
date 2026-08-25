# Minimal PDF/A-2b writer, standard library only, shared by the recovery-sheet
# scripts. PDF/A is the archival profile: fonts embedded, sRGB output intent, XMP
# metadata, so a file written today still renders the same decades from now.
from __future__ import annotations

import base64
import hashlib
import os
import struct
import zlib
from datetime import datetime
from pathlib import Path

PAGE_W = 595.28
PAGE_H = 841.89
MARGIN = 48.0
BODY_W = PAGE_W - 2 * MARGIN

BLACK = (0.07, 0.07, 0.07)
GRAY = (0.42, 0.44, 0.47)
RULE = (0.30, 0.30, 0.32)
DANGER = (0.55, 0.05, 0.05)

FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/TTF",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/liberation2",
    "/usr/share/fonts/liberation",
)
# PDF/A forbids the base-14 fonts, so a real TTF must be found and embedded.
FONT_FILES = {
    "sans": ("DejaVuSans.ttf", "LiberationSans-Regular.ttf"),
    "sans-bold": ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"),
    "mono-bold": ("DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf"),
}

# sRGB v4 profile generated with lcms2 ("no copyright, use freely"), embedded so the
# PDF/A output intent needs no system ICC files.
SRGB_ICC_B64 = (
    "AAACTGxjbXMEQAAAbW50clJHQiBYWVogB+oACAAYAAgAJwA7YWNzcEFQUEwAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAPbWAAEAAAAA0y1sY21zAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAALZGVzYwAAAQgAAAA2Y3BydAAAAUAAAABMd3RwdAAAAYwA"
    "AAAUY2hhZAAAAaAAAAAsclhZWgAAAcwAAAAUYlhZWgAAAeAAAAAUZ1hZWgAAAfQAAAAUclRSQwAA"
    "AggAAAAgZ1RSQwAAAggAAAAgYlRSQwAAAggAAAAgY2hybQAAAigAAAAkbWx1YwAAAAAAAAABAAAA"
    "DGVuVVMAAAAaAAAAHABzAFIARwBCACAAYgB1AGkAbAB0AC0AaQBuAABtbHVjAAAAAAAAAAEAAAAM"
    "ZW5VUwAAADAAAAAcAE4AbwAgAGMAbwBwAHkAcgBpAGcAaAB0ACwAIAB1AHMAZQAgAGYAcgBlAGUA"
    "bAB5WFlaIAAAAAAAAPbWAAEAAAAA0y1zZjMyAAAAAAABDEIAAAXe///zJQAAB5MAAP2Q///7of//"
    "/aIAAAPcAADAblhZWiAAAAAAAABvoAAAOPUAAAOQWFlaIAAAAAAAACSfAAAPhAAAtsNYWVogAAAA"
    "AAAAYpcAALeHAAAY2XBhcmEAAAAAAAMAAAACZmYAAPKnAAANWQAAE9AAAApbY2hybQAAAAAAAwAA"
    "AACj1wAAVHsAAEzNAACZmgAAJmYAAA9c"
)


class EmbeddedFont:
    """A TrueType file read straight from disk: metrics for layout, bytes for embedding."""

    def __init__(self, path: Path) -> None:
        self.data = path.read_bytes()
        self.tables = {}
        for i in range(self.u16(4)):
            tag, _, offset, length = struct.unpack(">4sIII", self.data[12 + 16 * i : 28 + 16 * i])
            self.tables[tag.decode("latin-1")] = (offset, length)
        head = self.tables["head"][0]
        scale = 1000 / self.u16(head + 18)
        self.bbox = [round(self.i16(head + 36 + 2 * i) * scale) for i in range(4)]
        hhea = self.tables["hhea"][0]
        self.ascent = round(self.i16(hhea + 4) * scale)
        self.descent = round(self.i16(hhea + 6) * scale)
        post = self.tables["post"][0]
        self.italic_angle = round(struct.unpack(">i", self.data[post + 4 : post + 8])[0] / 65536)
        self.fixed_pitch = self.u32(post + 12) != 0
        os2, _ = self.tables["OS/2"]
        weight = self.u16(os2 + 4)
        self.cap_height = (
            round(self.i16(os2 + 88) * scale)
            if self.u16(os2) >= 2
            else round(0.7 * self.ascent)
        )
        self.stem_v = round(50 + (weight / 65) ** 2)
        self.name = self.postscript_name() or path.stem
        self.widths = self.winansi_widths(scale)

    def u16(self, at: int) -> int:
        return struct.unpack(">H", self.data[at : at + 2])[0]

    def i16(self, at: int) -> int:
        return struct.unpack(">h", self.data[at : at + 2])[0]

    def u32(self, at: int) -> int:
        return struct.unpack(">I", self.data[at : at + 4])[0]

    def postscript_name(self) -> str:
        table, _ = self.tables["name"]
        strings = table + self.u16(table + 4)
        for i in range(self.u16(table + 2)):
            record = table + 6 + 12 * i
            platform, _, _, name_id, length, offset = struct.unpack(
                ">HHHHHH", self.data[record : record + 12]
            )
            if name_id != 6:
                continue
            raw = self.data[strings + offset : strings + offset + length]
            return raw.decode("utf-16-be" if platform == 3 else "latin-1", "ignore")
        return ""

    def glyph_map(self) -> dict[int, int]:
        """Unicode to glyph id, from the format 4 subtable every modern TTF carries."""
        table, _ = self.tables["cmap"]
        subtable = 0
        for i in range(self.u16(table + 2)):
            platform, encoding, offset = struct.unpack(
                ">HHI", self.data[table + 4 + 8 * i : table + 12 + 8 * i]
            )
            if (platform, encoding) in ((3, 1), (0, 3), (0, 4), (0, 6)):
                subtable = table + offset
                break
        if not subtable or self.u16(subtable) != 4:
            raise SystemExit(f"{self.name}: no usable Unicode cmap, cannot embed it.")
        segments = self.u16(subtable + 6) // 2
        ends = [self.u16(subtable + 14 + 2 * i) for i in range(segments)]
        starts = [self.u16(subtable + 16 + 2 * segments + 2 * i) for i in range(segments)]
        deltas = [self.i16(subtable + 16 + 4 * segments + 2 * i) for i in range(segments)]
        range_offsets_at = subtable + 16 + 6 * segments
        mapping = {}
        for i in range(segments):
            range_offset = self.u16(range_offsets_at + 2 * i)
            for code in range(starts[i], min(ends[i], 0xFFFF) + 1):
                if range_offset:
                    at = range_offsets_at + 2 * i + range_offset + 2 * (code - starts[i])
                    glyph = self.u16(at)
                    if glyph:
                        glyph = (glyph + deltas[i]) & 0xFFFF
                else:
                    glyph = (code + deltas[i]) & 0xFFFF
                if glyph:
                    mapping[code] = glyph
        return mapping

    def winansi_widths(self, scale: float) -> list[int]:
        hmtx, _ = self.tables["hmtx"]
        pairs = self.u16(self.tables["hhea"][0] + 34)
        glyphs = self.glyph_map()

        def advance(glyph: int) -> int:
            index = min(glyph, pairs - 1)
            return round(self.u16(hmtx + 4 * index) * scale)

        widths = []
        for code in range(32, 256):
            try:
                char = bytes([code]).decode("cp1252")
            except UnicodeDecodeError:
                widths.append(0)
                continue
            widths.append(advance(glyphs.get(ord(char), 0)))
        return widths

    def width(self, text: str, size: float) -> float:
        total = 0
        for byte in text.encode("cp1252", "replace"):
            total += self.widths[byte - 32] if byte >= 32 else 0
        return total * size / 1000


def find_font(names: tuple[str, ...]) -> EmbeddedFont:
    for directory in FONT_DIRS:
        for name in names:
            path = Path(directory) / name
            if path.is_file():
                return EmbeddedFont(path)
    raise SystemExit(
        f"No font found for {names}. PDF/A must embed its fonts — install "
        "fonts-dejavu-core (Debian, and present on Tails) or fonts-liberation."
    )


def load_fonts() -> dict[str, EmbeddedFont]:
    return {key: find_font(names) for key, names in FONT_FILES.items()}


class JpegImage:
    """A JPEG embedded verbatim: PDF's DCTDecode reads the compressed bytes directly,
    so a phone photo needs no slow pure-Python decode. Colour is declared as
    DeviceRGB/Gray and rides the document's sRGB output intent — an embedded ICC
    profile (Adobe RGB, Display P3) is ignored, which is fine for phone cameras."""

    def __init__(self, path: Path) -> None:
        self.data = path.read_bytes()
        if self.data[:2] != b"\xff\xd8":
            raise SystemExit(f"{path}: not a JPEG file.")
        try:
            self.width, self.height, components = self.frame()
            self.orientation = self.exif_orientation()
        except (struct.error, IndexError) as error:
            raise SystemExit(f"{path}: truncated or corrupt JPEG ({error}).")
        if self.width == 0 or self.height == 0:
            raise SystemExit(f"{path}: JPEG reports a zero dimension.")
        self.colorspace = {1: "/DeviceGray", 3: "/DeviceRGB"}.get(components, "")
        if not self.colorspace:
            raise SystemExit(
                f"{path}: {components}-component JPEG (likely CMYK) is not supported; "
                "re-save it as RGB."
            )

    def frame(self) -> tuple[int, int, int]:
        sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        at = 2
        while at + 1 < len(self.data):
            if self.data[at] != 0xFF:
                at += 1
                continue
            marker = self.data[at + 1]
            at += 2
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
                continue
            length = struct.unpack(">H", self.data[at : at + 2])[0]
            if marker in sof:
                height, width = struct.unpack(">HH", self.data[at + 3 : at + 7])
                return width, height, self.data[at + 7]
            at += length
        raise SystemExit("JPEG has no start-of-frame marker.")

    def exif_orientation(self) -> int:
        """The EXIF Orientation tag (1-8); 1 if absent. Phone cameras store the
        sensor's landscape pixels plus this tag, so honouring it keeps portrait
        photos upright."""
        at = 2
        while at + 4 < len(self.data):
            if self.data[at] != 0xFF:
                at += 1
                continue
            marker = self.data[at + 1]
            at += 2
            if marker == 0xDA:  # start of scan — no metadata past here
                break
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
                continue
            length = struct.unpack(">H", self.data[at : at + 2])[0]
            segment = self.data[at + 2 : at + length]
            if marker == 0xE1 and segment[:6] == b"Exif\x00\x00":
                return tiff_orientation(segment[6:])
            at += length
        return 1

    @property
    def display_width(self) -> int:
        return self.height if self.orientation >= 5 else self.width

    @property
    def display_height(self) -> int:
        return self.width if self.orientation >= 5 else self.height

    def pdf_dict(self) -> str:
        return f"/ColorSpace {self.colorspace} /BitsPerComponent 8 /Filter /DCTDecode"

    def pdf_payload(self) -> bytes:
        return self.data


def tiff_orientation(tiff: bytes) -> int:
    try:
        order = {b"II": "<", b"MM": ">"}.get(tiff[:2], "")
        if not order:
            return 1
        ifd = struct.unpack(order + "I", tiff[4:8])[0]
        for i in range(struct.unpack(order + "H", tiff[ifd : ifd + 2])[0]):
            entry = ifd + 2 + 12 * i
            if struct.unpack(order + "H", tiff[entry : entry + 2])[0] == 0x0112:
                value = struct.unpack(order + "H", tiff[entry + 8 : entry + 10])[0]
                return value if 1 <= value <= 8 else 1
    except (struct.error, IndexError):
        return 1
    return 1


class RasterImage:
    """A PNG decoded to opaque RGB rows — PDF has no PNG filters, and alpha over
    white keeps the page free of transparency."""

    def __init__(self, path: Path) -> None:
        data = path.read_bytes()
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            raise SystemExit(f"{path}: not a PNG file.")
        chunks: dict[str, bytes] = {}
        pixels = bytearray()
        at = 8
        while at < len(data):
            length, kind = struct.unpack(">I4s", data[at : at + 8])
            body = data[at + 8 : at + 8 + length]
            at += 12 + length
            if kind == b"IDAT":
                pixels += body
            else:
                chunks[kind.decode("latin-1")] = body
        self.width, self.height, depth, color_type, _, _, interlace = struct.unpack(
            ">IIBBBBB", chunks["IHDR"]
        )
        if depth != 8 or interlace or color_type not in (0, 2, 3, 4, 6):
            raise SystemExit(
                f"{path}: only 8-bit non-interlaced PNGs are supported "
                f"(got depth {depth}, colour type {color_type}, interlace {interlace})."
            )
        if self.width == 0 or self.height == 0:
            raise SystemExit(f"{path}: PNG reports a zero dimension.")
        self.orientation = 1  # PNG carries no EXIF orientation
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
        rows = self.unfilter(zlib.decompress(bytes(pixels)), channels)
        self.rgb = self.to_rgb(rows, color_type, channels, chunks)

    def unfilter(self, raw: bytes, channels: int) -> list[bytearray]:
        stride = self.width * channels
        rows, previous, at = [], bytearray(stride), 0
        for _ in range(self.height):
            method = raw[at]
            row = bytearray(raw[at + 1 : at + 1 + stride])
            at += 1 + stride
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                up = previous[i]
                corner = previous[i - channels] if i >= channels else 0
                if method == 1:
                    row[i] = (row[i] + left) & 0xFF
                elif method == 2:
                    row[i] = (row[i] + up) & 0xFF
                elif method == 3:
                    row[i] = (row[i] + (left + up) // 2) & 0xFF
                elif method == 4:
                    estimate = left + up - corner
                    de_left = abs(estimate - left)
                    de_up = abs(estimate - up)
                    de_corner = abs(estimate - corner)
                    if de_left <= de_up and de_left <= de_corner:
                        row[i] = (row[i] + left) & 0xFF
                    elif de_up <= de_corner:
                        row[i] = (row[i] + up) & 0xFF
                    else:
                        row[i] = (row[i] + corner) & 0xFF
            rows.append(row)
            previous = row
        return rows

    def to_rgb(
        self, rows: list[bytearray], color_type: int, channels: int, chunks: dict[str, bytes]
    ) -> bytes:
        palette = chunks.get("PLTE", b"")
        alpha_map = chunks.get("tRNS", b"")
        out = bytearray()
        for row in rows:
            for i in range(0, len(row), channels):
                pixel = row[i : i + channels]
                if color_type == 3:
                    index = pixel[0]
                    red, green, blue = palette[3 * index : 3 * index + 3]
                    alpha = alpha_map[index] if index < len(alpha_map) else 255
                elif color_type in (0, 4):
                    red = green = blue = pixel[0]
                    alpha = pixel[1] if color_type == 4 else 255
                else:
                    red, green, blue = pixel[0], pixel[1], pixel[2]
                    alpha = pixel[3] if color_type == 6 else 255
                if alpha == 255:
                    out += bytes((red, green, blue))
                else:
                    blend = 255 - alpha
                    out += bytes(
                        (value * alpha + 255 * blend) // 255 for value in (red, green, blue)
                    )
        return bytes(out)

    @property
    def display_width(self) -> int:
        return self.width

    @property
    def display_height(self) -> int:
        return self.height

    def pdf_dict(self) -> str:
        return "/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode"

    def pdf_payload(self) -> bytes:
        return zlib.compress(self.rgb, 9)


def load_image(path: Path) -> RasterImage | JpegImage:
    """The single boundary where an image file is parsed; every malformed-file
    failure is normalised to SystemExit so callers need only handle that."""
    with open(path, "rb") as fh:
        signature = fh.read(2)
    try:
        return JpegImage(path) if signature == b"\xff\xd8" else RasterImage(path)
    except (struct.error, zlib.error, KeyError, IndexError, ValueError) as error:
        raise SystemExit(f"{path}: unreadable or corrupt image ({error}).")


class Sheet:
    def __init__(self, fonts: dict[str, EmbeddedFont]) -> None:
        self.fonts = fonts
        self.ops: list[str] = []
        self.finished_pages: list[list[str]] = []
        self.images: list[RasterImage | JpegImage] = []

    def new_page(self) -> None:
        """Close the current page and start a fresh one; ops written after this land
        on the new page."""
        self.finished_pages.append(self.ops)
        self.ops = []

    def width(self, text: str, font: str, size: float) -> float:
        return self.fonts[font].width(text, size)

    def font_id(self, font: str) -> str:
        return f"F{list(self.fonts).index(font) + 1}"

    def fit(self, text: str, font: str, max_w: float, size: float) -> float:
        while size > 5.5 and self.width(text, font, size) > max_w:
            size -= 0.25
        return size

    def text(
        self,
        x: float,
        y: float,
        text: str,
        font: str = "sans",
        size: float = 10,
        color: tuple[float, float, float] = BLACK,
    ) -> None:
        self.ops.append(
            f"BT {color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg "
            f"/{self.font_id(font)} {size:.2f} Tf 1 0 0 1 {x:.2f} {PAGE_H - y:.2f} Tm "
            f"{escape(text)} Tj ET"
        )

    def text_centered(self, cx: float, y: float, text: str, **kw) -> None:
        size = kw.get("size", 10)
        self.text(cx - self.width(text, kw.get("font", "sans"), size) / 2, y, text, **kw)

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        color: tuple[float, float, float] = RULE,
        line: float = 0.7,
    ) -> None:
        self.ops.append(
            f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG {line:.2f} w "
            f"{x:.2f} {PAGE_H - y - h:.2f} {w:.2f} {h:.2f} re S"
        )

    def rule(self, x: float, y: float, w: float, line: float = 0.7, color=RULE) -> None:
        self.ops.append(
            f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG {line:.2f} w "
            f"{x:.2f} {PAGE_H - y:.2f} m {x + w:.2f} {PAGE_H - y:.2f} l S"
        )

    def draw_image(self, picture: RasterImage | JpegImage, x: float, y: float, height: float) -> float:
        """Place an already-decoded image with its top-left at (x, y); returns its
        width. The placement matrix honours the EXIF orientation so a portrait
        phone photo is not stored on its side."""
        self.images.append(picture)
        width = height * picture.display_width / picture.display_height
        left, bottom = x, PAGE_H - y - height
        orientation = getattr(picture, "orientation", 1)
        if orientation in (3, 4):  # 180
            matrix = (-width, 0.0, 0.0, -height, left + width, bottom + height)
        elif orientation in (5, 6):  # 90 clockwise
            matrix = (0.0, -height, width, 0.0, left, bottom + height)
        elif orientation in (7, 8):  # 90 counter-clockwise
            matrix = (0.0, height, -width, 0.0, left + width, bottom)
        else:
            matrix = (width, 0.0, 0.0, height, left, bottom)
        numbers = " ".join(f"{value:.2f}" for value in matrix)
        self.ops.append(f"q {numbers} cm /Im{len(self.images)} Do Q")
        return width

    def image(self, path: Path, x: float, y: float, height: float) -> float:
        """Place a PNG with its top-left at (x, y); returns the width it took."""
        return self.draw_image(RasterImage(path), x, y, height)

    def rounded_box(
        self, x: float, y: float, w: float, h: float, radius: float, color: tuple[float, float, float]
    ) -> None:
        bottom, top = PAGE_H - y - h, PAGE_H - y
        bend = radius * 0.5523  # circular arc as a cubic bezier
        self.ops.append(
            f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg "
            f"{x + radius:.2f} {bottom:.2f} m "
            f"{x + w - radius:.2f} {bottom:.2f} l "
            f"{x + w - radius + bend:.2f} {bottom:.2f} {x + w:.2f} {bottom + radius - bend:.2f} "
            f"{x + w:.2f} {bottom + radius:.2f} c "
            f"{x + w:.2f} {top - radius:.2f} l "
            f"{x + w:.2f} {top - radius + bend:.2f} {x + w - radius + bend:.2f} {top:.2f} "
            f"{x + w - radius:.2f} {top:.2f} c "
            f"{x + radius:.2f} {top:.2f} l "
            f"{x + radius - bend:.2f} {top:.2f} {x:.2f} {top - radius + bend:.2f} "
            f"{x:.2f} {top - radius:.2f} c "
            f"{x:.2f} {bottom + radius:.2f} l "
            f"{x:.2f} {bottom + radius - bend:.2f} {x + radius - bend:.2f} {bottom:.2f} "
            f"{x + radius:.2f} {bottom:.2f} c f"
        )

    def to_pdf(self, title: str, created: datetime, producer: str) -> bytes:
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        def stream(header: str, payload: bytes) -> int:
            return add(
                f"<< {header} /Length {len(payload)} >>".encode("latin-1")
                + b"\nstream\n"
                + payload
                + b"\nendstream"
            )

        pages_content = self.finished_pages + [self.ops]
        catalog, pages = add(b""), add(b"")
        # Content streams first, so the page content stays the file's first flate stream.
        content_refs = [
            stream("/Filter /FlateDecode", zlib.compress("\n".join(ops).encode("latin-1"), 9))
            for ops in pages_content
        ]
        page_numbers = [add(b"") for _ in pages_content]
        metadata = stream("/Type /Metadata /Subtype /XML", xmp_packet(title, created, producer))
        icc = stream("/N 3 /Filter /FlateDecode", zlib.compress(base64.b64decode(SRGB_ICC_B64), 9))

        font_refs = []
        for index, (key, font) in enumerate(self.fonts.items(), 1):
            program = zlib.compress(font.data, 9)
            file_ref = stream(
                f"/Filter /FlateDecode /Length1 {len(font.data)}", program
            )
            descriptor = add(
                (
                    f"<< /Type /FontDescriptor /FontName /{font.name} "
                    f"/Flags {32 | (1 if font.fixed_pitch else 0)} "
                    f"/FontBBox [ {' '.join(str(v) for v in font.bbox)} ] "
                    f"/ItalicAngle {font.italic_angle} /Ascent {font.ascent} "
                    f"/Descent {font.descent} /CapHeight {font.cap_height} "
                    f"/StemV {font.stem_v} /MissingWidth 0 /FontFile2 {file_ref} 0 R >>"
                ).encode("latin-1")
            )
            widths = " ".join(str(w) for w in font.widths)
            number = add(
                (
                    f"<< /Type /Font /Subtype /TrueType /BaseFont /{font.name} "
                    f"/FirstChar 32 /LastChar 255 /Widths [ {widths} ] "
                    f"/Encoding /WinAnsiEncoding /FontDescriptor {descriptor} 0 R >>"
                ).encode("latin-1")
            )
            font_refs.append(f"/F{index} {number} 0 R")

        image_refs = []
        for index, picture in enumerate(self.images, 1):
            number = stream(
                f"/Type /XObject /Subtype /Image /Width {picture.width} "
                f"/Height {picture.height} {picture.pdf_dict()}",
                picture.pdf_payload(),
            )
            image_refs.append((f"/Im{index}", f"/Im{index} {number} 0 R"))

        for page_obj, content, page_ops in zip(page_numbers, content_refs, pages_content):
            joined = "\n".join(page_ops)
            used = [ref for name, ref in image_refs if f"{name} Do" in joined]
            xobjects = f"/XObject << {' '.join(used)} >> " if used else ""
            procset = "/PDF /Text /ImageC" if used else "/PDF /Text"
            objects[page_obj - 1] = (
                f"<< /Type /Page /Parent {pages} 0 R "
                f"/MediaBox [ 0 0 {PAGE_W:.2f} {PAGE_H:.2f} ] "
                f"/Resources << /ProcSet [ {procset} ] {xobjects}"
                f"/Font << {' '.join(font_refs)} >> >> "
                f"/Contents {content} 0 R >>"
            ).encode("latin-1")

        info = add(
            b"<< /Title "
            + text_string(title)
            + b" /Producer "
            + text_string(producer)
            + b" /Creator "
            + text_string(producer)
            + f" /CreationDate ({pdf_date(created)}) /ModDate ({pdf_date(created)}) >>".encode("latin-1")
        )
        objects[catalog - 1] = (
            f"<< /Type /Catalog /Pages {pages} 0 R /Lang (en) /Metadata {metadata} 0 R "
            "/OutputIntents [ << /Type /OutputIntent /S /GTS_PDFA1 "
            "/OutputConditionIdentifier (sRGB) /OutputCondition (sRGB IEC61966-2.1) "
            "/RegistryName (http://www.color.org) /Info (sRGB built-in, lcms2) "
            f"/DestOutputProfile {icc} 0 R >> ] >>"
        ).encode("latin-1")
        kids = " ".join(f"{number} 0 R" for number in page_numbers)
        objects[pages - 1] = (
            f"<< /Type /Pages /Kids [ {kids} ] /Count {len(page_numbers)} >>"
        ).encode("latin-1")

        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(objects, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        for offset in offsets:
            out += b"%010d 00000 n \n" % offset
        file_id = hashlib.sha256(bytes(out)).hexdigest()[:32].upper().encode()
        out += (
            b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R /ID [ <%s> <%s> ] >>\n"
            b"startxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, catalog, info, file_id, file_id, xref)
        )
        return bytes(out)

def pdf_date(moment: datetime) -> str:
    offset = moment.strftime("%z")
    return moment.strftime("D:%Y%m%d%H%M%S") + f"{offset[:3]}'{offset[3:]}'"


def xmp_date(moment: datetime) -> str:
    offset = moment.strftime("%z")
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f"{offset[:3]}:{offset[3:]}"


def text_string(text: str) -> bytes:
    """UTF-16BE with a BOM, so a code name with accents survives intact."""
    out = bytearray(b"(\xfe\xff")
    for byte in text.encode("utf-16-be"):
        if byte in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([byte])
        elif 32 <= byte <= 126:
            out.append(byte)
        else:
            out += f"\\{byte:03o}".encode("latin-1")
    out += b")"
    return bytes(out)


def xmp_packet(title: str, created: datetime, producer: str) -> bytes:
    escaped = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    stamp = xmp_date(created)
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/">\n'
        "   <pdfaid:part>2</pdfaid:part>\n"
        "   <pdfaid:conformance>B</pdfaid:conformance>\n"
        "  </rdf:Description>\n"
        '  <rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f"   <dc:title><rdf:Alt><rdf:li xml:lang=\"x-default\">{escaped}</rdf:li></rdf:Alt></dc:title>\n"
        "  </rdf:Description>\n"
        '  <rdf:Description rdf:about="" xmlns:pdf="http://ns.adobe.com/pdf/1.3/">\n'
        f"   <pdf:Producer>{producer}</pdf:Producer>\n"
        "  </rdf:Description>\n"
        '  <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">\n'
        f"   <xmp:CreatorTool>{producer}</xmp:CreatorTool>\n"
        f"   <xmp:CreateDate>{stamp}</xmp:CreateDate>\n"
        f"   <xmp:ModifyDate>{stamp}</xmp:ModifyDate>\n"
        "  </rdf:Description>\n"
        " </rdf:RDF>\n"
        "</x:xmpmeta>\n"
        '<?xpacket end="w"?>'
    ).encode()


def escape(text: str) -> str:
    data = text.encode("cp1252", "replace")
    out = ["("]
    for byte in data:
        if byte in (0x28, 0x29, 0x5C):
            out.append("\\" + chr(byte))
        elif 32 <= byte <= 126:
            out.append(chr(byte))
        else:
            out.append(f"\\{byte:03o}")
    out.append(")")
    return "".join(out)


def fact_grid(sheet: Sheet, facts: list[tuple[str, str]], top: float) -> float:
    """Three columns of small grey label over bold value, closed by a rule."""
    col_w = BODY_W / 3
    for i, (label, value) in enumerate(facts):
        x = MARGIN + (i % 3) * col_w
        y = top + 18 + (i // 3) * 34
        sheet.text(x, y, label.upper(), "sans", 7.2, GRAY)
        sheet.text(x, y + 13, value, "sans-bold", sheet.fit(value, "sans-bold", col_w - 12, 11.5))
    bottom = top + 18 + -(-len(facts) // 3) * 34 - 12
    sheet.rule(MARGIN, bottom, BODY_W)
    return bottom


def notes_box(sheet: Sheet, top: float, footer_top: float) -> float:
    if footer_top - top < 70:
        return top
    sheet.text(MARGIN, top + 24, "NOTES", "sans-bold", 10)
    box_top = top + 30
    box_h = footer_top - 12 - box_top
    sheet.rect(MARGIN, box_top, BODY_W, box_h, line=0.6)
    for line in range(1, int(box_h // 22)):
        sheet.rule(MARGIN + 10, box_top + line * 22, BODY_W - 20, 0.3, (0.78, 0.78, 0.80))
    return box_top + box_h


def write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def hex_color(value: str) -> tuple[float, float, float]:
    raw = value.lstrip("#")
    red, green, blue = (int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return (red, green, blue)
