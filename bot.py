"""
IsraelGPT Discord bot - a reference client for the public API documented at
https://www.israelgpt.site/docs.

Responds when @mentioned, DM'd, or replied to. Reads the real last 10 messages in the
channel (from any author, via Discord's own message history - not a bot-maintained log) so
the model sees actual multi-user conversation context, tagging each non-bot message with its
author's display name since the API's messages array only knows role "user"/"assistant", not
arbitrary usernames.

Per-channel settings (model, persona, effort, uncensored mode, whether to post resolved
media) are configured with slash commands (/model, /persona, /effort, /uncensored, /images,
/settings, /reset) and held in memory only - no database, lost on restart. This is a
reference implementation, not a full-featured bot.

IMPORTANT: this cannot run on Vercel. A Discord bot needs a persistent WebSocket gateway
connection; Vercel serverless functions are short-lived and stateless. Run this as its own
always-on process - a small VPS, Railway, Fly.io, or your own machine. See README.md.
"""

import os
import re
from collections import defaultdict

import discord
import httpx
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
ISRAELGPT_API_KEY = os.environ["ISRAELGPT_API_KEY"]
ISRAELGPT_API_BASE_URL = os.environ.get("ISRAELGPT_API_BASE_URL", "https://www.israelgpt.site/api/v1")
# Optional - if set, slash commands sync instantly to this one server instead of a global
# sync, which Discord can take up to an hour to propagate. Handy while testing.
DEV_GUILD_ID = os.environ.get("DISCORD_DEV_GUILD_ID")

HISTORY_LIMIT = 10
DISCORD_MESSAGE_LIMIT = 2000

# Must be kept in sync with the PERSONAS export in lib/systemPrompt.ts (id, name) - this is
# only a display list for the /persona command's choices, the API validates the real thing.
PERSONAS = [
    (1, "Normal"), (2, "Overconfident"), (3, "Horny"), (4, "Lazy"), (5, "Tinfoil-hat"),
    (6, "Fun-Facts"), (7, "Self-Aware"), (8, "Overengineered"), (9, "Tsundere"), (10, "Luka"),
]
MODELS = [("israelbot-1", "IsraelBot 1"), ("israelbot-1.5", "IsraelBot 1.5"), ("israelbot-2", "IsraelBot 2")]
EFFORTS = ["low", "medium", "high", "xhigh"]

DEFAULT_SETTINGS = {"model": "israelbot-2", "persona_id": 1, "effort": "medium", "uncensored": False, "show_media": True}

# Per-channel config only - conversation context itself is read live from Discord on every
# trigger (see build_messages), not stored here.
channel_settings: dict[int, dict] = defaultdict(lambda: dict(DEFAULT_SETTINGS))

# Tags with no server-side effect via this API (see /docs) - stripped before posting.
# Content-bearing tags ([IMAGE], [car], [podcast], [music]) are left in place; their
# resolved URLs are posted separately from response["media"] below.
STRIP_TAG_RE = re.compile(
    r"\[(ACTION:[^\]]*|MEMORY:[^\]]*|flashcard:[^\]]*|QUESTION:[^\]]*|get-time:[^\]]*|"
    r"get-date:[^\]]*|create-file:[^\]]*|LGBTQ_IMG|SEND_IDF|download|crisis-event[^\]]*)\]",
    re.IGNORECASE,
)
CONTENT_TAG_RE = re.compile(r"\[(IMAGE(\?[^\]]*)?|car(\?[^\]]*)?|podcast|music)\]", re.IGNORECASE)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)


class RateLimited(Exception):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Rate limited, retry after {retry_after_seconds}s")


def clean_reply(text: str) -> str:
    text = STRIP_TAG_RE.sub("", text)
    text = CONTENT_TAG_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def chunk_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text] if text else []
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


def strip_mentions(content: str) -> str:
    if bot.user:
        content = content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "")
    return content.strip()


async def build_messages(channel: discord.abc.Messageable) -> list[dict]:
    """Reads the real last HISTORY_LIMIT messages in the channel, oldest first, regardless of
    who sent them - not a bot-maintained log. Non-bot messages are tagged with the author's
    display name folded into the content, since the API's messages array only has role
    "user"/"assistant", not a username field."""
    raw = [msg async for msg in channel.history(limit=HISTORY_LIMIT)]
    raw.reverse()

    messages = []
    for msg in raw:
        if msg.type not in (discord.MessageType.default, discord.MessageType.reply):
            continue  # skip joins/pins/other system messages
        content = strip_mentions(msg.content)
        if not content:
            continue
        if bot.user and msg.author.id == bot.user.id:
            messages.append({"role": "assistant", "content": content})
        else:
            messages.append({"role": "user", "content": f"{msg.author.display_name}: {content}"})
    return messages


async def is_reply_to_bot(message: discord.Message) -> bool:
    ref = message.reference
    if not ref or not ref.message_id:
        return False
    if isinstance(ref.resolved, discord.Message):
        return bool(bot.user and ref.resolved.author.id == bot.user.id)
    try:
        replied_to = await message.channel.fetch_message(ref.message_id)
    except (discord.NotFound, discord.Forbidden):
        return False
    return bool(bot.user and replied_to.author.id == bot.user.id)


