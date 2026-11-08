"""Persistent private favorites and conservative natural-language feedback."""
from __future__ import annotations

import hashlib
import io
import os
import re
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing, contextmanager
from pathlib import Path

from PIL import Image as PILImage

MAX_IMAGE = 8 * 1024 * 1024
FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif"}
POSITIVE = re.compile(r"喜欢|好喜欢|好可爱|太可爱|真好看|太好看|好图|太会说|说得好|好会说")
NEGATIVE = re.compile(r"不.{0,3}喜欢|没.{0,3}喜欢|别|不要|不想|喜欢吗|你喜欢|讨厌|算了|如果|要是")
PICTURE = re.compile(r"这张|这个视角|这[个幅张]?图|这[个张]?表情|这个角度|好图")
SPEECH = re.compile(r"这句|这段话|这个说法|这种语气|这么说|太会说|说得好|好会说")
REPLAY = re.compile(r"^(?:能不能|可以|把|给我)?(?:再发(?:一次|一遍|一下)?|重发|再来)(?:给我)?(?:刚才|刚刚|之前|收藏的|我喜欢的)?(?:的)?(?:那张|这张|上一张)(?:图|表情包)?(?:给我)?[吧嘛呗吗。！!？?\s]*$")
SEND_THIS = re.compile(r"^(?:你|请|帮我|能不能|可以)?(?:把)?(?:发(?:一下)?(?:给我)?这张(?:图|表情包)?|这张(?:图|表情包)?发(?:一下)?)(?:给我(?:看看)?|我看看|看看|一下)?[吧嘛呗吗。！!？?\s]*$")
BY_ID = re.compile(r"^发收藏图\s+([a-f0-9]{12})[。！!\s]*$", re.I)
BY_INDEX = re.compile(r"^发收藏图\s+(\d{1,4})[。！!\s]*$")
NOTE = re.compile(r"^(?:备注|描述|改名)\s+(\d{1,4})\s+(.{1,40})$")
MARKER = re.compile(r"&&偏好图:([a-f0-9]{12})&&", re.I)
SAFE_CATEGORY = re.compile(r"[a-zA-Z0-9_\-\u4e00-\u9fff]{1,24}\Z")


def feedback(text: str, *, quoted_image: bool = False) -> str | None:
    if not text or len(text) > 60 or NEGATIVE.search(text) or not POSITIVE.search(text):
        return None
    if SPEECH.search(text):
        return "phrase"
    if quoted_image or PICTURE.search(text):
        return "image"
    return None


def describe_style(text: str) -> list[str]:
    lines = [x.strip() for x in re.split(r"[。！？!?；;\n]", text) if x.strip()]
    traits = []
    if lines and sum(map(len, lines)) / len(lines) <= 18:
        traits.append("用自然的短句")
    if "……" in text or "——" in text or "..." in text:
        traits.append("有轻微的停顿和转折")
    if re.search(r"哼|啧|嘿|诶|嘛|呗|呀|喂", text):
        traits.append("带一点俏皮的口语语气")
    if re.search(r"哈[哈嗯]|[😂😏😤😳🥺🤣]", text):
        traits.append("带一点玩笑感，情绪自然")
    if re.search(r"小声|害羞|脸红|凑近|逗你|靠近", text):
        traits.append("私聊时语气轻松、点到为止")
    return traits or ["说话自然，直接接住对方的话"]


