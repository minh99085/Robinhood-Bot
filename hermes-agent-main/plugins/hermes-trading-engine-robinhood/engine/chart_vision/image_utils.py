"""Load chart images from path, URL, or base64 into bytes + mime type."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx

_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[\w/+.-]+);base64,(?P<data>.+)$",
    re.DOTALL | re.IGNORECASE,
)


def guess_mime(path_or_url: str, default: str = "image/png") -> str:
    lower = path_or_url.lower().split("?")[0]
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return "image/jpeg"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    if lower.endswith(".png"):
        return "image/png"
    return default


def load_image_bytes(
    *,
    image_base64: Optional[str] = None,
    image_url: Optional[str] = None,
    image_path: Optional[str] = None,
    mime_type: Optional[str] = None,
    timeout_s: float = 30.0,
) -> Tuple[bytes, str]:
    """
    Return ``(raw_bytes, mime_type)``.

    Exactly one of base64 / url / path should be provided (base64 preferred if multiple).
    """
    if image_base64:
        raw = image_base64.strip()
        m = _DATA_URL_RE.match(raw)
        if m:
            mime = m.group("mime")
            data = base64.b64decode(m.group("data"))
            return data, mime_type or mime
        # strip whitespace/newlines
        data = base64.b64decode(re.sub(r"\s+", "", raw))
        return data, mime_type or "image/png"

    if image_path:
        path = Path(image_path)
        if not path.is_file():
            raise FileNotFoundError(f"image_path not found: {image_path}")
        data = path.read_bytes()
        return data, mime_type or guess_mime(str(path))

    if image_url:
        url = image_url.strip()
        if url.startswith("data:"):
            return load_image_bytes(image_base64=url, mime_type=mime_type)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            # Treat as local path
            return load_image_bytes(image_path=url, mime_type=mime_type)
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "").split(";")[0].strip()
            mime = mime_type or (ct if ct.startswith("image/") else guess_mime(url))
            return resp.content, mime

    raise ValueError("Provide image_base64, image_url, or image_path")


def to_data_url(data: bytes, mime: str) -> str:
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
