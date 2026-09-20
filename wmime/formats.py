"""Format-aware, byte-exact size control for each supported file family.

Every generator returns (data: bytes, achieved_deviation: Optional[str]).
`achieved_deviation` is None on a clean hit, or a short human-readable note
when the target could not be hit exactly (e.g. target smaller than the
minimum safely-shrinkable size for that format).

Every family also exposes a validate(data: bytes) -> (bool, str) check used
by both the generator (self-check before writing) and the standalone
validator tool.
"""
from __future__ import annotations

import io
import random
import struct
import zipfile
import zlib
from typing import Optional, Tuple

from . import common

try:
    from PIL import Image
    _HAVE_PIL = True
except ImportError:  # pragma: no cover
    _HAVE_PIL = False

try:
    import pypdf
    from pypdf.generic import DecodedStreamObject, NameObject
    _HAVE_PYPDF = True
except ImportError:  # pragma: no cover
    _HAVE_PYPDF = False

try:
    import pefile
    _HAVE_PEFILE = True
except ImportError:  # pragma: no cover
    _HAVE_PEFILE = False


# ------------------------------------------------------------------ HTML/CSS/JS

def _pad_comment(seed: bytes, target: int, open_tok: bytes, close_tok: bytes,
                  rng: random.Random, insert_before: Optional[bytes] = None
                  ) -> Tuple[bytes, Optional[str]]:
    overhead = len(open_tok) + len(close_tok)
    if target <= len(seed):
        if target < len(seed):
            return seed, f"target ({target}B) < seed size ({len(seed)}B); cannot shrink safely, used seed as-is"
        return seed, None
    pad_len = target - len(seed) - overhead
    if pad_len < 0:
        pad_len = 0
    filler = common.make_filler_text(rng, pad_len)
    block = open_tok + filler + close_tok
    if insert_before is not None:
        idx = seed.rfind(insert_before)
        if idx != -1:
            out = seed[:idx] + block + seed[idx:]
            return out, None
    return seed + block, None


def generate_html(seed: bytes, target: int, rng: random.Random) -> Tuple[bytes, Optional[str]]:
    return _pad_comment(seed, target, b"<!--", b"-->", rng, insert_before=b"</body>")


def generate_css(seed: bytes, target: int, rng: random.Random) -> Tuple[bytes, Optional[str]]:
    return _pad_comment(seed, target, b"/*", b"*/", rng)


def generate_js(seed: bytes, target: int, rng: random.Random) -> Tuple[bytes, Optional[str]]:
    return _pad_comment(seed, target, b"/*", b"*/", rng)


def generate_xml(seed: bytes, target: int, rng: random.Random) -> Tuple[bytes, Optional[str]]:
    import re
    m = list(re.finditer(rb"</[A-Za-z_][\w:.-]*>\s*$", seed))
    if not m:
        return generate_binary_fallback(seed, target, rng)
    idx = m[-1].start()
    overhead = len(b"<!--") + len(b"-->")
    if target <= len(seed):
        if target < len(seed):
            return seed, f"target ({target}B) < seed size ({len(seed)}B); cannot shrink XML safely, used seed as-is"
        return seed, None
    pad_len = max(0, target - len(seed) - overhead)
    filler = common.make_filler_text(rng, pad_len)
    block = b"<!--" + filler + b"-->"
    return seed[:idx] + block + seed[idx:], None


def validate_markup_family(data: bytes, family: str) -> Tuple[bool, str]:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        try:
            text = data.decode("latin-1")
        except Exception as e:  # pragma: no cover
            return False, f"undecodable text: {e}"
    if family == "html":
        ok = "<html" in text.lower() or "<!doctype" in text.lower() or "<body" in text.lower() or len(text) > 0
        return ok, "" if ok else "no html-ish markers found"
    if family == "xml":
        try:
            import xml.etree.ElementTree as ET
            ET.fromstring(data)
            return True, ""
        except Exception as e:
            return False, f"xml parse failed: {e}"
    return True, ""


# ------------------------------------------------------------------ text (txt/log/ini/csv/md)

def generate_text(seed: bytes, target: int, rng: random.Random) -> Tuple[bytes, Optional[str]]:
    if target >= len(seed):
        pad_len = target - len(seed)
        filler = common.make_filler_text(rng, pad_len)
        return seed + filler, None
    # Plain text truncation is structurally safe (unlike markup).
    return seed[:target], f"target ({target}B) < seed size ({len(seed)}B); truncated plain text"


