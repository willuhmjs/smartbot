# SmartBot — Qwen agent for Discord via discord-mcp

@mention the bot and a Qwen agent reads your message, along with recent channel context, and calls
[discord-mcp](https://github.com/willuhmjs/discord-mcp) tools (channels, roles, moderation, events,
invites, emojis, forums...) as many times as it needs. Then it replies. It can also search the web
through [SearXNG](https://github.com/ihor-sokoliuk/mcp-searxng) or any other MCP server you add.

```
@SmartBot make a private #staff-chat channel under the Admin category that only @Mods can see
@SmartBot timeout @spammer for 10 minutes for spamming
@SmartBot schedule a movie night event friday 8pm in the Cinema voice channel
@SmartBot summarize what people talked about in #general today
@SmartBot what changed in the latest python release?
```

## Setup

1. **Discord app**: in the [developer portal](https://discord.com/developers/applications), create
   a bot and turn on **Message Content Intent** (also **Server Members Intent** if you set `MEMBERS_INTENT=true`).
   Invite the bot with the permissions you want it to have. Administrator is easiest. Its role must sit
   above any roles it should manage.
2. **Qwen**: any OpenAI-compatible endpoint with tool calling, e.g. `ollama pull qwen3:14b`.
   Bigger models handle multi-step tasks much better; qwen3:14b is a reasonable minimum.
3. `cp .env.example .env` and fill it in. Set `SEARXNG_URL` to enable web search (needs Node 22+ when
   running locally).

### Run with Docker (both services)
```bash
docker compose up -d --build
```

### Run locally
```bash
git clone https://github.com/willuhmjs/discord-mcp.git && cd discord-mcp && npm ci && npm run build   # Node 22+
DISCORD_TOKEN=... node dist/index.js &    # serves MCP on http://127.0.0.1:8085/mcp
cd ..
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python bot.py
```

## One bot, a different identity per server

`profiles.json` (see [`profiles.example.json`](profiles.example.json); path set by `PROFILES_FILE`) gives the bot
its own face in each server, so one bot account can carry several brands:

| Field | Effect |
|---|---|
| `nickname` | Its name in that server |
| `avatar`, `banner` | Its picture and profile banner there. A path relative to the profiles file, or an https URL; PNG, JPEG or GIF |
| `bio` | Its "About me" there |
| `persona` / `persona_file` | Extra instructions added to its system prompt there: voice, brand, what the server is about |
| `admins_only`, `allowed_role_ids` | Who can use it there, overriding `ADMINS_ONLY` / `ALLOWED_ROLE_IDS` |

`default` applies everywhere and each entry under `servers` (keyed by server ID) is layered on top. A field that
is absent is left alone, and `null` resets it to the bot's global profile. Profiles are applied on start-up, when
the bot joins a server, and within 30 seconds of the file (or a persona file) changing, without a restart.

Discord rate limits profile changes, so the bot only sends fields that changed: the nickname is compared with
Discord's, and for images and bios it remembers what it last sent in `.profile-state.json` (`PROFILE_STATE_FILE`).
If you change an avatar by hand in Discord, the bot won't notice until the file changes; delete the state file
to make it resend everything. Changing the nickname needs the **Change Nickname** permission.

## Running discord-mcp privately

discord-mcp's endpoint has no authentication. On a machine other people can log in to, start it with
`MCP_SOCKET=/some/private/dir/sock` so it listens on a Unix socket instead of a TCP port, and set the same
`MCP_SOCKET` for the bot. Only accounts that can enter that directory can reach it. Docker compose connects
the two containers over a private network and publishes no ports.

## Extra MCP servers
`mcp_servers.json` uses the usual `mcpServers` format. Each entry gets either a `url` (streamable HTTP)
or a `command`/`args` (stdio). `${VAR}` is filled in from the environment, and an entry that references
an empty variable is skipped. Subprocesses get only a minimal environment plus their `env`, so the bot's
tokens never reach them. `permissions` lists the Discord permissions a requester needs to use that
server's tools. It defaults to `["administrator"]`; searxng uses `[]`, so anyone can search.

`web_url_read` fetches any URL from the bot's host, including internal ones. If that matters on your
network, add it to `DISABLED_TOOLS` or give searxng `"permissions": ["administrator"]`.

## How it works
- **Triggers**: an @mention, a ping of the bot's role, or a reply to one of the bot's messages.
- **Context**: server, channel, requester (roles and permissions), the last `HISTORY_LIMIT` messages,
  the replied-to message, mentioned users/roles/channels with IDs, attachments, and images if `VISION=true`.
- **Agent loop** (`agent.py`): runs up to `MAX_TOOL_ROUNDS` rounds of tool calls, recovers from tool errors,
  strips Qwen `<think>` blocks, and also parses `<tool_call>` text when the server's tool parser is off.
- **Safety** (`permissions.py`): every tool call is checked against the *requester's* Discord
  permissions, scoped to the target channel, plus role-hierarchy checks and an @everyone/role-ping
  guard. `guildId` is always forced to the current server.
- **Access**: `OWNER_IDS` bypass every check. `ADMINS_ONLY=true` limits the bot to members with
  **Administrator** (and the server owner); `ALLOWED_ROLE_IDS` limits it to members with one of those roles.
  Both can be set per server in `profiles.json`.
- One request at a time per channel, a per-user cooldown, and long replies split across messages.

## Tests
```bash
pip install pytest && pytest
```
