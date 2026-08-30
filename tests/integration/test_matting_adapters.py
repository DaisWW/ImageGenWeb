from __future__ import annotations

import base64
import io
import json
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from imagegen.config.matting_models import MattingModelRegistry
from imagegen.errors import ServiceError
from imagegen.integrations.background_removal import MattingAdapterFactory
from imagegen.integrations.chroma import (
    ChromaKeyAdapter,
    ChromaKeyConfig,
    TwoPassChromaKeyAdapter,
    _sample_border_color,
)
from imagegen.integrations.matting import (
    GenericMattingClient,
    _normalize_alpha_png,
    validate_matting_output,
)


def _image_bytes(pixels: list[tuple[int, int, int, int]], size: tuple[int, int]) -> bytes:
    image = Image.new("RGBA", size)
    image.putdata(pixels)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    image.close()
    return stream.getvalue()


def _read_pixels(content: bytes) -> list[tuple[int, int, int, int]]:
    with Image.open(io.BytesIO(content)) as image:
        image.load()
        return list(image.convert("RGBA").getdata())


class _Response:
    status_code = 200

    def __init__(self, content: bytes):
        self.content = content
        self.closed = False

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, content: bytes):
        self.content = content
        self.calls: list[dict] = []
        self.last_response = None

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        self.last_response = _Response(self.content)
        return self.last_response

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        self.last_response = _Response(b"ok")
        return self.last_response


class TestChromaAdapters(unittest.TestCase):
    KEY = (10, 230, 30)

    def test_soft_alpha_and_foreground_reconstruction_are_deterministic(self):
        source = _image_bytes(
            [
                (*self.KEY, 255),
                (110, 135, 30, 255),
                (220, 40, 40, 255),
                (240, 240, 240, 255),
            ],
            (4, 1),
        )
        adapter = ChromaKeyAdapter(
            {
                "key_color": list(self.KEY),
                "threshold": 0.18,
                "softness": 0.30,
                "despill_strength": 0.5,
            }
        )
        first = adapter.remove_background(source)
        second = adapter.remove_background(source)
        self.assertEqual(first, second)
        pixels = _read_pixels(first)
        self.assertEqual(pixels[0][3], 0)
        self.assertGreater(pixels[1][3], 0)
        self.assertLess(pixels[1][3], 255)
        self.assertEqual(pixels[2][3], 255)
        self.assertEqual(pixels[3][3], 255)

    def test_preserves_transparent_source_and_cleans_hidden_rgb(self):
        source = _image_bytes(
            [
                (20, 220, 30, 0),
                (220, 40, 40, 255),
            ],
            (2, 1),
        )
        output = _read_pixels(
            ChromaKeyAdapter({"key_color": list(self.KEY)}).remove_background(source)
        )
        self.assertEqual(output[0], (0, 0, 0, 0))
        self.assertEqual(output[1][3], 255)

    def test_two_pass_adapter_keeps_soft_particle_candidate(self):
        source = _image_bytes(
            [(*self.KEY, 255), (150, 190, 150, 255), (220, 220, 220, 255)],
            (3, 1),
        )
        output = _read_pixels(
            TwoPassChromaKeyAdapter({"key_color": list(self.KEY)}).remove_background(source)
        )
        self.assertGreater(output[1][3], 0)
        self.assertGreater(output[2][3], 0)

    def test_local_adapter_rejects_a_fully_opaque_result(self):
        source = _image_bytes([(220, 40, 40, 255)] * 4, (2, 2))
        with self.assertRaises(ServiceError) as context:
            ChromaKeyAdapter(
                {"key_color": [0, 255, 0], "threshold": 0, "softness": 0.01}
            ).remove_background(source)
        self.assertEqual(context.exception.code, "matting_opaque_result")

    def test_local_adapter_rejects_images_above_its_memory_budget(self):
        source = _image_bytes([(*self.KEY, 255)] * 2, (2, 1))
        with patch("imagegen.integrations.chroma.MAX_LOCAL_CHROMA_PIXELS", 1):
            with self.assertRaises(ServiceError) as context:
                ChromaKeyAdapter({"key_color": list(self.KEY)}).remove_background(source)
        self.assertEqual(context.exception.code, "matting_input_too_large")

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            ChromaKeyConfig(threshold=2)
        with self.assertRaises(ValueError):
            ChromaKeyConfig.from_mapping({"profile": "unknown"})

    def test_border_sampling_handles_extreme_aspect_ratio(self):
        width = 100_000
        content = bytes((10, 230, 30, 255)) * width
        self.assertEqual(_sample_border_color(content, width, 1, 24), (10, 230, 30))


