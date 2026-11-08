import io
import sys
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from favorites import (Favorites, feedback, local_image)


def png_bytes(color=(1, 2, 3)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(buf, format="PNG")
    return buf.getvalue()


class FeedbackTests(unittest.TestCase):
    def test_picture_and_phrase(self):
        self.assertEqual(feedback("这张我喜欢"), "image")
        self.assertEqual(feedback("这句我喜欢"), "phrase")
        self.assertEqual(feedback("我喜欢", quoted_image=True), "image")
        self.assertIsNone(feedback("我喜欢"))

    def test_rejects_negation_condition_and_length(self):
        self.assertIsNone(feedback("别喜欢这张"))
        self.assertIsNone(feedback("如果这张我喜欢"))
        self.assertIsNone(feedback("你喜欢这张吗"))
        self.assertIsNone(feedback("这张我喜欢" + "啊" * 60))


class StoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Favorites(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_note_is_detail_and_id_is_stable(self):
        data = png_bytes()
        key, inserted = self.store.save_image(data, "喜欢的表情")
        self.assertTrue(inserted)
        self.assertEqual(len(key), 12)
        self.store.set_note(key, "猫猫转圈")
        row = self.store.list("image")[0]
        self.assertEqual(row["id"], key)
        self.assertEqual(row["detail"], "猫猫转圈")
        self.assertEqual(row["title"], "喜欢的表情")
        self.assertEqual(self.store.favorite_id("1", "image"), key)
        self.assertEqual(self.store.favorite_id(key.upper(), "image"), key)

    def test_duplicate_reports_existing_and_keeps_note(self):
        data = png_bytes((4, 5, 6))
        key, _ = self.store.save_image(data, "喜欢的表情")
        self.store.set_note(key, "猫猫转圈")
        again, inserted = self.store.save_image(data, "喜欢的视角")
        self.assertEqual(again, key)
        self.assertFalse(inserted)
        row = self.store.list("image")[0]
        self.assertEqual(row["title"], "喜欢的表情")
        self.assertEqual(row["detail"], "猫猫转圈")

    def test_duplicate_without_note_refreshes_title(self):
        data = png_bytes((7, 8, 9))
        self.store.save_image(data, "喜欢的表情")
        _, inserted = self.store.save_image(data, "喜欢的视角")
        self.assertFalse(inserted)
        self.assertEqual(self.store.list("image")[0]["title"], "喜欢的视角")

    def test_corrupt_file_is_rewritten(self):
        data = png_bytes((9, 1, 2))
        key, _ = self.store.save_image(data, "喜欢的表情")
        path = self.store.image(key)
        path.write_bytes(b"corrupted")
        _, inserted = self.store.save_image(data, "喜欢的表情")
        self.assertFalse(inserted)
        self.assertEqual(path.read_bytes(), data)

    def test_delete_removes_row_and_file(self):
        key, _ = self.store.save_image(png_bytes((3, 3, 3)), "喜欢的表情")
        path = self.store.image(key)
        self.assertTrue(self.store.delete(key, "image"))
        self.assertIsNone(self.store.favorite_id(key, "image"))
        self.assertFalse(path.exists())
        self.assertFalse(self.store.delete(key, "image"))

    def test_phrase_delete_by_index(self):
        key, inserted = self.store.save_phrase("哼，就这样嘛")
        self.assertTrue(inserted)
        _, again = self.store.save_phrase("哼，就这样嘛")
        self.assertFalse(again)
        self.assertEqual(self.store.favorite_id("1", "phrase"), key)
        self.assertTrue(self.store.delete(key, "phrase"))
        self.assertIsNone(self.store.by_index("phrase", 1))

    def test_rejects_non_image_and_oversize(self):
        with self.assertRaises(ValueError) as ctx:
            self.store.save_image(b"hello", "喜欢的表情")
        self.assertIn("image format", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            Favorites.image_suffix(b"x" * (8 * 1024 * 1024 + 1))
        self.assertIn("image size", str(ctx.exception))


class LocalPathTests(unittest.TestCase):
    def test_blocks_sensitive_locations(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / ".ssh" / "face.png"
            secret.parent.mkdir()
            secret.write_bytes(png_bytes())
            self.assertIsNone(local_image(str(secret)))
            database = root / "favorites.sqlite3"
            database.write_bytes(b"not-an-image")
            self.assertIsNone(local_image(str(database)))
            allowed = root / "cache.png"
            allowed.write_bytes(png_bytes())
            self.assertEqual(local_image(str(allowed)), allowed.resolve())


if __name__ == "__main__":
    unittest.main()
