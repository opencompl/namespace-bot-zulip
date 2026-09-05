# Zulip Bot

each allowed Zulip topic maps to one Claude managed agent session in a fresh devbox. `!` runs one command on the bot host.

1. install:

   ```sh
   brew install anthropics/tap/ant
   xattr -d com.apple.quarantine "$(brew --prefix)/bin/ant"
   curl -fsSL get.namespace.so/devbox/install.sh | bash
   ```

2. Anthropic console → API key:

   ```sh
   export ANTHROPIC_API_KEY=sk-ant-...
   ```

3. create the environment, note the new `env_...`:

   ```sh
   devbox login
   devbox claude managed-agents setup-environment
   ant beta:environments list --transform '{id,name}' --format jsonl
   ```

4. create the agent, note the printed `agent_...`:

   ```sh
   ant beta:agents create --transform id -r <<'YAML'
   name: zulip-bot
   model: claude-opus-5
   tools:
     - type: agent_toolset_20260401
   YAML
   ```

5. Zulip → settings → bots → add a Generic bot.

6. write the env file, fill it in, run:

   ```sh
   cat > ../namespace-bot-zulip.env <<'ENV'
   ZULIP_EMAIL=shell-bot@example.com
   ZULIP_API_KEY=
   ZULIP_SITE=https://zulip.example.com
   ANTHROPIC_API_KEY=
   SHELL_BOT_AGENT_ID=
   SHELL_BOT_ENVIRONMENT_ID=
   SHELL_BOT_ALLOWED_SENDERS=you@example.com
   SHELL_BOT_ALLOWED_STREAMS=agents
   SHELL_BOT_ALLOW_DMS=false
   SHELL_BOT_AGENT_WORKSPACE=default
   ENV
   code ../namespace-bot-zulip.env
   uv run --env-file ../namespace-bot-zulip.env bot.py
   ```

   `SHELL_BOT_ALLOWED_SENDERS` takes emails or numeric zulip user ids. realms that hide email addresses report every sender as `user<id>@<realm>`, so an email entry matches nobody there — use ids, which the bot warns about at startup.

   every setting is also a flag (`bot.py --help`), so the env file is optional and a flag wins over its env var. keep the keys in the file anyway: `!` commands already run with `ZULIP*`/`ANTHROPIC*`/`SHELL_BOT_*` stripped from their env, but flags stay readable in `ps` and `/proc/<pid>/cmdline`.

7. try it:

   ```
   @bot fix the failing tests   agent in this topic's devbox
   @bot !uname -a               one-shot shell on the bot host
   ```

   the `!` prefix in messages grants the allowlist access to the bot host. keep the env file outside the repo. use a private stream and disposable host. agent tools are auto-approved in the configured environment.

8. push, then deploy for `DURATION` (12h default):

   ```sh
   curl -fsSL https://get.namespace.so/cloud/install.sh | sh
   nsc login
   git push
   uv run --env-file ../namespace-bot-zulip.env bot.py --deploy
   ```

   nothing is built or pushed to a registry: the instance boots the uv image and `uv run --script` fetches this commit's `bot.py` from `origin`. deploy fails early if that commit is not on the remote. override with `--url`, `--name`, `--duration`.

finally, to lint and type-check the script, run:

```sh
uvx ruff check --no-cache --line-length 5000 --target-version py312 --extend-select I --ignore BLE001 bot.py
uv run --with pyright --with anthropic --with zulip --with click --with httpx --with sh pyright bot.py
```