class TestMattingAdapters(unittest.TestCase):
    def test_factory_scopes_http_sessions_to_threads(self):
        sessions = []
        sessions_lock = threading.Lock()

        def make_session():
            session = _Session(b"")
            with sessions_lock:
                sessions.append(session)
            return session

        factory = MattingAdapterFactory(session_factory=make_session)
        source = {
            "adapter_id": "generic_http",
            "base_url": "http://matting.local",
            "model": "birefnet",
        }
        barrier = threading.Barrier(3)
        observed = []
        errors = []

        def build_adapter():
            try:
                barrier.wait()
                observed.append(factory.create(source).session)
                barrier.wait()
            except BaseException as exc:  # pragma: no cover - assertion below reports it
                errors.append(exc)

        threads = [threading.Thread(target=build_adapter) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len({id(session) for session in observed}), 2)
        self.assertEqual(len(sessions), 2)
        factory.close()

    def test_factory_maps_result_snapshot_fields_and_aliases(self):
        factory = MattingAdapterFactory(session_factory=lambda: _Session(b""))
        adapter = factory.create(
            {
                "adapter_id": "hybrid",
                "upstream_model": "balanced",
                "model_base_url": "",
                "model_timeout_seconds": 30,
                "adapter_options": {"key_color": "#0AE61E"},
            }
        )
        self.assertIsInstance(adapter, TwoPassChromaKeyAdapter)
        self.assertEqual(adapter.adapter_id, "two_pass_chroma")

    def test_generic_http_normalizes_rgba_result_to_png(self):
        source = _image_bytes([(1, 2, 3, 0), (220, 30, 30, 255)], (2, 1))
        session = _Session(source)
        client = GenericMattingClient(
            base_url="http://matting.local/api/",
            model="birefnet",
            remove_path="/remove",
            health_path="/ready",
            session=session,
        )
        output = client.remove_background(source, filename="sample.png")
        with Image.open(io.BytesIO(output)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.mode, "RGBA")
        self.assertEqual(session.calls[0]["url"], "http://matting.local/api/remove")
        self.assertEqual(session.calls[0]["params"]["model"], "birefnet")

    def test_generic_http_supports_custom_file_and_json_base64_response(self):
        source = _image_bytes([(1, 2, 3, 0), (220, 30, 30, 255)], (2, 1))
        payload = json.dumps(
            {"result": {"image": base64.b64encode(source).decode("ascii")}}
        ).encode("utf-8")
        session = _Session(payload)
        client = GenericMattingClient(
            base_url="http://matting.local",
            file_field="image",
            response_field="result.image",
            session=session,
        )
        output = client.remove_background(source, filename="sample.png")
        with Image.open(io.BytesIO(output)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.mode, "RGBA")
        self.assertIn("image", session.calls[0]["files"])
        self.assertNotIn("file", session.calls[0]["files"])

    def test_generic_http_can_skip_health_and_model_query_parameter(self):
        source = _image_bytes([(1, 2, 3, 0), (220, 30, 30, 255)], (2, 1))
        session = _Session(source)
        client = GenericMattingClient(
            base_url="http://matting.local",
            model="",
            model_param="",
            health_path="",
            session=session,
        )
        client.healthcheck()
        client.remove_background(source)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0]["params"], {})

    def test_http_responses_are_closed_after_healthcheck_and_processing(self):
        source = _image_bytes([(1, 2, 3, 0), (220, 30, 30, 255)], (2, 1))
        for client_type in ("lucida", "generic"):
            with self.subTest(client_type=client_type):
                session = _Session(source)
                if client_type == "lucida":
                    client = MattingAdapterFactory(session_factory=lambda: session).create(
                        {"adapter_id": "lucida", "base_url": "http://matting.local"}
                    )
                else:
                    client = GenericMattingClient(
                        base_url="http://matting.local",
                        session=session,
                    )
                client.healthcheck()
                self.assertTrue(session.last_response.closed)
                client.remove_background(source)
                self.assertTrue(session.last_response.closed)

    def test_normalized_http_png_obeys_output_byte_limit(self):
        source = _image_bytes([(1, 2, 3, 0), (220, 30, 30, 255)], (2, 1))
        with patch("imagegen.integrations.matting.MAX_MATTING_BYTES", 1):
            with self.assertRaises(ServiceError) as context:
                _normalize_alpha_png(source)
        self.assertEqual(context.exception.code, "matting_output_too_large")

    def test_unknown_adapter_has_stable_service_error(self):
        with self.assertRaises(ServiceError) as context:
            MattingAdapterFactory().create({"adapter_id": "not-installed"})
        self.assertEqual(context.exception.code, "matting_adapter_unsupported")

    def test_output_validator_rejects_dimension_mismatch(self):
        source = _image_bytes([(1, 2, 3, 0), (220, 30, 30, 255)], (2, 1))
        with self.assertRaises(ServiceError) as context:
            validate_matting_output(source, expected_size=(3, 1))
        self.assertEqual(context.exception.code, "matting_dimension_mismatch")


