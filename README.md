# IsraelGPT Discord Bot

A reference Python client for the [IsraelGPT public API](https://www.israelgpt.site/docs). Responds when @mentioned or DM'd, using the same persona that powers the main IsraelGPT chat.

> **This cannot run on Vercel.** A Discord bot needs a persistent WebSocket connection to Discord's gateway; Vercel serverless functions are short-lived and stateless and cannot host it. Run this as its own always-on process — a small VPS, [Railway](https://railway.app), [Fly.io](https://fly.io), or your own machine. It is a completely separate deployment from the Next.js app in the rest of this repo.

## Setup

1. Create a Discord application and bot at the [Discord Developer Portal](https://discord.com/developers/applications). Under **Bot**, enable the **Message Content** privileged intent (the bot needs it to read message text). Copy the bot token and invite the bot to your server with the `bot` scope and `Send Messages`/`Read Message History` permissions.
2. Get a free IsraelGPT API key from the [dashboard](https://www.israelgpt.site/dashboard) (requires signing in).
3. Copy `.env.example` to `.env` and fill in `DISCORD_BOT_TOKEN` and `ISRAELGPT_API_KEY`.
4. Install dependencies and run:

```bash
pip install -r requirements.txt
python bot.py
```

## What it does

- Responds when the bot is `@mentioned` in a server channel, or DM'd directly.
- Keeps the last 10 messages per channel in memory for conversational context — no database, lost on restart. This is a reference implementation, not a full-featured bot.
- Strips bracket tags with no effect through the API (`[ACTION:...]`, `[MEMORY:...]`, `[QUESTION:...]`, etc.) before posting the reply, and posts any resolved images/audio from the response's `media` object as follow-up messages.
- Handles `429` rate-limit responses gracefully, telling the user how long to wait instead of erroring.

See `bot.py`'s `call_israelgpt()` function for the exact request shape — it's mirrored verbatim in the Python example on [/docs](https://www.israelgpt.site/docs) so the two never drift apart.
