from __future__ import annotations

import io
import zipfile

from PIL import Image, ImageDraw

from imagegen.extensions import db
from tests.support.platform import PlatformTestCase, png_bytes


def atlas_png_bytes(*, rows=2, columns=3, cell=(72, 56), margin=9, gap=7) -> bytes:
    width = margin * 2 + columns * cell[0] + (columns - 1) * gap
    height = margin * 2 + rows * cell[1] + (rows - 1) * gap
    image = Image.new("RGB", (width, height), (242, 244, 246))
    draw = ImageDraw.Draw(image)
    colors = [
        (33, 105, 170),
        (196, 65, 74),
        (51, 148, 91),
        (224, 154, 42),
        (111, 79, 164),
        (20, 145, 158),
    ]
    for row in range(rows):
        for column in range(columns):
            x = margin + column * (cell[0] + gap)
            y = margin + row * (cell[1] + gap)
            color = colors[(row * columns + column) % len(colors)]
            draw.rectangle((x, y, x + cell[0] - 1, y + cell[1] - 1), fill=color)
            draw.ellipse((x + 12, y + 9, x + 36, y + 33), outline=(255, 255, 255), width=3)
            draw.line((x + 7, y + 45, x + 63, y + 45), fill=(255, 255, 255), width=2)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def poster_png_bytes() -> bytes:
    image = Image.new("RGB", (640, 480), (238, 238, 238))
    draw = ImageDraw.Draw(image)
    draw.ellipse((110, 50, 530, 440), fill=(40, 120, 180))
    draw.rectangle((250, 150, 390, 380), fill=(230, 90, 60))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def transparent_atlas_png_bytes() -> bytes:
    image = Image.new("RGBA", (178, 130), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    colors = [
        (220, 50, 50, 255),
        (50, 120, 220, 255),
        (50, 180, 90, 255),
        (220, 170, 40, 255),
    ]
    for row in range(2):
        for column in range(2):
            x = 8 + column * 85
            y = 8 + row * 61
            draw.rectangle((x, y, x + 76, y + 52), fill=colors[row * 2 + column])
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class TestImageSlicing(PlatformTestCase):
    def _completed_item(self, content: bytes, *, prompt="六张素材，2×3 图集"):
        workspace = self.create_workspace("智能切图")
        job = self.submit(workspace, prompt=prompt)
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

    def test_analysis_uses_grid_count_and_uniformly_splits_the_whole_image(self):
        _workspace, item = self._completed_item(
            atlas_png_bytes(),
            prompt="六张均匀排列的素材图",
        )
        response = self.user_client().post(f"/api/generation-items/{item.id}/slice-analysis")

        self.assertEqual(response.status_code, 200)
        analysis = response.json["analysis"]
        self.assertTrue(analysis["detected"])
        self.assertEqual((analysis["rows"], analysis["columns"]), (2, 3))
        self.assertTrue(
            {"margin_x", "margin_y", "gap_x", "gap_y"}.isdisjoint(analysis)
        )
        self.assertEqual(len(analysis["boxes"]), 6)
        self.assertEqual(
            [
                (box["x"], box["y"], box["width"], box["height"])
                for box in analysis["boxes"]
            ],
            [
                (0, 0, 83, 69),
                (83, 0, 82, 69),
                (165, 0, 83, 69),
                (0, 69, 83, 68),
                (83, 69, 82, 68),
                (165, 69, 83, 68),
            ],
        )

    def test_analysis_detects_zero_gap_grid_without_inventing_gutters(self):
        _workspace, item = self._completed_item(
            atlas_png_bytes(rows=2, columns=3, margin=0, gap=0),
            prompt="六张连续排列的素材图",
        )
        response = self.user_client().post(f"/api/generation-items/{item.id}/slice-analysis")

        analysis = response.json["analysis"]
        self.assertTrue(analysis["detected"])
        self.assertEqual((analysis["rows"], analysis["columns"]), (2, 3))
        self.assertEqual(
            (analysis["boxes"][0]["width"], analysis["boxes"][0]["height"]),
            (72, 56),
        )

    def test_visual_grid_count_wins_over_a_wrong_prompt_hint(self):
        _workspace, item = self._completed_item(
            atlas_png_bytes(rows=2, columns=3, margin=0, gap=0),
            prompt="2行2列素材图",
        )
        response = self.user_client().post(f"/api/generation-items/{item.id}/slice-analysis")

        analysis = response.json["analysis"]
        self.assertTrue(analysis["detected"])
        self.assertEqual((analysis["rows"], analysis["columns"]), (2, 3))

    def test_prompt_grid_hint_still_uniformly_splits_transparent_image(self):
        _workspace, item = self._completed_item(
            transparent_atlas_png_bytes(),
            prompt="透明背景四宫格",
        )
        response = self.user_client().post(f"/api/generation-items/{item.id}/slice-analysis")

        analysis = response.json["analysis"]
        self.assertTrue(analysis["detected"])
        self.assertEqual((analysis["rows"], analysis["columns"]), (2, 2))
        self.assertTrue(all(box["width"] == 89 for box in analysis["boxes"]))
        self.assertTrue(all(box["height"] == 65 for box in analysis["boxes"]))

    def test_plain_image_is_not_reported_as_detected_atlas(self):
        _workspace, item = self._completed_item(png_bytes(), prompt="一张完整人物肖像")
        response = self.user_client().post(f"/api/generation-items/{item.id}/slice-analysis")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["analysis"]["detected"])
        self.assertEqual(response.json["analysis"]["confidence"], "low")

    def test_single_subject_on_flat_background_is_not_mistaken_for_grid(self):
        _workspace, item = self._completed_item(
            poster_png_bytes(),
            prompt="一张完整的单主体海报",
        )
        response = self.user_client().post(f"/api/generation-items/{item.id}/slice-analysis")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["analysis"]["detected"])

    def test_export_downloads_selected_crops_as_png_zip(self):
        _workspace, item = self._completed_item(
            atlas_png_bytes(rows=2, columns=3, margin=0, gap=0)
        )
        boxes = [
            {"x": 0, "y": 0, "width": 72, "height": 56},
            {"x": 72, "y": 56, "width": 72, "height": 56},
        ]
        response = self.user_client().post(
            f"/api/generation-items/{item.id}/slice-export",
            json={"action": "download", "boxes": boxes},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(len(archive.namelist()), 2)
            with Image.open(io.BytesIO(archive.read(archive.namelist()[0]))) as crop:
                self.assertEqual(crop.size, (72, 56))

    def test_export_can_save_to_library_or_use_one_slice_as_reference(self):
        workspace, item = self._completed_item(
            atlas_png_bytes(rows=2, columns=3, margin=0, gap=0)
        )
        client = self.user_client()
        box = {"x": 0, "y": 0, "width": 72, "height": 56}

        library = client.post(
            f"/api/generation-items/{item.id}/slice-export",
            json={"action": "library", "boxes": [box]},
        )
        reference = client.post(
            f"/api/generation-items/{item.id}/slice-export",
            json={"action": "reference", "boxes": [box]},
        )

        self.assertEqual(library.status_code, 201, library.get_data(as_text=True))
        self.assertEqual(library.json["images"][0]["width"], 72)
        self.assertEqual(reference.status_code, 201, reference.get_data(as_text=True))
        self.assertEqual(reference.json["asset"]["width"], 72)
        self.assertEqual(reference.json["asset"]["height"], 56)
        self.assertEqual(reference.json["asset"]["name"], "slice_01_72x56.png")
        self.assertEqual(reference.json["asset"]["position"], 0)
        self.assertEqual(
            reference.json["asset"]["url"].split("/")[-1], reference.json["asset"]["id"]
        )
        self.assertEqual(workspace.id, item.job.workspace_id)

    def test_export_rejects_out_of_bounds_and_multiple_reference_slices(self):
        _workspace, item = self._completed_item(
            atlas_png_bytes(rows=2, columns=3, margin=0, gap=0)
        )
        client = self.user_client()
        invalid = client.post(
            f"/api/generation-items/{item.id}/slice-export",
            json={
                "action": "download",
                "boxes": [{"x": 0, "y": 0, "width": 999, "height": 10}],
            },
        )
        fractional = client.post(
            f"/api/generation-items/{item.id}/slice-export",
            json={
                "action": "download",
                "boxes": [{"x": 0.5, "y": 0, "width": 72, "height": 56}],
            },
        )
        multiple = client.post(
            f"/api/generation-items/{item.id}/slice-export",
            json={
                "action": "reference",
                "boxes": [
                    {"x": 0, "y": 0, "width": 72, "height": 56},
                    {"x": 72, "y": 0, "width": 72, "height": 56},
                ],
            },
        )

        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(fractional.status_code, 400)
        self.assertEqual(fractional.json["error"], "切片坐标无效")
        self.assertEqual(multiple.status_code, 400)
        self.assertEqual(multiple.json["error"], "继续生成时只能选择一个切片")