class TestMattingModelConfig(unittest.TestCase):
    def test_nested_options_are_copied_at_config_boundary(self):
        raw_options = {
            "object": {"threshold": 0.2},
            "particle": {"threshold": 0.1},
        }
        model = MattingModelRegistry._parse_model(
            {
                "id": "local",
                "label": "Local",
                "backend": "hybrid",
                "options": raw_options,
            }
        )
        raw_options["object"]["threshold"] = 0.9
        public = model.public_dict()
        public["options"]["particle"]["threshold"] = 0.9
        self.assertEqual(model.options["object"]["threshold"], 0.2)
        self.assertEqual(model.options["particle"]["threshold"], 0.1)

    def test_legacy_and_local_configs_parse(self):
        legacy = MattingModelRegistry._parse_model(
            {
                "id": "legacy",
                "label": "Legacy",
                "base_url": "http://localhost",
                "model": "lucida",
            }
        )
        local = MattingModelRegistry._parse_model(
            {
                "id": "local",
                "label": "Local",
                "backend": "chroma",
                "options": {"profile": "soft", "threshold": 0.2},
            }
        )
        self.assertEqual(legacy.adapter_id, "lucida")
        self.assertTrue(legacy.configured)
        self.assertEqual(local.adapter_id, "chroma_key")
        self.assertEqual(local.model, "balanced")
        self.assertTrue(local.configured)

    def test_optional_http_fields_and_invalid_values_are_handled(self):
        generic = MattingModelRegistry._parse_model(
            {
                "id": "generic",
                "label": "Generic",
                "adapter_id": "generic_http",
                "base_url": "http://localhost",
                "model": "",
                "options": {"health_path": "", "model_param": ""},
            }
        )
        self.assertEqual(generic.options["health_path"], "")
        self.assertEqual(generic.options["model_param"], "")
        with self.assertRaises(ValueError):
            MattingModelRegistry._parse_model(
                {
                    "id": "bad-color",
                    "label": "Bad",
                    "backend": "chroma",
                    "options": {"key_color": "#GGGGGG"},
                }
            )