class Favorites:
    def __init__(self, root: Path):
        self.root = root
        self.images = root / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS favorites (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                detail TEXT NOT NULL, file TEXT NOT NULL, created INTEGER NOT NULL
            )""")

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.root / "favorites.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    @staticmethod
    def image_suffix(data: bytes) -> str:
        if not 0 < len(data) <= MAX_IMAGE:
            raise ValueError("image size")
        with PILImage.open(io.BytesIO(data)) as image:
            if image.format not in FORMATS or image.width * image.height > 20_000_000:
                raise ValueError("image format")
            suffix = FORMATS[image.format]
            image.verify()
        return suffix

    def save_image(self, data: bytes, category: str) -> str:
        suffix = self.image_suffix(data)
        digest = hashlib.sha256(data).hexdigest()
        key = digest[:12]
        path = self.images / (digest + suffix)
        if not path.is_file():
            with tempfile.NamedTemporaryFile(dir=self.images, delete=False) as file:
                pending = Path(file.name)
            try:
                pending.write_bytes(data)
                os.replace(pending, path)
            finally:
                pending.unlink(missing_ok=True)
        elif path.read_bytes() != data:
            raise ValueError("image hash")
        label = category if SAFE_CATEGORY.fullmatch(category or "") else "收藏表情"
        self._insert(key, "image", label, "", path.name)
        return key

    def save_phrase(self, text: str) -> str:
        phrase = text.strip()[:500]
        if not phrase:
            raise ValueError("empty phrase")
        key = hashlib.sha256(("phrase:" + phrase).encode()).hexdigest()[:12]
        self._insert(key, "phrase", phrase, "|".join(describe_style(phrase)), "")
        return key

    def _insert(self, key, kind, title, detail, filename):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO favorites VALUES (?, ?, ?, ?, ?, unixepoch())",
                       (key, kind, title, detail, filename))

    def list(self, kind: str, limit: int = 8):
        with self.connect() as db:
            return list(db.execute(
                "SELECT * FROM favorites WHERE kind=? ORDER BY rowid DESC LIMIT ?", (kind, limit)))

    def image(self, key: str) -> Path | None:
        with self.connect() as db:
            row = db.execute("SELECT file FROM favorites WHERE id=? AND kind='image'", (key,)).fetchone()
        if row and re.fullmatch(r"[a-f0-9]{64}\.(?:png|jpg|webp|gif)", row["file"]):
            path = self.images / row["file"]
            if path.is_file():
                return path
        return None

    def image_by_index(self, n: int) -> str | None:
        if not isinstance(n, int) or n < 1:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM favorites WHERE kind='image' ORDER BY rowid DESC LIMIT 1 OFFSET ?",
                (n - 1,)).fetchone()
        return row["id"] if row else None

    def set_note(self, key: str, note: str) -> str:
        note = (note or "").strip()[:40]
        with self.connect() as db:
            db.execute("UPDATE favorites SET detail=? WHERE id=? AND kind='image'", (note, key))
        return note

    def styles(self) -> list[str]:
        counts = Counter(s for row in self.list("phrase", 20) for s in row["detail"].split("|"))
        return [s for s, _ in counts.most_common(4)]


def source_file(value, roots: tuple[Path, ...]) -> Path | None:
    try:
        path = Path(value).resolve(strict=True)
        if (path.is_file() and path.stat().st_size <= MAX_IMAGE
                and any(path.is_relative_to(root) for root in roots)):
            return path
    except (TypeError, ValueError, OSError):
        pass
    return None


def local_image(value) -> Path | None:
    """A local image path produced by the OneBot layer (NapCat's own image cache).

    Not limited to the plugin roots like source_file: the protocol layer decides this
    path, and every caller still validates size and image format before using it.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        path = Path(value)
        if not path.is_absolute():
            return None
        path = path.resolve(strict=True)
        if path.is_file() and path.stat().st_size <= MAX_IMAGE:
            return path
    except (OSError, ValueError):
        pass
    return None


STICKER_HOST = "https://gxh.vip.qq.com/club/item/parcel/item"
EMOJI_ID = re.compile(r"[A-Za-z0-9]{4,64}\Z")


def _segments(event):
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    segments = getattr(raw, "message", None)
    return segments if isinstance(segments, list) else []


def sticker_urls(event) -> list[str]:
    """QQ 收藏/商城表情：AstrBot 会把 mface 段整段丢掉，只能从原始事件自己取。

    NapCat 对 mface 只给 emoji_id/key/summary，没有地址；地址按它自己
    marketFaceElement 的规则拼（前两位做目录，末尾 raw300.gif）。
    """
    urls = []
    for segment in _segments(event):
        if not isinstance(segment, dict) or segment.get("type") != "mface":
            continue
        data = segment.get("data") or {}
        url = data.get("url")
        if not (isinstance(url, str) and url.startswith("https://")):
            emoji = data.get("emoji_id")
            url = ""
            if isinstance(emoji, str) and EMOJI_ID.fullmatch(emoji):
                url = f"{STICKER_HOST}/{emoji[:2]}/{emoji}/raw300.gif"
        if url:
            urls.append(url)
    return urls


def segment_types(event) -> list[str]:
    kinds = {str(s.get("type")) for s in _segments(event) if isinstance(s, dict)}
    return sorted(kinds - {"text"})