def validate_text(data: bytes) -> Tuple[bool, str]:
    try:
        data.decode("utf-8", errors="replace")
        return True, ""
    except Exception as e:  # pragma: no cover
        return False, str(e)


# ------------------------------------------------------------------ JPEG

def generate_jpeg(seed: bytes, target: int, rng: random.Random) -> Tuple[bytes, Optional[str]]:
    if not _HAVE_PIL:
        return generate_binary_fallback(seed, target, rng)
    if target > len(seed):
        pad_needed = target - len(seed)
        if len(seed) < 4 or seed[0:2] != b"\xff\xd8":
            return generate_binary_fallback(seed, target, rng)
        if pad_needed < 4:
            # Too small for a COM segment (min 4 bytes: marker+length, 0 payload).
            # 0xFF fill bytes immediately after SOI are explicitly legal JPEG
            # filler -- decoders skip extra 0xFF bytes before the next marker.
            out = seed[:2] + (b"\xff" * pad_needed) + seed[2:]
            return out, None
        # Partition pad_needed into COM segments (FF FE, 2-byte length counting
        # itself + payload, then payload bytes) so total bytes added == pad_needed
        # exactly. Each segment costs (4 + payload) bytes, payload in [0, 65533].
        # When a max-size segment would leave a 1-3 byte remainder (which can't
        # form a valid segment on its own), shave 4 bytes off this segment so
        # the remainder becomes 0 or >=4.
        segments = bytearray()
        remaining = pad_needed
        max_payload = 65533
        while remaining > 0:
            if remaining <= max_payload + 4:
                this_payload = remaining - 4
            else:
                this_payload = max_payload
                leftover = remaining - (4 + this_payload)
                if 0 < leftover < 4:
                    this_payload -= 4
            seg_len = this_payload + 2
            segments += b"\xff\xfe" + struct.pack(">H", seg_len) + common.make_filler_bytes(rng, this_payload)
            remaining -= (4 + this_payload)
        out = seed[:2] + bytes(segments) + seed[2:]
        if len(out) != target:  # pragma: no cover -- defensive, should be unreachable
            return generate_binary_fallback(seed, target, rng)
        return bytes(out), None
    if not _HAVE_PIL:
        return seed, "Pillow unavailable; cannot shrink JPEG"
    try:
        img = Image.open(io.BytesIO(seed))
        img.load()
    except Exception:
        return generate_binary_fallback(seed, target, rng)
    for quality in (85, 70, 55, 40, 25, 15, 8):
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality)
        if len(buf.getvalue()) <= target:
            return buf.getvalue(), f"re-encoded at quality={quality} to reach smaller target"
    w, h = img.size
    for scale in (0.7, 0.5, 0.3, 0.15):
        small = img.convert("RGB").resize((max(1, int(w * scale)), max(1, int(h * scale))))
        buf = io.BytesIO()
        small.save(buf, format="JPEG", quality=40)
        if len(buf.getvalue()) <= target:
            return buf.getvalue(), f"re-encoded+resized at scale={scale} to reach smaller target"
    return buf.getvalue(), "could not shrink below achieved size; used smallest achievable JPEG"


def validate_jpeg(data: bytes) -> Tuple[bool, str]:
    if not _HAVE_PIL:
        ok = data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"
        return ok, "" if ok else "missing SOI/EOI markers"
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        return True, ""
    except Exception as e:
        return False, f"jpeg verify failed: {e}"


# ------------------------------------------------------------------ PNG

def generate_png(seed: bytes, target: int, rng: random.Random) -> Tuple[bytes, Optional[str]]:
    if len(seed) < 8 or seed[:8] != b"\x89PNG\r\n\x1a\n":
        return generate_binary_fallback(seed, target, rng)
    if target > len(seed):
        pad_len = target - len(seed) - 12  # 4 length + 4 type + 4 crc, 0 data covered by pad_len calc below
        if pad_len < 0:
            pad_len = 0
        payload = common.make_filler_bytes(rng, pad_len)
        chunk_type = b"spAd"
        crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
        chunk = struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)
        iend_idx = seed.rfind(b"IEND")
        if iend_idx == -1:
            return generate_binary_fallback(seed, target, rng)
        insert_at = iend_idx - 4  # back up over the IEND length field
        out = seed[:insert_at] + chunk + seed[insert_at:]
        return out, None
    if not _HAVE_PIL:
        return seed, "Pillow unavailable; cannot shrink PNG"
    try:
        img = Image.open(io.BytesIO(seed))
        img.load()
    except Exception:
        return generate_binary_fallback(seed, target, rng)
    w, h = img.size
    for scale in (0.8, 0.6, 0.4, 0.2, 0.1):
        small = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        buf = io.BytesIO()
        small.save(buf, format="PNG", optimize=True)
        if len(buf.getvalue()) <= target:
            return buf.getvalue(), f"re-encoded+resized at scale={scale} to reach smaller target"
    return buf.getvalue(), "could not shrink below achieved size; used smallest achievable PNG"


