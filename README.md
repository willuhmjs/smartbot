# SmartBot — Qwen agent for Discord via discord-mcp

@mention the bot and a Qwen agent reads your message, along with recent channel context, and calls
[discord-mcp](https://github.com/SaseQ/discord-mcp) tools (channels, roles, moderation, events,
invites, emojis, forums...) as many times as it needs. Then it replies.

```
@SmartBot make a private #staff-chat channel under the Admin category that only @Mods can see
@SmartBot timeout @spammer for 10 minutes for spamming
@SmartBot schedule a movie night event friday 8pm in the Cinema voice channel
@SmartBot summarize what people talked about in #general today
```

## Setup

1. **Discord app**: in the [developer portal](https://discord.com/developers/applications), create
   a bot and turn on **Message Content Intent** (also **Server Members Intent** if you set `MEMBERS_INTENT=true`).
   Invite the bot with the permissions you want it to have. Administrator is easiest. Its role must sit
   above any roles it should manage.
2. **Qwen**: any OpenAI-compatible endpoint with tool calling, e.g. `ollama pull qwen3:14b`.
   Bigger models handle multi-step tasks much better; qwen3:14b is a reasonable minimum.
3. `cp .env.example .env` and fill it in.

### Run with Docker (both services)
```bash
docker compose up -d --build
```

### Run locally
```bash
docker run -d --name discord-mcp -p 8085:8085 -e SPRING_PROFILES_ACTIVE=http -e DISCORD_TOKEN saseq/discord-mcp:latest
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python bot.py
```

## How it works
- **Triggers**: an @mention, a ping of the bot's role, or a reply to one of the bot's messages.
- **Context**: server, channel, requester (roles and permissions), the last `HISTORY_LIMIT` messages,
  the replied-to message, mentioned users/roles/channels with IDs, attachments, and images if `VISION=true`.
- **Agent loop** (`agent.py`): runs up to `MAX_TOOL_ROUNDS` rounds of tool calls, recovers from tool errors,
  strips Qwen `<think>` blocks, and also parses `<tool_call>` text when the server's tool parser is off.
- **Safety** (`permissions.py`): every tool call is checked against the *requester's* Discord
  permissions, scoped to the target channel, plus role-hierarchy checks and an @everyone/role-ping
  guard. `guildId` is always forced to the current server. `OWNER_IDS` bypass the checks.
  Unknown tools need administrator.
- One request at a time per channel, a per-user cooldown, and long replies split across messages.
