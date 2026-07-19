"""MJPEG ingestion: turn GET /api/v1/stream.mjpg into a generator of JPEG
bytes. Frames are found by scanning for the JPEG SOI/EOI markers -- the same
proven approach the old UI used -- so we never depend on multipart framing
details."""

SOI = b"\xff\xd8"   # start of image
EOI = b"\xff\xd9"   # end of image


def iter_jpegs(response, chunk_size=16384, max_buffer=8 * 1024 * 1024):
    """Yield complete JPEG frames from a `requests` streamed response."""
    buf = b""
    for chunk in response.iter_content(chunk_size=chunk_size):
        if not chunk:
            continue
        buf += chunk
        while True:
            start = buf.find(SOI)
            if start < 0:
                buf = b""
                break
            end = buf.find(EOI, start + 2)
            if end < 0:
                if start > 0:
                    buf = buf[start:]
                break
            yield buf[start:end + 2]
            buf = buf[end + 2:]
        if len(buf) > max_buffer:   # corrupt stream guard
            buf = b""