def validate_png(data: bytes) -> Tuple[bool, str]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return False, "bad PNG signature"
    if not _HAVE_PIL:
        return True, ""
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        return True, ""
    except Exception as e:
        return False, f"png verify failed: {e}"


# ------------------------------------------------------------------ GIF

def generate_gif(seed: bytes, target: int, rng: random.Random) -> Tuple[bytes, Optional[str]]:
    if len(seed) < 6 or seed[:3] != b"GIF" or seed[-1:] != b"\x3b":
        return generate_binary_fallback(seed, target, rng)
    if target > len(seed):
        # ext_block = 0x21 0xFE + subblocks(L) + 0x00 ; len(subblocks) = L + ceil(L/255)
        # len(ext_block) = L + ceil(L/255) + 3 must equal (target - len(seed)).
        needed_block_len = target - len(seed)
        m = max(0, needed_block_len - 3)
        payload_len = m
        for _ in range(4):
            blocks = -(-payload_len // 255) if payload_len > 0 else 0
            new_len = max(0, m - blocks)
            if new_len == payload_len:
                break
            payload_len = new_len
        filler = common.make_filler_bytes(rng, payload_len)
        sub_blocks = bytearray()
        for i in range(0, len(filler), 255):
            chunk = filler[i:i + 255]
            sub_blocks += bytes([len(chunk)]) + chunk
        sub_blocks += b"\x00"
        ext_block = b"\x21\xfe" + bytes(sub_blocks)
        out = seed[:-1] + ext_block + b"\x3b"
        deviation = None
        if len(out) != target:
            # Residual off by a few bytes (rare rounding case): absorb via a second
            # small comment-extension block sized to close the gap exactly.
            diff = target - len(out)
            if diff > 0:
                extra_payload = common.make_filler_bytes(rng, max(0, diff - 3))
                extra_block = b"\x21\xfe" + bytes([len(extra_payload)]) + extra_payload + b"\x00" \
                    if extra_payload else b"\x21\xfe\x00"
                out = out[:-1] + extra_block + b"\x3b"
                if len(out) < target:
                    out = out[:-1] + common.make_filler_bytes(rng, target - len(out)) + b"\x3b"
            if len(out) != target:
                deviation = f"off by {len(out) - target} bytes after GIF comment-block padding"
        return out, deviation
    if not _HAVE_PIL:
        return seed, "Pillow unavailable; cannot shrink GIF"
    try:
        img = Image.open(io.BytesIO(seed))
        img.load()
    except Exception:
        return generate_binary_fallback(seed, target, rng)
    w, h = img.size
    for scale in (0.7, 0.5, 0.3, 0.15):
        small = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        buf = io.BytesIO()
        small.save(buf, format="GIF")
        if len(buf.getvalue()) <= target:
            return buf.getvalue(), f"re-encoded+resized at scale={scale} to reach smaller target"
    return buf.getvalue(), "could not shrink below achieved size; used smallest achievable GIF"


def validate_gif(data: bytes) -> Tuple[bool, str]:
    if data[:3] != b"GIF" or data[-1:] != b"\x3b":
        return False, "bad GIF header/trailer"
    if not _HAVE_PIL:
        return True, ""
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        return True, ""
    except Exception as e:
        return False, f"gif verify failed: {e}"


# ------------------------------------------------------------------ WAV

def generate_wav(seed: bytes, target: int, rng: random.Random) -> Tuple[bytes, Optional[str]]:
    if len(seed) < 12 or seed[:4] != b"RIFF" or seed[8:12] != b"WAVE":
        return generate_binary_fallback(seed, target, rng)
    if target > len(seed):
        pad_len = target - len(seed) - 8  # 4 id + 4 size for the JUNK chunk header
        if pad_len < 0:
            pad_len = 0
        if pad_len % 2 == 1:
            pad_len += 1  # RIFF chunks must be word-aligned
        payload = common.make_filler_bytes(rng, pad_len)
        junk = b"JUNK" + struct.pack("<I", len(payload)) + payload
        out = bytearray(seed + junk)
        new_riff_size = len(out) - 8
        out[4:8] = struct.pack("<I", new_riff_size)
        # Absorb odd-byte rounding by trimming/adding a single pad byte at the end.
        if len(out) > target:
            out = out[:target]
            out[4:8] = struct.pack("<I", len(out) - 8)
        elif len(out) < target:
            extra = target - len(out)
            out += b"\x00" * extra
            out[4:8] = struct.pack("<I", len(out) - 8)
        return bytes(out), None
    try:
        import wave
        r = wave.open(io.BytesIO(seed), "rb")
        params = r.getparams()
        frame_size = r.getsampwidth() * r.getnchannels()
        header_overhead = len(seed) - r.getnframes() * frame_size
        max_frames = max(0, (target - max(0, header_overhead)) // frame_size) if frame_size else 0
        frames = r.readframes(max_frames)
        buf = io.BytesIO()
        w = wave.open(buf, "wb")
        w.setparams(params)
        w.setnframes(0)
        w.writeframes(frames)
        w.close()
        data = buf.getvalue()
        if len(data) <= target:
            return data, "truncated PCM frames to reach smaller target"
    except Exception:
        pass
    return generate_binary_fallback(seed, target, rng)


def validate_wav(data: bytes) -> Tuple[bool, str]:
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return False, "bad RIFF/WAVE header"
    try:
        import wave
        w = wave.open(io.BytesIO(data), "rb")
        w.getparams()
        return True, ""
    except Exception as e:
        return False, f"wave parse failed: {e}"


# ------------------------------------------------------------------ PDF

def generate_pdf(seed: bytes, target: int, rng: random.Random) -> Tuple[bytes, Optional[str]]:
    if not _HAVE_PYPDF:
        return generate_binary_fallback(seed, target, rng)
    if target <= len(seed):
        if target < len(seed):
            return seed, f"target ({target}B) < seed size ({len(seed)}B); cannot shrink PDF safely, used seed as-is"
        return seed, None

    def _render(stream_len: int) -> bytes:
        writer = pypdf.PdfWriter()
        try:
            reader = pypdf.PdfReader(io.BytesIO(seed))
            for page in reader.pages:
                writer.add_page(page)
        except Exception:
            writer.add_blank_page(width=200, height=200)
        stream = DecodedStreamObject()
        stream.set_data(common.make_filler_bytes(rng, stream_len))
        stream[NameObject("/Type")] = NameObject("/WMimeWorkloadPadding")
        writer._add_object(stream)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()

    base = _render(0)
    stream_len = max(0, target - len(base))
    out = _render(stream_len)
    # Byte-exact convergence only matters at small scale; at multi-MB sizes a
    # few bytes of xref/offset drift is irrelevant and not worth another full
    # re-render (each _render() re-serializes the whole padded stream).
    close_enough = 64 if target > 1_000_000 else 0
    for _ in range(5):
        diff = target - len(out)
        if abs(diff) <= close_enough:
            break
        stream_len = max(0, stream_len + diff)
        out = _render(stream_len)
    deviation = None if len(out) == target else f"off by {len(out) - target} bytes after convergence attempts"
    return out, deviation


def validate_pdf(data: bytes) -> Tuple[bool, str]:
    if not data.startswith(b"%PDF-"):
        return False, "missing %PDF- header"
    if not _HAVE_PYPDF:
        return b"%%EOF" in data[-2048:], "" if b"%%EOF" in data[-2048:] else "missing %%EOF trailer"
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        _ = len(reader.pages)
        return True, ""
    except Exception as e:
        return False, f"pdf parse failed: {e}"


# ------------------------------------------------------------------ ZIP

def generate_zip(seed: bytes, target: int, rng: random.Random) -> Tuple[bytes, Optional[str]]:
    is_zip = zipfile.is_zipfile(io.BytesIO(seed))

    def _render(pad_len: int) -> bytes:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
            if is_zip:
                with zipfile.ZipFile(io.BytesIO(seed)) as src:
                    for info in src.infolist():
                        zf.writestr(info.filename, src.read(info.filename))
            else:
                zf.writestr("content.bin", seed)
            if pad_len > 0:
                zf.writestr("padding.bin", common.make_filler_bytes(rng, pad_len))
        return out.getvalue()

    if target <= len(seed) and not is_zip:
        return seed, f"target ({target}B) too small to wrap as ZIP with seed content"

    base = _render(0)
    if target <= len(base):
        if is_zip and target >= len(seed):
            return base, None
        return seed if not is_zip else base, (
            None if len(base) <= target else f"minimum valid ZIP is {len(base)}B, larger than target {target}B"
        )
    pad_len = target - len(base)
    out = _render(pad_len)
    if len(out) != target:
        pad_len += target - len(out)
        out = _render(max(0, pad_len))
    deviation = None if len(out) == target else f"off by {len(out) - target} bytes"
    return out, deviation


def validate_zip(data: bytes) -> Tuple[bool, str]:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        return False, "not a valid zip"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            bad = zf.testzip()
            if bad is not None:
                return False, f"corrupt member: {bad}"
        return True, ""
    except Exception as e:
        return False, f"zip test failed: {e}"


# ------------------------------------------------------------------ PE (exe/dll/com)

def _is_valid_pe(data: bytes) -> bool:
    if len(data) < 0x40 or data[:2] != b"MZ":
        return False
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew + 4 > len(data):
        return False
    return data[e_lfanew:e_lfanew + 4] == b"PE\x00\x00"


def generate_pe(seed: bytes, target: int, rng: random.Random) -> Tuple[bytes, Optional[str]]:
    if not _is_valid_pe(seed):
        return generate_binary_fallback(seed, target, rng)
    if target <= len(seed):
        if target < len(seed):
            return seed, f"target ({target}B) < seed size ({len(seed)}B); cannot shrink PE safely, used seed as-is"
        return seed, None
    overlay = common.make_filler_bytes(rng, target - len(seed))
    return seed + overlay, None


def validate_pe(data: bytes) -> Tuple[bool, str]:
    if not _is_valid_pe(data):
        return False, "missing/invalid MZ or PE signature"
    if _HAVE_PEFILE:
        try:
            pe = pefile.PE(data=data, fast_load=True)
            pe.close()
        except Exception as e:
            return False, f"pefile parse failed: {e}"
    return True, ""


# ------------------------------------------------------------------ binary_fallback

def generate_binary_fallback(seed: bytes, target: int, rng: random.Random) -> Tuple[bytes, Optional[str]]:
    if target >= len(seed):
        return seed + common.make_filler_bytes(rng, target - len(seed)), \
            "binary_fallback: no format-specific validity guarantee (seed preserved, padded)"
    return seed[:target], "binary_fallback: no format-specific validity guarantee (seed truncated)"


def validate_binary_fallback(data: bytes) -> Tuple[bool, str]:
    return True, "binary_fallback: no structural validation performed by design"


# ------------------------------------------------------------------ quick seed pre-check
# Used only during *seed selection* (planning), before any padding/generation is
# attempted, so a corpus file whose extension doesn't match its real content
# (e.g. an .jpg that is actually HTML -- these exist for real in Workload04,
# see docs/DESIGN.md) is never picked as "the" representative for a required
# major MIME family. This is cheap (magic-byte only, no full decode).

def quick_signature_ok(family: str, data: bytes) -> bool:
    if family == "jpeg":
        return len(data) >= 4 and data[:2] == b"\xff\xd8"
    if family == "png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if family == "gif":
        return data[:3] == b"GIF" and data[-1:] == b"\x3b"
    if family == "pdf":
        return data.startswith(b"%PDF-")
    if family == "zip":
        return data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
    if family == "pe":
        return _is_valid_pe(data)
    if family == "wav":
        return data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    return True  # html/css/js/xml/text/binary_fallback: no magic-byte contract to check


# ------------------------------------------------------------------ dispatch tables

GENERATORS = {
    "html": generate_html,
    "css": generate_css,
    "js": generate_js,
    "xml": generate_xml,
    "text": generate_text,
    "jpeg": generate_jpeg,
    "png": generate_png,
    "gif": generate_gif,
    "wav": generate_wav,
    "pdf": generate_pdf,
    "zip": generate_zip,
    "pe": generate_pe,
    "binary_fallback": generate_binary_fallback,
}

VALIDATORS = {
    "html": lambda d: validate_markup_family(d, "html"),
    "css": lambda d: (True, ""),
    "js": lambda d: (True, ""),
    "xml": lambda d: validate_markup_family(d, "xml"),
    "text": validate_text,
    "jpeg": validate_jpeg,
    "png": validate_png,
    "gif": validate_gif,
    "wav": validate_wav,
    "pdf": validate_pdf,
    "zip": validate_zip,
    "pe": validate_pe,
    "binary_fallback": validate_binary_fallback,
}


def generate(family: str, seed: bytes, target: int, rng: random.Random) -> Tuple[bytes, Optional[str]]:
    fn = GENERATORS.get(family, generate_binary_fallback)
    return fn(seed, target, rng)


def validate(family: str, data: bytes) -> Tuple[bool, str]:
    fn = VALIDATORS.get(family, validate_binary_fallback)
    return fn(data)
