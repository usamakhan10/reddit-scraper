import asyncio
from typing import Optional, Dict, Any, List

import discord

from main import (
    db_record_discord_post,
    db_record_discord_reply,
    db_get_or_create_match,
    db_link_keywords,
    db_record_discord_message,
    db_find_match_id_by_message_id,
    db_record_discord_reply_ext,
    log,
)


class RedditDiscordBot(discord.Client):
    def __init__(self, channel_id: int, db_conn, *, intents: Optional[discord.Intents] = None):
        if intents is None:
            intents = discord.Intents.default()
            intents.message_content = True  # requires Message Content Intent enabled on the bot
        super().__init__(intents=intents)
        self.channel_id = channel_id
        self.db_conn = db_conn
        self._queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._poster_task: Optional[asyncio.Task] = None

    async def setup_hook(self) -> None:
        # Start background task once the bot is ready
        self._poster_task = asyncio.create_task(self._poster_worker(), name="poster-worker")

    def enqueue_match(self, payload: Dict[str, Any]) -> None:
        # Thread-safe scheduling of queue.put from non-async threads
        fut = asyncio.run_coroutine_threadsafe(self._queue.put(payload), self.loop)
        try:
            fut.result(timeout=2)
        except Exception as e:
            log.warning("Failed to enqueue payload to bot: %s", e)

    async def _poster_worker(self):
        await self.wait_until_ready()
        channel = self.get_channel(self.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.channel_id)
            except Exception as e:
                log.error("Could not fetch Discord channel %s: %s", self.channel_id, e)
                return
        assert isinstance(channel, (discord.TextChannel, discord.Thread))

        while not self.is_closed():
            payload = await self._queue.get()
            try:
                await self._post_and_record(channel, payload)
            except Exception as e:
                log.exception("Error posting to Discord: %s", e)

    async def _post_and_record(self, channel: discord.abc.Messageable, payload: Dict[str, Any]):
        kind = (payload.get("kind") or "").upper()
        url = payload.get("url") or ""
        subreddit = payload.get("subreddit") or ""
        reddit_id = payload.get("reddit_id") or ""
        keywords: List[str] = payload.get("keywords") or []

        text = payload.get("title") or payload.get("body") or ""
        snippet = text if len(text) <= 200 else text[:200] + "..."
        kw_str = ", ".join(keywords) if keywords else ""
        header = f"[{kind}] r/{subreddit} | {kw_str}" if kw_str else f"[{kind}] r/{subreddit}"
        content = f"**{header}**\n{snippet}\n{url}"

        msg = await channel.send(content)

        # Phase 3: ensure match exists and link keywords; record message mapping
        match_id = db_get_or_create_match(
            self.db_conn,
            reddit_id=reddit_id,
            reddit_url=url,
            subreddit=subreddit,
            kind=(payload.get("kind") or ""),
            title=payload.get("title"),
            body=payload.get("body"),
        )
        db_link_keywords(self.db_conn, match_id, keywords)
        db_record_discord_message(
            self.db_conn,
            match_id=match_id,
            channel_id=msg.channel.id,
            message_id=msg.id,
            guild_id=(msg.guild.id if getattr(msg, "guild", None) else None),
        )

        # Back-compat: record to legacy table as well
        db_record_discord_post(
            self.db_conn,
            reddit_id=reddit_id,
            kind=payload.get("kind") or "",
            subreddit=subreddit,
            reddit_url=url,
            keywords=keywords,
            channel_id=msg.channel.id,
            message_id=msg.id,
        )

    async def on_message(self, message: discord.Message):
        # Ignore bot's own messages
        if message.author.id == self.user.id:
            return
        # Only consider replies in the configured channel (or thread)
        if message.channel.id != self.channel_id and not isinstance(message.channel, discord.Thread):
            return

        ref = message.reference
        if not ref or not ref.message_id:
            return

        # Record any reply to a message we posted (parent message stored in DB)
        try:
            # New schema: link reply to match via message mapping
            match_id = db_find_match_id_by_message_id(self.db_conn, message_id=ref.message_id)
            if match_id is not None:
                db_record_discord_reply_ext(
                    self.db_conn,
                    match_id=match_id,
                    discord_message_id=ref.message_id,
                    channel_id=message.channel.id,
                    message_id=message.id,
                    guild_id=(message.guild.id if message.guild else None),
                    author_id=message.author.id,
                    author_name=str(message.author),
                    content=(message.content or ""),
                    url=message.jump_url,
                )
        except Exception as e:
            log.warning("Failed to record reply (v3): %s", e)
        finally:
            # Legacy table (best-effort)
            try:
                db_record_discord_reply(
                    self.db_conn,
                    discord_message_id=ref.message_id,
                    reply_message_id=message.id,
                    author_id=message.author.id,
                    author_name=str(message.author),
                    content=message.content or "",
                    url=message.jump_url,
                )
            except Exception as e:
                log.warning("Failed to record reply (legacy): %s", e)
