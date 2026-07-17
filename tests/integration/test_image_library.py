from __future__ import annotations

import io
import threading
from unittest.mock import patch

from PIL import Image
from sqlalchemy import func, select

from imagegen.extensions import db
from imagegen.models import (
    LibraryImage,
)
from tests.support.platform import (
    PlatformTestCase,
    png_bytes,
)


class TestImageLibrary(PlatformTestCase):
    def test_image_library_deduplicates_and_copies_across_workspaces(self):
        source = self.create_workspace("图库来源")
        target = self.create_workspace("图库目标")
        content = png_bytes((220, 35, 45))
        source_asset = self.services.workspaces.add_assets(
            source,
            [("source.png", content)],
        )[0]
        client = self.user_client()

        saved = client.post("/api/library-images", json={"asset_id": source_asset.id})
        self.assertEqual(saved.status_code, 201, saved.get_data(as_text=True))
        image = saved.json["images"][0]
        self.assertEqual(saved.json["added_count"], 1)
        self.assertEqual(client.get(image["url"]).data, content)

        duplicate = client.post(
            "/api/library-images",
            data={"images": (io.BytesIO(content), "duplicate.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json["added_count"], 0)
        self.assertEqual(duplicate.json["images"][0]["id"], image["id"])

        imported = client.post(f"/api/workspaces/{target.id}/assets/from-library/{image['id']}")
        self.assertEqual(imported.status_code, 201, imported.get_data(as_text=True))
        imported_asset = imported.json["asset"]
        self.assertNotEqual(imported_asset["id"], source_asset.id)
        self.assertEqual(client.get(imported_asset["url"]).data, content)
        imported_again = client.post(
            f"/api/workspaces/{target.id}/assets/from-library/{image['id']}"
        )
        self.assertEqual(imported_again.status_code, 200)
        self.assertEqual(imported_again.json["asset"]["id"], imported_asset["id"])

        self.assertEqual(client.delete(f"/api/workspaces/{source.id}").status_code, 200)
        self.assertEqual(client.get(image["url"]).data, content)
        self.assertEqual(client.delete(f"/api/library-images/{image['id']}").status_code, 200)
        self.assertEqual(client.get(imported_asset["url"]).data, content)
        self.assertEqual(client.get("/api/library-images").json["images"], [])
        self.assertEqual(db.session.scalar(select(func.count(LibraryImage.id))), 0)

    def test_image_library_concurrent_duplicate_uploads_create_one_record(self):
        content = png_bytes((170, 55, 90))
        barrier = threading.Barrier(2)
        storage = self.app.extensions["image_storage"]
        inspect_static = storage.inspect_static
        responses = []
        failures = []

        def synchronized_inspect(payload):
            inspected = inspect_static(payload)
            barrier.wait(timeout=10)
            return inspected

        def upload(name):
            try:
                client = self.app.test_client()
                login = client.post(
                    "/login",
                    data={"username": "artist", "password": "StrongPass123!"},
                )
                if login.status_code != 302:
                    raise AssertionError(f"concurrent login failed: {login.status_code}")
                responses.append(
                    client.post(
                        "/api/library-images",
                        data={"images": (io.BytesIO(content), name)},
                        content_type="multipart/form-data",
                    )
                )
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        with patch.object(storage, "inspect_static", side_effect=synchronized_inspect) as inspect:
            threads = [
                threading.Thread(target=upload, args=(f"duplicate-{index}.png",))
                for index in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(15)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertCountEqual([response.status_code for response in responses], [200, 201])
        self.assertEqual(len({response.json["images"][0]["id"] for response in responses}), 1)
        self.assertCountEqual([response.json["added_count"] for response in responses], [0, 1])
        self.assertEqual(inspect.call_count, 2)
        self.assertEqual(db.session.scalar(select(func.count(LibraryImage.id))), 1)

    def test_image_library_inspects_new_upload_once(self):
        storage = self.app.extensions["image_storage"]
        with patch.object(storage, "inspect_static", wraps=storage.inspect_static) as inspect:
            response = self.user_client().post(
                "/api/library-images",
                data={"images": (io.BytesIO(png_bytes()), "once.png")},
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(inspect.call_count, 1)

    def test_image_library_enforces_count_and_byte_quotas(self):
        client = self.user_client()
        first_content = png_bytes((15, 75, 135))
        second_content = png_bytes((135, 75, 15))

        with patch("imagegen.services.image_library.MAX_LIBRARY_IMAGES", 1):
            first = client.post(
                "/api/library-images",
                data={"images": (io.BytesIO(first_content), "first.png")},
                content_type="multipart/form-data",
            )
            over_count = client.post(
                "/api/library-images",
                data={"images": (io.BytesIO(second_content), "second.png")},
                content_type="multipart/form-data",
            )
            duplicate = client.post(
                "/api/library-images",
                data={"images": (io.BytesIO(first_content), "duplicate.png")},
                content_type="multipart/form-data",
            )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(over_count.status_code, 409)
        self.assertEqual(over_count.json["code"], "library_quota")
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json["added_count"], 0)

        with patch(
            "imagegen.services.image_library.MAX_LIBRARY_BYTES",
            len(first_content) + len(second_content) - 1,
        ):
            over_bytes = client.post(
                "/api/library-images",
                data={"images": (io.BytesIO(second_content), "second.png")},
                content_type="multipart/form-data",
            )

        self.assertEqual(over_bytes.status_code, 409)
        self.assertEqual(over_bytes.json["code"], "library_quota")
        self.assertEqual(db.session.scalar(select(func.count(LibraryImage.id))), 1)

    def test_image_library_list_is_paginated(self):
        contents = [png_bytes((index * 45, 80, 160)) for index in range(1, 4)]
        images, added_count = self.services.image_library.add(
            self.user.id,
            [(f"page-{index}.png", content) for index, content in enumerate(contents)],
        )
        client = self.user_client()

        first = client.get("/api/library-images?offset=0&limit=2")
        second = client.get("/api/library-images?offset=2&limit=2")

        self.assertEqual(added_count, 3)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["total"], 3)
        self.assertTrue(first.json["has_more"])
        self.assertEqual(len(first.json["images"]), 2)
        self.assertFalse(second.json["has_more"])
        self.assertEqual(len(second.json["images"]), 1)
        listed = first.json["images"] + second.json["images"]
        self.assertEqual({entry["id"] for entry in listed}, {image.id for image in images})
        self.assertTrue(all(entry["thumbnail_url"].endswith("/thumbnail") for entry in listed))
        self.assertTrue({"mime_type", "bytes", "width", "height", "created_at"}.issubset(listed[0]))
        self.assertEqual(client.get("/api/library-images?offset=-1").status_code, 400)
        self.assertEqual(client.get("/api/library-images?limit=0").status_code, 400)

    def test_image_library_rejects_animations_and_is_account_private(self):
        output = io.BytesIO()
        frames = [Image.new("RGB", (2, 2), color) for color in ("red", "blue")]
        frames[0].save(
            output,
            format="WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=100,
        )
        client = self.user_client()
        rejected = client.post(
            "/api/library-images",
            data={"images": (io.BytesIO(output.getvalue()), "animation.webp")},
            content_type="multipart/form-data",
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("仅支持静态图片", rejected.json["error"])

        stored = client.post(
            "/api/library-images",
            data={"images": (io.BytesIO(png_bytes()), "private.png")},
            content_type="multipart/form-data",
        ).json["images"][0]
        stored_url = stored["url"]
        thumbnail_url = stored["thumbnail_url"]
        thumbnail = client.get(thumbnail_url)
        self.assertEqual(thumbnail.status_code, 200)
        self.assertEqual(thumbnail.mimetype, "image/webp")
        with Image.open(io.BytesIO(thumbnail.data)) as preview:
            self.assertEqual(preview.format, "WEBP")
        db.session.get(LibraryImage, stored["id"]).thumbnail_path = None
        db.session.commit()
        legacy_thumbnail = client.get(thumbnail_url)
        self.assertEqual(legacy_thumbnail.status_code, 200)
        self.assertEqual(legacy_thumbnail.mimetype, "image/png")
        self.assertEqual(legacy_thumbnail.data, png_bytes())
        outsider = self.services.users.create(
            username="library-outsider",
            password="StrongPass123!",
            actor_user_id=self.admin.id,
        )
        self.context.pop()
        try:
            outsider_client = self.app.test_client()
            login = outsider_client.post(
                "/login",
                data={"username": outsider.username, "password": "StrongPass123!"},
            )
            self.assertEqual(login.status_code, 302)
            self.assertEqual(outsider_client.get(stored_url).status_code, 404)
            self.assertEqual(outsider_client.get(thumbnail_url).status_code, 404)
            self.assertEqual(outsider_client.get("/api/library-images").json["images"], [])
        finally:
            self.context.push()
