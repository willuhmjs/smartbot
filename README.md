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
| `access`, `allowed_role_ids` | Who can use it there (`everyone`, `manage_guild` or `administrator`), overriding `ACCESS` / `ALLOWED_ROLE_IDS` |
| any other setting | e.g. `automod`, `model`, `escalation`: the deployment's value for a setting below |

`default` applies everywhere and each entry under `servers` (keyed by server ID) is layered on top. A field that
is absent is left alone, and `null` resets it to the bot's global profile. Profiles are applied on start-up, when
the bot joins a server, and within 30 seconds of the file (or a persona file) changing, without a restart.

Discord rate limits profile changes, so the bot only sends fields that changed: the nickname is compared with
Discord's, and for images and bios it remembers what it last sent in its database. If you change an avatar by
hand in Discord, the bot won't notice until the file changes; `DELETE FROM profile_state` in the database makes it
resend everything. Changing the nickname needs the **Change Nickname** permission.

## Settings from Discord

Members with **Manage Server** can change the bot's settings in their server, either with slash commands
or by asking it (`@SmartBot turn on automod and log to #mod-log`). Changes are stored in
the database and layered on top of `profiles.json`; `/settings reset` goes back to the
deployment's value. The slash commands are registered in each server the bot is in, when it starts and when
it joins one.

| Command | |
|---|---|
| `/settings view [section]` | Every setting, its value and where it comes from |
| `/settings set <key> <value>` | Change one (autocompletes keys and values) |
| `/settings reset <key>` | Undo a change made in Discord |
| `/settings rules` · `persona` · `bio` | Edit long text in a form |
| `/settings avatar` · `banner` | Upload an image |
| `/settings test-automod <message>` | What automod would decide, without acting |
| `/strikes view` · `add` · `clear` | Members' strikes (needs **Moderate Members**) |

Settings: identity (nickname, avatar, banner, bio, persona), access, the model for chat and for automod,
automod (rules, channels, exempt roles, severity tiers), anti-spam (flood, duplicate and mention limits, raid
alerts), the mod log channel, and strikes (expiry, escalation to timeout/kick/ban).

**A setting needs the permissions its value uses.** Turning on automod or a `delete_warn` tier needs
**Manage Messages**; an escalation step that bans needs **Ban Members**. Someone with only Manage Server can't
set up the bot to do what they couldn't do themselves.

### Staff-only tools and member rate limits
With `tools` set to `staff`, anyone allowed by `access` can chat with the bot, but only members holding a
moderation or management permission (Manage Messages, Moderate Members, Manage Server, ...) get tools. Everyone
else gets a minimal mode: no Discord tools, no web search, no image reading, and a shorter history. Members who
aren't staff are also rate limited: `member_per_minute` (4), `member_per_hour` (30), and
`member_server_per_hour` (150, all of them together). Someone who hits a limit gets one notice that deletes
itself, and is then ignored until the window passes.

### Automod
With `automod` on, each message in a watched channel goes to `automod_model` (`AUTOMOD_MODEL`; gpt-oss-120b
works well) with the rules and the previous few messages. It answers with a rule and a severity, and
`automod_tiers` decides what happens for each: `none`, `log` (mod log only) or `delete_warn` (delete the
message and warn the author), plus how many strikes to add. Edited messages are checked again. Bots, owners,
admins, members with Manage Messages and `automod_exempt_roles` are never checked.

### Anti-spam and strikes
`antispam` deletes floods, repeats and mass mentions and adds strikes. `raid_joins` alerts the mod log (and can
raise verification) when that many members join within a minute; this needs `MEMBERS_INTENT=true`. Strikes
expire after `strike_expiry_days`, and `escalation` lists steps like
`[{"strikes": 3, "action": "timeout", "minutes": 60}, {"strikes": 5, "action": "ban"}]`. Every action is
posted to `modlog_channel`.

## Database
Everything the bot remembers lives in one SQLite file, `DATA_DIR/smartbot.db` (or `DATABASE_FILE`): settings
changed from Discord (with who changed them and when), strikes (cleared and expired ones are kept as history),
uploaded avatars and banners, and what it last sent to Discord for each profile. It's created readable only by
the bot's account. It uses SQLite's default rollback journal, not WAL, so it works on NFS, as long as only
one bot process uses it. Back it up with `sqlite3 data/smartbot.db ".backup backup.db"`. A
`.profile-state.json` from older versions is imported once.

## Chat only

`CHAT_ONLY=true` runs the bot as a plain chatbot: it never connects to discord-mcp or any server in
`mcp_servers.json`, offers the model no tools at all (not even its own settings tools), registers no slash
commands, and does no automod or anti-spam. Only the bot process is needed. Access, per-server profiles
(name, picture, persona, model) and member rate limits still apply; set the identity in `profiles.json`.

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
  the replied-to message (with `HISTORY_MODE=replies`, no channel history: just the message, or if it's a
  reply, the chain of replies it continues, up to `HISTORY_LIMIT`, as conversation turns), mentioned users/roles/channels with IDs, attachments, and images if `VISION=true`.
- **Agent loop** (`agent.py`): runs up to `MAX_TOOL_ROUNDS` rounds of tool calls, recovers from tool errors,
  strips Qwen `<think>` blocks, and also parses `<tool_call>` text when the server's tool parser is off.
- **Safety** (`permissions.py`): the model is only *given* the tools the requester could use themselves:
  someone without Ban Members never sees `ban_member`, so a prompt injection can't reach it. A tool call
  for anything not offered is refused, and every call is checked again against the requester's
  permissions in the target channel, plus role-hierarchy checks and an @everyone/role-ping guard.
  `guildId` is always forced to the current server. Changes made through discord-mcp go to the mod log.
- **One server per request** (`scope.py`): every ID in a tool call (channels, threads, categories, roles,
  webhooks, invites, DM recipients) must belong to the server the request came from, or the call is refused;
  ID arguments it doesn't know how to check are refused too. This applies to everyone, `OWNER_IDS` included,
  so a prompt injection can't reach another server the bot is in. App emojis are shared by every server, so
  only `OWNER_IDS` can change them.
- **Access**: `OWNER_IDS` bypass the permission checks (not the one-server rule). `ACCESS` is `everyone`, `manage_guild` or `administrator`
  (the server owner always qualifies); `ALLOWED_ROLE_IDS` limits the bot to members with one of those roles.
  Both can be set per server.
- One request at a time per channel and a per-user cooldown. A reply longer than five sentences or one message goes in a thread on the question (with Create Public Threads; otherwise in the channel), split across messages as needed.

## Tests
```bash
pip install pytest && pytest
```
