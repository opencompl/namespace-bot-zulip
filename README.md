# Zulip Shell Bot

A Zulip bot that runs shell commands on the machine it's hosted on and replies
with the output. Direct-message it a command, or @-mention it in a stream.

> ⚠️ **This is remote code execution over chat.** Anyone on the allowlist — or
> anyone who steals the bot's API key — gets a shell on the host. Run it as an
> unprivileged user, keep the allowlist tight, and never commit `zuliprc`.

## Setup

1. Create a **Generic bot** in Zulip: *Settings → Personal / Organization →
   Bots → Add a new bot*. Download its `zuliprc`.

2. Put the `zuliprc` next to `shell_bot.py` (or point `ZULIPRC` at it). See
   [zuliprc.example](zuliprc.example) for the format.

3. Install dependencies:

   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. Set the allowlist and run:

   ```bash
   export SHELL_BOT_ALLOWED_SENDERS="you@example.com,teammate@example.com"
   python shell_bot.py
   ```

   The bot refuses to start if the allowlist is empty.

   Allowlist entries may be emails or numeric Zulip user IDs. If your
   organization hides email addresses, Zulip reports every sender as
   `user<id>@<realm>` and an entry with a real email will never match — use
   user IDs there. The bot prints a warning at startup listing every
   allowlisted address no account in the realm can send as, along with the
   user IDs to use instead.

## Usage

Commands must start with `!` (configurable via `SHELL_BOT_PREFIX`); anything else
is ignored, so the bot stays quiet in normal conversation.

- **Direct message:** send the bot `!ls -la` — it replies in the DM.
- **Stream:** `@**shell-bot** !df -h` — it replies in the same topic.

### One shell per thread

Each Zulip thread (a stream topic, or a DM conversation) gets its **own
persistent bash session**, so state carries across commands within a thread and
threads are isolated from one another:

```
!cd /var/log      # in thread A
!pwd              -> /var/log          (thread A remembers)
!export TOKEN=abc
!echo $TOKEN      -> abc

# a different topic / DM is a completely separate shell:
!pwd              -> /                 (thread B, unaffected)
```

- `!:reset` — discard this thread's shell and start a fresh one.
- A command that hits the timeout resets that thread's shell (only that thread).
- Interactive commands (`sudo` prompts, `cat` with no args, `read`) don't hang —
  their stdin is `/dev/null`, so they get EOF immediately.
- Up to `SHELL_BOT_MAX_SESSIONS` shells are kept alive; the least-recently-used
  is evicted beyond that.

### Claude assistant (`?` prefix)

Messages prefixed with `?` are answered by Claude instead of the shell, with
per-thread conversation memory:

```
?summarize what df -h reported above
?now suggest which mount to clean up first   # same thread → Claude remembers
```

- Requires `ANTHROPIC_API_KEY`. Without it, `?` messages report that Claude is
  disabled; the shell (`!`) works regardless.
- `?:reset` clears this thread's Claude history (separate from `!:reset`).
- Defaults to `claude-opus-5`; tune with `SHELL_BOT_CLAUDE_*` (see the table).
- The API key is scrubbed from the per-thread shells, so `!env` can't read it.

> This is the Claude **Messages API** — a chat assistant with no tools. For an
> agent that actually *runs* commands, see the `%` mode below.

### Namespace agent (`%` prefix)

