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
from .favorites import (BY_ID, BY_INDEX, MARKER, MAX_IMAGE, NOTE, REPLAY, SEND_THIS, Favorites,
                        feedback, local_image, segment_types, source_file, sticker_urls)

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
        self.image_in_last_reply = False
        self.last_favorite = None
        self.seen_segments = set()
        self.lock = asyncio.Lock()

    def is_owner(self, event: AstrMessageEvent) -> bool:
        owner = str(self.config.get("owner_qq") or "").strip()
        adapter = str(self.config.get("adapter_id") or "")
        return (bool(owner and adapter) and event.is_private_chat()
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
            kinds = segment_types(event)
            names = sorted({type(item).__name__ for item in messages})
            if kinds or any(name != "Plain" for name in names):
                key = ",".join(kinds) + "|" + ",".join(names)
                if key not in self.seen_segments:
                    self.seen_segments.add(key)
                    logger.warning(f"[偏好收藏] 未识别的消息段: raw={kinds} 组件={names}")
        quotes = [item for item in messages if isinstance(item, Reply)]
        quote = quotes[0] if len(quotes) == 1 else None
        quote_sender = str(quote.sender_id) if quote else ""
        bot_quote = quote_sender == str(event.get_self_id())
        own_quote = quote_sender == str(event.get_sender_id())
        quoted_chain = (quote.chain or []) if quote and (bot_quote or own_quote) else []
        images = [item for item in quoted_chain if isinstance(item, Image)]
        quoted_text = "".join(item.text for item in quoted_chain if isinstance(item, Plain)).strip() if bot_quote else ""
        match = BY_ID.fullmatch(text)
        by_index = BY_INDEX.fullmatch(text)
        note_match = NOTE.fullmatch(text)
        if note_match:
            key = self.store.image_by_index(int(note_match[1]))
            if not key:
                yield event.plain_result("没有这个编号，发 /偏好收藏 看看。")
            else:
                await asyncio.to_thread(self.store.set_note, key, note_match[2])
                yield event.plain_result("描述记下了。")
            event.stop_event()
            return
        send_this = SEND_THIS.fullmatch(text)
        if match or by_index or send_this or REPLAY.fullmatch(text):
            path = None
            direct_image = None
            latest_user = self.recent(self.last_user_images)
            attached = attachments or (latest_user[0] if latest_user and "这张" in text else ())
            requested = images if quotes else attached
            try:
                if match:
                    path = self.store.image(match[1].lower())
                elif by_index:
                    key = self.store.image_by_index(int(by_index[1]))
                    path = self.store.image(key) if key else None
                elif len(requested) == 1:
                    data = await self.image_data(requested[0], event)
                    await asyncio.to_thread(self.store.image_suffix, data)
                    direct_image = Image.fromBytes(data)
                elif not (quotes or attached or send_this):
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
            elif by_index:
                yield event.plain_result("没有这个编号，发 /偏好收藏 看看。")
            else:
                yield event.plain_result("我没能取到那张原图。可以重新发一张、引用单张图，或在 /偏好收藏 里找编号。")
            event.stop_event()
            return

        kind = feedback(text, quoted_image=bot_quote and len(images) == 1)
        if not kind:
            return
        try:
            async with self.lock:
                if kind == "phrase":
                    previous = self.recent(self.last_text)
                    original = quoted_text or (previous[0] if previous and not quotes else "")
                    if original:
                        await asyncio.to_thread(self.store.save_phrase, original)
                        event.set_extra("favorites_saved", "你喜欢的说话方式，已经记录为语气偏好")
                elif attachments and not quotes:
                    # "我喜欢这张" 指的是主人自己刚发的那张图（含 QQ 收藏表情）。
                    data = await self.image_data(attachments[-1], event)
                    await asyncio.to_thread(self.store.save_image, data, "喜欢的视角" if "视角" in text else "喜欢的表情")
                    event.set_extra("favorites_saved", "你喜欢的那张图已收藏")
                elif len(images) == 1 and (bot_quote or own_quote):
                    data = await self.image_data(images[0], event)
                    await asyncio.to_thread(self.store.save_image, data, "喜欢的视角" if "视角" in text else "喜欢的图")
                    event.set_extra("favorites_saved", "你喜欢的那张图已收藏")
                elif not quotes:
                    # 图和「我喜欢这张」常常是两条消息，取最近的那张：主人刚发的优先，
                    # 否则就是机器人上一条回复里的图。
                    user = self.recent(self.last_user_images)
                    bot = self.recent(self.last_image)
                    use_user = bool(user) and (not bot or self.last_user_images[0] >= self.last_image[0])
                    if use_user:
                        data = await self.image_data(user[0][-1], event)
                        await asyncio.to_thread(self.store.save_image, data, "喜欢的视角" if "视角" in text else "喜欢的表情")
                        event.set_extra("favorites_saved", "你喜欢的那张图已收藏")
                    elif bot and (path := source_file(bot[0], self.roots)):
                        data = await asyncio.to_thread(path.read_bytes)
                        label = "喜欢的视角" if "视角" in text else bot[1]
                        await asyncio.to_thread(self.store.save_image, data, label)
                        event.set_extra("favorites_saved", "你喜欢的那张图已收藏")
        except Exception as exc:
            logger.warning(f"[偏好收藏] 保存失败: {type(exc).__name__}: {exc}")

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
        return await self.resolve_by_file(event, file_ref, url)

    async def resolve_by_file(self, event, file_ref: str, url: str) -> bytes:
        bot = getattr(event, "bot", None) if event is not None else None
        if not isinstance(file_ref, str) or not file_ref or bot is None:
            raise ValueError("no usable image reference")
        try:
            info = await bot.call_action("get_image", file=file_ref)
        except Exception:
            raise ValueError("get_image failed") from None
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
            return f"{i}. {note or row['title']}"
        image_lines = [label(i, row) for i, row in enumerate(images, 1)]
        phrase_lines = [f"· {row['title'][:90]}" for row in phrases]
        yield event.plain_result("\n".join(
            ["【喜欢的表情包】", *(image_lines or ["还没有收藏"]), "发收藏图 编号 可以取原图，备注 编号 描述 可以起名。",
             "", "【喜欢的话术】", *(phrase_lines or ["还没有收藏"])]
        ))

    @filter.on_llm_request(priority=-100_000)
    async def inject_preferences(self, event: AstrMessageEvent, req):
        if not self.is_owner(event):
            return
        req.system_prompt += (
            "\n【私聊表达偏好】保持原有人设。气氛合适时可以偶尔用俏皮的玩笑接主人的话。"
            "学习、正事、安慰或主人表示收住时，按当前话题自然回应。"
            "涉及收藏结果，只在本轮提供了实际保存回执时才说已经收藏。"
        )
        if styles := self.store.styles():
            req.system_prompt += "\n主人喜欢的语气：" + "、".join(styles) + "。只学语气，按当前情境组织新句子。"
        if saved := event.get_extra("favorites_saved"):
            req.system_prompt += "\n本轮真实收藏结果：" + saved + "。可以自然简短地回应。"
        recent = self.recent(self.last_favorite)
        candidates = [(row["id"], row["title"]) for row in self.store.list("image")
                      if (not recent or row["id"] != recent[0]) and self.store.image(row["id"])]
        event.set_extra("favorites_candidates", {key for key, _ in candidates})
        if candidates:
            req.system_prompt += (
                "\n以下是主人喜欢的表情图。先判断是否适合发图，合适且贴合语境时优先选一张，"
                "在正文末尾单独写 &&偏好图:实际编号&&。同一回复只选一种表情标记。"
                "主人说不发图或语境不合适时只发正文。编号和标签是内部数据。\n"
                + "\n".join(f"{key} {label}" for key, label in candidates)
            )

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
        self.image_in_last_reply = len(found) == 1
        self.last_text = (time.monotonic(), text) if text else None
        if key := event.get_extra("favorites_selected"):
            self.last_favorite = (time.monotonic(), key)
