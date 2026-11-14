"""Ảnh mẫu cho test P2-4 (do test tự sinh, không dùng ảnh thật) và bộ đọc cấu trúc file ảnh."""

import io
import struct
import zlib

from PIL import Image, ImageCms, PngImagePlugin
from PIL.ExifTags import GPS, IFD, Base

XMP_PACKET = (
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description '
    b'GPSLatitude="10,46N" CreatorTool="SecretCam"/></rdf:RDF></x:xmpmeta>'
)
TEXT_COMMENT = "secret-comment-do-not-keep"
CAMERA_MAKE = "SecretCam"
ICC_PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
TOP_LEFT_COLOR = (20, 40, 220)  # góc trên-trái màu xanh để kiểm hướng xoay
BACKGROUND = (220, 30, 30)

# Segment / chunk chứa metadata nhạy cảm (EXIF, GPS, XMP, text) — không được còn trong file đầu ra
FORBIDDEN_METADATA = {
    "JPEG": {"APP1", "APP13", "COM"},
    "PNG": {"eXIf", "tEXt", "iTXt", "zTXt"},
    "WEBP": {"EXIF", "XMP"},
}


def make_exif(orientation: int = 6) -> bytes:
    exif = Image.Exif()
    exif[Base.Make] = CAMERA_MAKE
    exif[Base.Orientation] = orientation
    gps = exif.get_ifd(IFD.GPSInfo)
    gps[GPS.GPSLatitudeRef] = "N"
    gps[GPS.GPSLatitude] = (10.0, 46.0, 0.0)
    gps[GPS.GPSLongitudeRef] = "E"
    gps[GPS.GPSLongitude] = (106.0, 40.0, 0.0)
    return exif.tobytes()


def make_image(
    fmt: str,
    size: tuple[int, int] = (60, 40),
    *,
    mode: str = "RGB",
    orientation: int = 6,
    with_metadata: bool = True,
) -> bytes:
    """Ảnh `fmt` (PNG/JPEG/WEBP) kèm EXIF (GPS + hướng xoay), XMP, text và ICC."""
    image = Image.new(mode, size, BACKGROUND if mode != "L" else 128)
    if mode != "L":
        image.paste(TOP_LEFT_COLOR, (0, 0, size[0] // 3, size[1] // 3))
    out = io.BytesIO()
    options: dict[str, object] = {}
    if with_metadata:
        options = {"exif": make_exif(orientation), "icc_profile": ICC_PROFILE}
        if fmt == "PNG":
            info = PngImagePlugin.PngInfo()
            info.add_text("Comment", TEXT_COMMENT)
            info.add_itxt("XML:com.adobe.xmp", XMP_PACKET.decode())
            options["pnginfo"] = info
        else:
            options["xmp"] = XMP_PACKET
        if fmt == "JPEG":
            options["comment"] = TEXT_COMMENT.encode()
    image.save(out, fmt, **options)
    return out.getvalue()


def make_animated(fmt: str) -> bytes:
    frames = [Image.new("RGB", (20, 20), color) for color in [(255, 0, 0), (0, 255, 0)]]
    out = io.BytesIO()
    frames[0].save(out, fmt, save_all=True, append_images=frames[1:], duration=100, loop=0)
    return out.getvalue()


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def make_png_header_only(width: int, height: int) -> bytes:
    """PNG hợp lệ về header nhưng khai kích thước khổng lồ (decompression bomb) — vài chục byte."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # grayscale 8-bit
    idat = zlib.compress(b"\x00" * 16)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def metadata_sections(data: bytes, fmt: str) -> list[str]:
    """Tên segment (JPEG) / chunk (PNG, WebP) theo thứ tự trong file."""
    if fmt == "JPEG":
        return _jpeg_segments(data)
    if fmt == "PNG":
        return _png_chunks(data)
    return _webp_chunks(data)


def _jpeg_segments(data: bytes) -> list[str]:
    names = {0xE1: "APP1", 0xE2: "APP2", 0xED: "APP13", 0xFE: "COM", 0xE0: "APP0"}
    found, position = [], 2
    while position + 4 <= len(data) and data[position] == 0xFF:
        marker = data[position + 1]
        if marker == 0xDA:  # SOS: sau đó là dữ liệu ảnh đã nén
            found.append("SOS")
            break
        length = struct.unpack(">H", data[position + 2 : position + 4])[0]
        found.append(names.get(marker, hex(marker)))
        position += 2 + length
    return found


def _png_chunks(data: bytes) -> list[str]:
    found, position = [], 8
    while position + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[position : position + 8])
        found.append(kind.decode("latin-1"))
        position += 12 + length
    return found


def _webp_chunks(data: bytes) -> list[str]:
    found, position = [], 12
    while position + 8 <= len(data):
        kind, length = struct.unpack("<4sI", data[position : position + 8])
        found.append(kind.decode("latin-1").strip())
        position += 8 + length + (length & 1)
    return found
