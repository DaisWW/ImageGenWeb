from __future__ import annotations

import io
import zipfile
from pathlib import Path

import requests
from PIL import Image

from imagegen.errors import ServiceError
from imagegen.extensions import db
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


class TestMattingRoutes(PlatformTestCase):
    def setUp(self):
        super().setUp()
        self.session = RecordingMattingSession(
            response=FakeMattingResponse(content=transparent_icon_png_bytes())
        )
        self.app.extensions["lucida_matting_client"] = LucidaMattingClient(
            base_url="http://lucida.local:8756",
            model="lucida",
            session=self.session,
        )

    def _completed_item(self, content: bytes, *, name: str = "Lucida 抠图"):
        workspace = self.create_workspace(name)
        job = self.submit(workspace, prompt="opaque icon")
        item = job.items[0]
        stored = self.app.extensions["image_storage"].save_output(
            user_id=self.user.id,
            workspace_id=workspace.id,
            job_id=job.id,
            item_id=item.id,
            content=content,
        )
        item.status = "succeeded"
        item.output_path = stored.image.relative_path
        item.thumbnail_path = stored.thumbnail_path
        item.output_mime_type = stored.image.mime_type
        item.output_width = stored.image.width
        item.output_height = stored.image.height
        item.output_byte_count = stored.image.byte_count
        job.status = "succeeded"
        db.session.commit()
        return workspace, item

    def test_single_image_download_and_existing_alpha_rejected(self):
        _workspace, item = self._completed_item(png_bytes())
        client = self.user_client()

        response = client.post(f"/api/generation-items/{item.id}/matting")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")
        self.assertIn(
            f"image_{item.id}_lucida.png",
            response.headers.get("Content-Disposition", ""),
        )
        self.assertTrue(image_has_real_alpha(response.data))
        self.assertEqual(len(self.session.calls), 1)

        original = Path(self.app.config["IMAGE_STORAGE_PATH"]) / item.output_path
        with Image.open(original) as image:
            self.assertEqual(image.mode, "RGB")

        _workspace, transparent_item = self._completed_item(transparent_icon_png_bytes(), name="Lucida 已透明")
        rejected = client.post(f"/api/generation-items/{transparent_item.id}/matting")
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.get_json()["code"], "matting_already_transparent")

    def test_slice_matting_zip(self):
        image = Image.new("RGB", (128, 64), (20, 20, 20))
        image.paste(Image.new("RGB", (56, 48), (220, 50, 50)), (4, 8))
        image.paste(Image.new("RGB", (56, 48), (50, 120, 220)), (68, 8))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        _workspace, item = self._completed_item(buffer.getvalue())

        client = self.user_client()
        response = client.post(
            f"/api/generation-items/{item.id}/slice-export",
            json={
                "action": "matting",
                "boxes": [
                    {"x": 4, "y": 8, "width": 56, "height": 48},
                    {"x": 68, "y": 8, "width": 56, "height": 48},
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")
        self.assertIn(
            f"image_{item.id}_slices_lucida.zip",
            response.headers.get("Content-Disposition", ""),
        )
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            names = sorted(archive.namelist())
            self.assertEqual(len(names), 2)
            for name in names:
                self.assertTrue(name.endswith("_lucida.png"))
                self.assertTrue(image_has_real_alpha(archive.read(name)))
        self.assertEqual(len(self.session.calls), 2)

    def test_disabled_returns_503(self):
        self.app.extensions["lucida_matting_client"] = LucidaMattingClient(base_url="")
        _workspace, item = self._completed_item(png_bytes())
        response = self.user_client().post(f"/api/generation-items/{item.id}/matting")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "matting_unavailable")
