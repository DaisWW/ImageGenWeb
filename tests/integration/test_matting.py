from __future__ import annotations

import io

import requests
from PIL import Image

from imagegen.errors import ServiceError
from imagegen.integrations.matting import LucidaMattingClient, image_has_real_alpha
from tests.support.platform import (
    PlatformTestCase,
    png_bytes,
    transparent_icon_png_bytes,
)


class FakeMattingResponse:
    def __init__(self, *, content: bytes = b"", status_code: int = 200, payload=None, text: str = ""):
        self.content = content
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class RecordingMattingSession:
    def __init__(self, *, response: FakeMattingResponse | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response or FakeMattingResponse(content=transparent_icon_png_bytes())


class TestLucidaMattingClient(PlatformTestCase):
    def test_client_success_and_disabled(self):
        session = RecordingMattingSession(
            response=FakeMattingResponse(content=transparent_icon_png_bytes())
        )
        client = LucidaMattingClient(
            base_url="http://lucida.local:8756",
            model="lucida",
            timeout_seconds=30,
            session=session,
        )
        result = client.remove_background(png_bytes(), filename="sample.png")
        self.assertTrue(image_has_real_alpha(result))
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["url"], "http://lucida.local:8756/remove")
        self.assertEqual(call["params"]["model"], "lucida")
        self.assertEqual(call["params"]["decontaminate"], "true")

        disabled = LucidaMattingClient(base_url="")
        with self.assertRaises(ServiceError) as ctx:
            disabled.remove_background(png_bytes())
        self.assertEqual(ctx.exception.code, "matting_unavailable")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_client_timeout_upstream_and_opaque(self):
        timeout_client = LucidaMattingClient(
            base_url="http://lucida.local",
            session=RecordingMattingSession(error=requests.Timeout("slow")),
        )
        with self.assertRaises(ServiceError) as timeout_ctx:
            timeout_client.remove_background(png_bytes())
        self.assertEqual(timeout_ctx.exception.code, "matting_timeout")

        conn_client = LucidaMattingClient(
            base_url="http://lucida.local",
            session=RecordingMattingSession(error=requests.ConnectionError("down")),
        )
        with self.assertRaises(ServiceError) as conn_ctx:
            conn_client.remove_background(png_bytes())
        self.assertEqual(conn_ctx.exception.code, "matting_connection_failed")

        upstream = LucidaMattingClient(
            base_url="http://lucida.local",
            session=RecordingMattingSession(
                response=FakeMattingResponse(
                    status_code=500,
                    payload={"detail": "segmenter boom"},
                )
            ),
        )
        with self.assertRaises(ServiceError) as upstream_ctx:
            upstream.remove_background(png_bytes())
        self.assertEqual(upstream_ctx.exception.code, "matting_upstream_failed")
        self.assertIn("segmenter boom", str(upstream_ctx.exception))

        opaque = LucidaMattingClient(
            base_url="http://lucida.local",
            session=RecordingMattingSession(response=FakeMattingResponse(content=png_bytes())),
        )
        with self.assertRaises(ServiceError) as opaque_ctx:
            opaque.remove_background(png_bytes())
        self.assertEqual(opaque_ctx.exception.code, "matting_opaque_result")

        invalid = LucidaMattingClient(
            base_url="http://lucida.local",
            session=RecordingMattingSession(
                response=FakeMattingResponse(content=b"not-an-image")
            ),
        )
        with self.assertRaises(ServiceError) as invalid_ctx:
            invalid.remove_background(png_bytes())
        self.assertEqual(invalid_ctx.exception.code, "matting_invalid_result")