Messages prefixed with `%` drive a Claude **Managed Agent** session — one per
thread — that executes tools (bash, file ops, web) in an ephemeral **Namespace
Devbox**, via the [Claude-on-Namespace integration](https://namespace.so/docs/integrations/claude).
Anthropic runs the agent loop; Namespace runs the Devbox; the bot is the client
that relays messages into the Zulip thread.

```
%clone the repo, run the tests, and summarize failures
%now fix the first failure          # same thread → same session & Devbox
```

- Requires `SHELL_BOT_AGENT_ID` and `SHELL_BOT_ENVIRONMENT_ID` (from `devbox
  claude managed-agents setup-environment`), plus `ANTHROPIC_API_KEY`.
- The bot posts a "working…" ack, then the agent's reply. **Runs are autonomous
  and can take minutes**; the bot processes messages serially, so a long agent
  run blocks other bot messages until it finishes (`SHELL_BOT_AGENT_TIMEOUT`,
  default 300s, bounds the wait — the session keeps running in the console after).
- `%:reset` starts a fresh session (new Devbox) for the thread.
- Tool-approval prompts (`always_ask` policies) aren't serviced yet — the bot
  links you to the Anthropic console to continue those.

## Deploy on Namespace (namespace.so)

Run the bot inside a container on a Namespace VM that's created automatically.
The bot connects *outbound* to Zulip (long-poll), so the VM needs no inbound
ingress — the exposed thing is that VM's shell, reachable through the bot.

One-time:

```bash
nsc login
nsc docker login   # lets the remote builder push to your workspace registry
```

Configure and deploy:

```bash
cp .env.example .env   # fill in ZULIP_* and SHELL_BOT_ALLOWED_SENDERS
./deploy.sh
```

`deploy.sh` builds the image with the Namespace remote builder, pushes it to
`nscr.io/<workspace>/zulip-shell-bot`, then runs `nsc run` — which **creates a
fresh ephemeral VM automatically** and starts the bot on it, injecting the
Zulip credentials and allowlist as env vars (no secrets baked into the image).

Manage the running VM:

```bash
nsc list                 # find the instance id
nsc logs <instance-id>   # view bot output
nsc ssh <instance-id>    # shell into the VM directly
nsc destroy <instance-id># tear it down
```

### Lifetime / keeping it alive

`nsc run` VMs are **ephemeral** — they're destroyed when `--duration` elapses
(default `12h` in `deploy.sh`; set `DURATION=24h` etc.). When the VM dies the bot
stops. To keep a bot running continuously, re-run `./deploy.sh` on a schedule
(e.g. cron / a Namespace GitHub Actions runner) so a fresh VM is provisioned
each cycle, or raise `DURATION`. Each new VM is clean — nothing persists between
runs unless you attach a `persistent` volume.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ZULIPRC` | `zuliprc` | Path to the bot's credentials file |
| `SHELL_BOT_ALLOWED_SENDERS` | *(required)* | Comma-separated sender emails and/or numeric Zulip user IDs allowed to run commands |
| `SHELL_BOT_ALLOWED_STREAMS` | *(all)* | Comma-separated channel names the bot acts in; empty = all |
| `SHELL_BOT_ALLOW_DMS` | `true` (`false` if streams set) | Whether to honor direct messages |
| `SHELL_BOT_PREFIX` | `!` | Messages must start with this to run as a command; others ignored |
| `SHELL_BOT_TIMEOUT` | `30` | Per-command timeout, seconds |
| `SHELL_BOT_SHELL` | `/bin/bash` | Shell used to run commands |
| `SHELL_BOT_CWD` | bot's cwd | Working directory new thread shells start in |
| `SHELL_BOT_MAX_OUTPUT` | `3500` | Max reply characters before truncation |
| `SHELL_BOT_MAX_SESSIONS` | `50` | Max concurrent per-thread shells (LRU-evicted) |
| `ANTHROPIC_API_KEY` | *(unset)* | Enables the Claude assistant (`?` prefix) |
| `SHELL_BOT_CLAUDE_PREFIX` | `?` | Prefix that routes a message to Claude |
| `SHELL_BOT_CLAUDE_MODEL` | `claude-opus-5` | Claude model id |
| `SHELL_BOT_CLAUDE_EFFORT` | `medium` | Reasoning effort: low/medium/high/xhigh/max |
| `SHELL_BOT_CLAUDE_MAX_TOKENS` | `4096` | Max reply tokens |
| `SHELL_BOT_CLAUDE_MAX_HISTORY` | `20` | Messages kept per thread before trimming |
| `SHELL_BOT_CLAUDE_MAX_CONVERSATIONS` | `200` | Max threads with Claude history (LRU-evicted) |
| `SHELL_BOT_CLAUDE_SYSTEM` | *(built-in)* | System prompt for the assistant |

## Security notes

- Commands run through `/bin/bash -c`, so full shell syntax (pipes, `&&`,
  redirects) works — and so does anything destructive. The allowlist is the only
  gate.
- Consider running under a dedicated low-privilege user, in a container, or in a
  VM you don't mind losing.
- Rotate the bot's API key if you suspect it leaked.
