"""Owner-private favorites: collect and replay liked images and phrasing."""
from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.star import Context, Star, StarTools

from .downloader import download_image
from .favorites import (BY_ID, BY_INDEX, DELETE_IMAGE, DELETE_PHRASE, MARKER, MAX_IMAGE, NOTE,
                        NOTE_CLEAR, NOTE_LIMIT, REPLAY, SEND_THIS, Favorites, feedback, local_image,
                        segment_types, source_file, sticker_urls)

PLUGIN = "astrbot_plugin_dafeiyu_favorites"


class FavoritesPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        data = Path(StarTools.get_data_dir(PLUGIN)).resolve()
        self.store = Favorites(data)
        self.roots = ((data.parent / "meme_manager" / "packs").resolve(), self.store.images)
        self.last_image = None
        self.last_user_images = None
        self.last_text = None
        self.last_favorite = None
        self.seen_segments = set()
        self.lock = asyncio.Lock()
        if not self._owner_binding():
            logger.warning("[偏好收藏] owner_qq 或 adapter_id 未填，插件不会响应任何消息。")

    def _owner_binding(self) -> tuple[str, str] | None:
        owner = str(self.config.get("owner_qq") or "").strip()
        adapter = str(self.config.get("adapter_id") or "").strip()
        return (owner, adapter) if owner and adapter else None

    def is_owner(self, event: AstrMessageEvent) -> bool:
        binding = self._owner_binding()
        if binding is None:
            return False
        owner, adapter = binding
        return (event.is_private_chat()
                and event.get_platform_name() == "aiocqhttp"
                and str(event.get_sender_id()) == owner
                and event.unified_msg_origin == f"{adapter}:FriendMessage:{owner}")

    @staticmethod
    def recent(entry):
        return entry[1:] if entry and time.monotonic() - entry[0] <= 180 else ()

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE, priority=100_000)
    async def handle_feedback(self, event: AstrMessageEvent):
        if not self.is_owner(event):
            return
        text = (event.message_str or "").strip()
        messages = event.get_messages()
        incoming = tuple(item for item in messages if isinstance(item, Image))
        stickers = tuple(sticker_urls(event))
        attachments = incoming + stickers
        if attachments:
            self.last_user_images = (time.monotonic(), attachments)
        else:
            self._note_unknown_segments(event, messages)
        quotes = [item for item in messages if isinstance(item, Reply)]
        quote = quotes[0] if len(quotes) == 1 else None
        quote_sender = str(quote.sender_id) if quote else ""
        bot_quote = quote_sender == str(event.get_self_id())
        own_quote = quote_sender == str(event.get_sender_id())
        quoted_chain = (quote.chain or []) if quote and (bot_quote or own_quote) else []
        images = [item for item in quoted_chain if isinstance(item, Image)]
        quoted_text = "".join(item.text for item in quoted_chain if isinstance(item, Plain)).strip() if bot_quote else ""
        note_match = NOTE.fullmatch(text)
        clear_match = NOTE_CLEAR.fullmatch(text)
        if note_match or clear_match:
            matched = note_match or clear_match
            reply = await self._apply_note(matched[1], matched[2] if note_match else "")
            yield event.plain_result(reply)
            event.stop_event()
            return
        delete_image = DELETE_IMAGE.fullmatch(text)
        delete_phrase = DELETE_PHRASE.fullmatch(text)
        if delete_image or delete_phrase:
            matched = delete_image or delete_phrase
            kind = "image" if delete_image else "phrase"
            yield event.plain_result(await self._apply_delete(matched[1], kind))
            event.stop_event()
            return
        match = BY_ID.fullmatch(text)
        by_index = BY_INDEX.fullmatch(text)
        send_this = SEND_THIS.fullmatch(text)
        replay = REPLAY.fullmatch(text)
        if match or by_index or send_this or replay:
            async for result in self._send_saved(event, text, images, attachments, match, by_index, replay):
                yield result
            event.stop_event()
            return

        kind = feedback(text, quoted_image=bot_quote and len(images) == 1)
        if not kind:
            return
        try:
            async with self.lock:
                await self._save_feedback(
                    event, text, kind, quotes, images, attachments, quoted_text, bot_quote, own_quote)
        except Exception as exc:
            logger.warning(f"[偏好收藏] 保存失败: {type(exc).__name__}: {exc}")
            event.set_extra("favorites_saved", self._failure_receipt(exc))
            return
        if not event.get_extra("favorites_saved"):
            missing = "没有找到可收藏的那句话" if kind == "phrase" else "没有找到可收藏的那张图"
            event.set_extra("favorites_saved", missing)

    def _note_unknown_segments(self, event, messages):
        benign_components = {"Plain", "Reply", "Face", "At"}
        benign_raw = {"reply", "face", "at"}
        kinds = [kind for kind in segment_types(event) if kind not in benign_raw]
        names = sorted({type(item).__name__ for item in messages} - benign_components)
        if not kinds and not names:
            return
        key = ",".join(kinds) + "|" + ",".join(names)
        if key not in self.seen_segments:
            self.seen_segments.add(key)
            logger.warning(f"[偏好收藏] 未识别的消息段: raw={kinds} 组件={names}")

    async def _apply_note(self, token: str, raw: str) -> str:
        key = self.store.favorite_id(token, "image")
        if not key:
            return "没有这个编号，发 /偏好收藏 看看。"
        text = (raw or "").strip()
        if not text or text == "清空":
            await asyncio.to_thread(self.store.set_note, key, "")
            return "备注已清空。"
        if len(text) > NOTE_LIMIT:
            return "备注最多 40 个字。"
        await asyncio.to_thread(self.store.set_note, key, text)
        return "描述记下了。"

    async def _apply_delete(self, token: str, kind: str) -> str:
        key = self.store.favorite_id(token, kind)
        missing = "没有这个编号，发 /偏好收藏 看看。" if kind == "image" else "没有这条话术，发 /偏好收藏 看看。"
        if not key:
            return missing
        removed = await asyncio.to_thread(self.store.delete, key, kind)
        if not removed:
            return missing
        logger.info(f"[偏好收藏] 已删除{'图片' if kind == 'image' else '话术'} {key}")
        return "已经删掉了。"

    async def _send_saved(self, event, text, images, attachments, match, by_index, replay):
        path = None
        direct_image = None
        missing = False
        latest_user = self.recent(self.last_user_images)
        attached = attachments or (latest_user[0] if latest_user and "这张" in text else ())
        # 引用里有图就用引用；纯文字引用不挡住消息里的图，也还能退回刚才发出的那张。
        pool = images or attached
        try:
            if match or by_index:
                key = self.store.favorite_id((match or by_index)[1], "image")
                if not key:
                    missing = True
                else:
                    path = self.store.image(key)
            elif len(pool) == 1:
                data = await self.image_data(pool[0], event)
                await asyncio.to_thread(self.store.image_suffix, data)
                direct_image = Image.fromBytes(data)
            elif replay and not pool:
                latest = self.recent(self.last_image)
                if latest:
                    path = source_file(latest[0], self.roots)
        except Exception as exc:
            logger.warning(f"[偏好收藏] 读取原图失败: {type(exc).__name__}: {exc}")
        event.set_extra("favorites_direct", True)
        event.set_extra("meme_manager_direct_send_attempted", True)
        if path:
            event.set_extra("favorites_sent", str(path))
            yield event.chain_result([Image.fromFileSystem(str(path))])
        elif direct_image:
            yield event.chain_result([direct_image])
        elif missing:
            yield event.plain_result("没有这个编号，发 /偏好收藏 看看。")
        else:
            yield event.plain_result("我没能取到那张原图。可以重新发一张、引用单张图，或在 /偏好收藏 里找编号。")

    async def _save_feedback(self, event, text, kind, quotes, images, attachments, quoted_text, bot_quote, own_quote):
        if kind == "phrase":
            previous = self.recent(self.last_text)
            original = quoted_text or (previous[0] if previous and not quotes else "")
            if original:
                key, inserted = await asyncio.to_thread(self.store.save_phrase, original)
                if inserted:
                    logger.info(f"[偏好收藏] 已收藏话术 {key}")
                event.set_extra(
                    "favorites_saved",
                    "你喜欢的说话方式，已经记录为语气偏好" if inserted else "这句话之前已经收藏过了",
                )
            return
        viewpoint = "视角" in text
        if attachments and not quotes:
            # 「我喜欢这张」指的是主人自己刚发的那张图（含 QQ 收藏表情）。
            data = await self.image_data(attachments[-1], event)
            await self._store_image(event, data, "喜欢的视角" if viewpoint else "喜欢的表情")
        elif len(images) == 1 and (bot_quote or own_quote):
            data = await self.image_data(images[0], event)
            await self._store_image(event, data, "喜欢的视角" if viewpoint else "喜欢的图")
        elif not quotes:
            # 图和「我喜欢这张」常常是两条消息。主人刚发的优先，否则用机器人上一条回复里的图。
            user = self.recent(self.last_user_images)
            bot = self.recent(self.last_image)
            use_user = bool(user) and (not bot or self.last_user_images[0] >= self.last_image[0])
            if use_user:
                data = await self.image_data(user[0][-1], event)
                await self._store_image(event, data, "喜欢的视角" if viewpoint else "喜欢的表情")
            elif bot and (path := source_file(bot[0], self.roots)):
                data = await asyncio.to_thread(path.read_bytes)
                await self._store_image(event, data, "喜欢的视角" if viewpoint else bot[1])

    async def _store_image(self, event, data: bytes, category: str) -> None:
        key, inserted = await asyncio.to_thread(self.store.save_image, data, category)
        if inserted:
            logger.info(f"[偏好收藏] 已收藏图片 {key}")
        event.set_extra(
            "favorites_saved",
            "你喜欢的那张图已收藏" if inserted else "这张图之前已经收藏过了",
        )

    @staticmethod
    def _failure_receipt(exc: Exception) -> str:
        if isinstance(exc, OSError):
            return "没能取到那张原图，所以没有收藏"
        text = str(exc)
        if "image size" in text or "too large" in text:
            return "这张图太大了，没能收藏"
        if "image format" in text:
            return "这张图的格式不能收藏"
        fetched = (
            "get_image", "no usable", "url host", "redirect host", "invalid url", "http ",
            "http status", "network error", "tls error", "download error", "timeout",
            "empty response", "invalid length",
        )
        if any(item in text for item in fetched):
            return "没能取到那张原图，所以没有收藏"
        return "这次没能保存成功"

    async def image_data(self, image, event=None) -> bytes:
        if isinstance(image, str):
            return await download_image(image, MAX_IMAGE)
        if path := source_file(getattr(image, "path", None), self.roots):
            return await asyncio.to_thread(path.read_bytes)
        if path := local_image(getattr(image, "path", None)):
            return await asyncio.to_thread(path.read_bytes)
        url = getattr(image, "url", None) or ""
        file_ref = getattr(image, "file", None) or ""
        for value in (url, file_ref):
            if isinstance(value, str) and value.startswith("base64://"):
                encoded = value[len("base64://"):]
                if len(encoded) > ((MAX_IMAGE + 2) // 3) * 4:
                    raise ValueError("image size")
                return base64.b64decode(encoded, validate=True)
        if isinstance(url, str) and url.startswith("https://"):
            return await download_image(url, MAX_IMAGE)
        # QQ 收藏表情只带 file id、没有地址，交给 NapCat 自己找回本地文件或可用地址。
        return await self.resolve_by_file(event, file_ref)

    async def resolve_by_file(self, event, file_ref: str) -> bytes:
        bot = getattr(event, "bot", None) if event is not None else None
        if not isinstance(file_ref, str) or not file_ref or bot is None:
            raise ValueError("no usable image reference")
        try:
            info = await bot.call_action("get_image", file=file_ref)
        except Exception as exc:
            raise ValueError(f"get_image failed: {type(exc).__name__}") from None
        if not isinstance(info, dict):
            raise ValueError("get_image returned nothing")
        inline = info.get("base64") or ""
        if isinstance(inline, str) and inline:
            encoded = inline.split("base64://")[-1]
            if len(encoded) > ((MAX_IMAGE + 2) // 3) * 4:
                raise ValueError("image size")
            data = base64.b64decode(encoded, validate=True)
            if data:
                return data
        if path := local_image(info.get("file")):
            return await asyncio.to_thread(path.read_bytes)
        if isinstance(candidate := info.get("url"), str) and candidate.startswith("https://"):
            return await download_image(candidate, MAX_IMAGE)
        raise ValueError("get_image result unusable")

    @filter.command("偏好收藏")
    async def list_favorites(self, event: AstrMessageEvent):
        if not self.is_owner(event):
            return
        event.set_extra("favorites_direct", True)
        images = self.store.list("image")
        phrases = self.store.list("phrase", 5)
        def label(i, row):
            note = (row["detail"] or "").strip()
            return f"{i}. {note or row['title']}  {row['id']}"
        image_lines = [label(i, row) for i, row in enumerate(images, 1)]
        phrase_lines = [f"{i}. {row['title'][:90]}  {row['id']}" for i, row in enumerate(phrases, 1)]
        yield event.plain_result("\n".join(
            ["【喜欢的表情包】", *(image_lines or ["还没有收藏"]),
             "发收藏图 编号或ID 取原图。备注 编号或ID 描述 起名，备注 编号或ID 清空备注。删除收藏 编号或ID 删除。",
             "", "【喜欢的话术】", *(phrase_lines or ["还没有收藏"]),
             "删除话术 编号或ID 删除。"]
        ))
        event.stop_event()

    @filter.on_llm_request(priority=-100_000)
    async def inject_preferences(self, event: AstrMessageEvent, req):
        if not self.is_owner(event):
            return
        marker = "\n【收藏偏好】\n"
        original = req.system_prompt or ""
        base = original.split(marker, 1)[0]
        blocks = []
        if saved := event.get_extra("favorites_saved"):
            blocks.append("本轮真实收藏结果：" + saved + "。只按这个事实简短回应，不要改成另一种结果。")
        if styles := self.store.styles():
            blocks.append("主人喜欢的语气：" + "、".join(styles) + "。只学语气，按当前情境组织新句子。")
        recent = self.recent(self.last_favorite)
        candidates = []
        for row in self.store.list("image"):
            if recent and row["id"] == recent[0]:
                continue
            if not self.store.image(row["id"]):
                continue
            candidates.append((row["id"], self._candidate_label(row)))
        event.set_extra("favorites_candidates", {key for key, _ in candidates})
        if candidates:
            blocks.append(
                "以下是主人收藏的表情，编号后面是备注或分类。"
                "只有当前语境确实适合发一张时，才在正文末尾单独写 &&偏好图:实际编号&&。"
                "不合适、主人说不发图、或回复里已经有图时，不要写这个标记。"
                "编号和标签是内部数据，不要念出来。\n"
                + "\n".join(f"{key} {label}" for key, label in candidates)
            )
        if blocks:
            req.system_prompt = base + marker + "\n".join(blocks)
        elif base != original:
            req.system_prompt = base

    @staticmethod
    def _candidate_label(row) -> str:
        note = MARKER.sub("", row["detail"] or "")
        note = " ".join(note.split())
        return note[:NOTE_LIMIT] or row["title"]

    @filter.on_decorating_result(priority=100_001)
    async def apply_favorite(self, event: AstrMessageEvent):
        if not self.is_owner(event) or event.get_extra("favorites_direct"):
            return
        result = event.get_result()
        chain = getattr(result, "chain", None)
        if not chain:
            return
        candidates = event.get_extra("favorites_candidates") or set()
        selected = None
        for item in chain:
            if not isinstance(item, Plain):
                continue
            for marker in MARKER.finditer(item.text):
                key = marker[1].lower()
                if key in candidates and selected is None:
                    selected = self.store.image(key)
            item.text = MARKER.sub("", item.text)
        if selected and not any(isinstance(item, Image) for item in chain):
            chain.append(Image.fromFileSystem(str(selected)))
            event.set_extra("meme_manager_direct_send_attempted", True)
            event.set_extra("favorites_sent", str(selected))
            event.set_extra("favorites_selected", selected.stem[:12])

    @filter.after_message_sent(priority=-100_000)
    async def track_delivered(self, event: AstrMessageEvent):
        if not self.is_owner(event):
            return
        path = source_file(event.get_extra("favorites_sent"), self.roots)
        found = {path: "收藏表情"} if path else {}
        registry = event.get_extra("meme_manager_delivery_registry")
        if isinstance(registry, dict):
            for record in registry.values():
                if isinstance(record, dict) and record.get("confirmed"):
                    source = source_file(record.get("source"), self.roots)
                    if source:
                        found[source] = str(record.get("category") or "表情")
        if len(found) == 1:
            path, category = next(iter(found.items()))
            self.last_image = (time.monotonic(), path, category)
        elif found:
            self.last_image = None

        if event.get_extra("favorites_direct"):
            return
        chain = getattr(event.get_result(), "chain", None) or []
        text = "".join(item.text for item in chain if isinstance(item, Plain)).strip()
        self.last_text = (time.monotonic(), text) if text else None
        if key := event.get_extra("favorites_selected"):
            self.last_favorite = (time.monotonic(), key)