async def call_israelgpt(messages: list[dict], settings: dict) -> dict:
    async with httpx.AsyncClient(timeout=60) as http:
        response = await http.post(
            f"{ISRAELGPT_API_BASE_URL}/chat",
            headers={"Authorization": f"Bearer {ISRAELGPT_API_KEY}"},
            json={
                "messages": messages,
                "personaId": settings["persona_id"],
                "selectedModel": settings["model"],
                "effort": settings["effort"],
                "uncensoredMode": settings["uncensored"],
            },
        )
    if response.status_code == 429:
        body = response.json()
        retry_after = body.get("error", {}).get("retry_after_seconds", 30)
        raise RateLimited(retry_after)
    response.raise_for_status()
    return response.json()


@bot.event
async def on_ready():
    if DEV_GUILD_ID:
        guild = discord.Object(id=int(DEV_GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"[israelgpt-bot] Synced commands to dev guild {DEV_GUILD_ID}")
    else:
        await bot.tree.sync()
        print("[israelgpt-bot] Synced commands globally (can take up to ~1hr to appear)")
    print(f"[israelgpt-bot] Logged in as {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = bot.user in message.mentions if bot.user else False
    if not is_dm and not is_mentioned and not await is_reply_to_bot(message):
        return

    settings = channel_settings[message.channel.id]
    messages = await build_messages(message.channel)
    if not messages:
        return

    async with message.channel.typing():
        try:
            data = await call_israelgpt(messages, settings)
        except RateLimited as e:
            await message.reply(f"Rate limited — try again in about {e.retry_after_seconds}s.")
            return
        except httpx.HTTPStatusError as e:
            print(f"[israelgpt-bot] API error: {e.response.status_code} {e.response.text}")
            await message.reply("Something went wrong talking to IsraelGPT. Try again shortly.")
            return
        except Exception as e:  # noqa: BLE001 - reference bot, keep the failure path simple
            print(f"[israelgpt-bot] Unexpected error: {e}")
            await message.reply("Something went wrong talking to IsraelGPT. Try again shortly.")
            return

    cleaned = clean_reply(data.get("reply", ""))
    for chunk in chunk_message(cleaned) or ["(empty response)"]:
        await message.reply(chunk)

    if settings["show_media"]:
        media = data.get("media") or {}
        for image in media.get("images", []):
            await message.channel.send(image["url"])
        for car in media.get("cars", []):
            await message.channel.send(car["url"])
        if media.get("podcast"):
            await message.channel.send(f"🎙️ {media['podcast']['url']}")
        if media.get("music"):
            await message.channel.send(f"🎵 {media['music']['url']}")


# ── Slash commands - per-channel settings, no persistence across restarts ──

@bot.tree.command(name="model", description="Set which IsraelGPT model this channel uses")
@app_commands.choices(model=[app_commands.Choice(name=label, value=key) for key, label in MODELS])
async def model_command(interaction: discord.Interaction, model: app_commands.Choice[str]):
    channel_settings[interaction.channel_id]["model"] = model.value
    await interaction.response.send_message(f"Model set to **{model.name}** for this channel.", ephemeral=True)


@bot.tree.command(name="persona", description="Set which IsraelGPT persona this channel uses")
@app_commands.choices(persona=[app_commands.Choice(name=name, value=pid) for pid, name in PERSONAS])
async def persona_command(interaction: discord.Interaction, persona: app_commands.Choice[int]):
    channel_settings[interaction.channel_id]["persona_id"] = persona.value
    await interaction.response.send_message(f"Persona set to **{persona.name}** for this channel.", ephemeral=True)


@bot.tree.command(name="effort", description="Set response creativity/temperature for this channel")
@app_commands.choices(effort=[app_commands.Choice(name=e, value=e) for e in EFFORTS])
async def effort_command(interaction: discord.Interaction, effort: app_commands.Choice[str]):
    channel_settings[interaction.channel_id]["effort"] = effort.value
    await interaction.response.send_message(f"Effort set to **{effort.name}** for this channel.", ephemeral=True)


@bot.tree.command(name="uncensored", description="Toggle uncensored (profanity-heavy) mode for this channel")
@app_commands.describe(enabled="On or off")
async def uncensored_command(interaction: discord.Interaction, enabled: bool):
    channel_settings[interaction.channel_id]["uncensored"] = enabled
    await interaction.response.send_message(f"Uncensored mode **{'enabled' if enabled else 'disabled'}** for this channel.", ephemeral=True)


@bot.tree.command(name="images", description="Allow or block posting resolved images/audio in this channel")
@app_commands.describe(enabled="true to post resolved media, false to block it")
async def images_command(interaction: discord.Interaction, enabled: bool):
    channel_settings[interaction.channel_id]["show_media"] = enabled
    await interaction.response.send_message(f"Media posting **{'enabled' if enabled else 'disabled'}** for this channel.", ephemeral=True)


@bot.tree.command(name="settings", description="Show this channel's current IsraelGPT settings")
async def settings_command(interaction: discord.Interaction):
    s = channel_settings[interaction.channel_id]
    persona_name = next((name for pid, name in PERSONAS if pid == s["persona_id"]), "Normal")
    await interaction.response.send_message(
        f"**Model:** {s['model']}\n**Persona:** {persona_name}\n**Effort:** {s['effort']}\n"
        f"**Uncensored:** {s['uncensored']}\n**Media posting:** {s['show_media']}",
        ephemeral=True,
    )


@bot.tree.command(name="reset", description="Reset this channel's IsraelGPT settings to defaults")
async def reset_command(interaction: discord.Interaction):
    channel_settings[interaction.channel_id] = dict(DEFAULT_SETTINGS)
    await interaction.response.send_message("Settings reset to defaults for this channel.", ephemeral=True)


if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
