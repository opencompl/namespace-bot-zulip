#!/usr/bin/env python3
# pyright: strict
# /// script
# requires-python = ">=3.12"
# dependencies = ["anthropic>=1.0.0,<2", "click>=8.1", "httpx>=0.28", "sh>=2.1", "zulip>=0.9.0"]
# ///

import os
import time
from collections.abc import Callable
from typing import Any, Literal

import anthropic
import click
import httpx
import sh
import zulip


def run_shell(command: str, timeout: float = 30, limit: int = 3500) -> str:
    # run one `!` command on the bot host, secrets stripped from its env, whole process group killed on timeout
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ZULIP", "ANTHROPIC", "SHELL_BOT_"))}
    process = sh.sh("-lc", command, _env=env, _err_to_out=True, _new_session=True, _bg=True, _bg_exc=False)
    try:
        output = str(process.wait(timeout=timeout)).strip() or "[exit 0]"
    except sh.TimeoutException:
        process.kill_group()
        return "[shell timeout]"
    except sh.ErrorReturnCode as exc:
        output = f"{exc.stdout.decode(errors='replace').strip()}\n[exit {exc.exit_code}]".strip()
    return f"`$ {command}`\n```\n{output[:limit]}\n```"


type TurnStatus = Literal["idle", "terminated", "failed", "turn_error", "timeout"]


def stream_turn(api: anthropic.Anthropic, session_id: str, prompt: str, on_text: Callable[[str], bool], unsent: list[str], timeout: int, limit: int) -> tuple[TurnStatus, str]:
    # run one turn on an open stream, parking anything the caller could not relay live in unsent
    deadline = time.monotonic() + timeout
    with api.beta.sessions.events.stream(session_id, timeout=timeout) as stream:
        api.beta.sessions.events.send(session_id=session_id, events=[{"type": "user.message", "content": [{"type": "text", "text": prompt}]}])
        for event in stream:
            if event.type == "agent.message":
                text = "".join(block.text for block in event.content if block.type == "text").strip()[:limit]
                unsent += [text] if text and not on_text(text) else []
            elif event.type == "session.status_terminated":
                return "terminated", ""
            elif event.type == "session.status_idle" and event.stop_reason.type == "end_turn":
                return "idle", ""
            elif event.type == "session.status_idle" and event.stop_reason.type != "requires_action":
                return "failed", f"[agent {event.stop_reason.type}]"
            elif event.type == "session.error" and event.error.retry_status.type != "retrying":
                return "failed", "[agent session_error]"
            if time.monotonic() > deadline:
                break
    return "timeout", ""


def drain(api: anthropic.Anthropic, session_id: str, prompt: str, on_text: Callable[[str], bool], timeout: int = 300, limit: int = 3500) -> tuple[TurnStatus, str]:
    # one agent turn, reported as its status plus whatever text never made it out live
    unsent: list[str] = []
    try:
        status, note = stream_turn(api, session_id, prompt, on_text, unsent, timeout, limit)
    except Exception as exc:
        status, note = "turn_error", f"[agent stream: {exc}]"
    return status, f"{'\n\n'.join(unsent)[:limit]}\n\n{note}".strip()


def send(client: zulip.Client, target: dict[str, Any], text: str) -> bool:
    # post one message to a prepared target
    try:
        return client.send_message({**target, "content": text}).get("result") == "success"
    except Exception as exc:
        click.echo(f"send: {exc}", err=True)
        return False


def split_csv(value: str) -> set[str]:
    # "a@x.com, B@x.com" -> {"a@x.com", "b@x.com"}
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def reply_target(raw: dict[str, Any], user_id: int) -> dict[str, Any]:
    # where a reply goes: back to the same stream topic, or the same dm minus the bot itself
    recipients = raw["display_recipient"]
    if isinstance(recipients, str):
        return {"type": "stream", "to": recipients, "topic": raw["subject"]}
    return {"type": "private", "to": [item["email"] for item in recipients if item["id"] != user_id]}


def route(raw: dict[str, Any], values: dict[str, str], user_id: int, name: str) -> tuple[str, str] | None:
    # gate on sender, stream, mention and dm policy, then hand back the prompt and its session key
    if raw["sender_id"] == user_id or not {str(raw["sender_email"]).lower(), str(raw["sender_id"])} & split_csv(values["shell_bot_allowed_senders"]):
        return None
    content, recipients = str(raw["content"]).strip(), raw["display_recipient"]
    if isinstance(recipients, str):
        mentions = (f"@**{name}|{user_id}**", f"@_**{name}|{user_id}**", f"@**{name}**", f"@_**{name}**")
        mention = next((value for value in mentions if content.startswith(value)), None)
        allowed = split_csv(values["shell_bot_allowed_streams"])
        if mention is None or (allowed and recipients.lower() not in allowed):
            return None
        prompt, key = content[len(mention) :].strip(), f"stream:{raw['stream_id']}:{raw['subject']}"
    elif values["shell_bot_allow_dms"].lower() in ("1", "true", "yes"):
        prompt, key = content, "dm:" + ",".join(str(value) for value in sorted(item["id"] for item in recipients))
    else:
        return None
    return (prompt, key) if prompt and prompt != "!" else None


def agent_turn(api: anthropic.Anthropic, values: dict[str, str], sessions: dict[str, str], key: str, prompt: str, on_text: Callable[[str], bool]) -> str:
    # reuse or open this topic's session, run the turn, and drop the session unless it can be continued
    session_id = sessions.get(key)
    if session_id is None:
        session = api.beta.sessions.create(agent={"type": "agent_with_overrides", "id": values["shell_bot_agent_id"], "tools": [{"type": "agent_toolset_20260401", "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}}}]}, environment_id=values["shell_bot_environment_id"], title=f"Zulip {key}")
        session_id = sessions[key] = session.id
    status, text = drain(api, session_id, prompt, on_text)
    link = f"https://platform.claude.com/workspaces/{values['shell_bot_agent_workspace']}/sessions/{session_id}"
    if status not in ("idle", "turn_error"):
        sessions.pop(key, None)
    notes = {"idle": "", "terminated": "" if text else "[agent ended]", "timeout": f"[agent timeout: {link}]"}
    return f"{text}\n{notes.get(status, link)}".strip()


def handle(raw: dict[str, Any], client: zulip.Client, api: anthropic.Anthropic, values: dict[str, str], sessions: dict[str, str], user_id: int, name: str) -> None:
    # one inbound message: `!` goes to the shell, anything else to the agent
    if (routed := route(raw, values, user_id, name)) is None:
        return
    prompt, key = routed
    target = reply_target(raw, user_id)
    if not prompt.startswith("!"):
        send(client, target, "[working]")
    try:
        reply = run_shell(prompt[1:].strip()) if prompt.startswith("!") else agent_turn(api, values, sessions, key, prompt, lambda text: send(client, target, text))
    except Exception as exc:
        reply = f"[error: {exc}]"
    if reply:
        send(client, target, reply)


def warn_unmatchable(client: zulip.Client, values: dict[str, str]) -> None:
    # allowlist entries no account in this realm can ever send as, usually emails in a realm that hides them
    try:
        members: list[dict[str, Any]] = client.get_members()["members"]
        unmatchable = split_csv(values["shell_bot_allowed_senders"]) - {str(item["email"]).lower() for item in members} - {str(item["user_id"]) for item in members}
    except Exception as exc:
        return click.echo(f"could not check the allowlist against the realm: {exc}", err=True)
    if unmatchable:
        click.echo(f"no account in this realm sends as {', '.join(sorted(unmatchable))}: realms that hide emails report user<id>@<realm>, so list numeric user ids instead", err=True)


def listen(values: dict[str, str]) -> None:
    # open both clients, then block forever, zulip long-polls and calls handle per message
    client = zulip.Client(email=values["zulip_email"], api_key=values["zulip_api_key"], site=values["zulip_site"])
    api = anthropic.Anthropic(api_key=values["anthropic_api_key"], max_retries=0, timeout=60)
    profile: dict[str, Any] = client.get_profile()
    user_id, name, sessions = int(profile["user_id"]), str(profile["full_name"]), dict[str, str]()
    warn_unmatchable(client, values)
    print(f"bot {profile['email']}")
    client.call_on_each_message(lambda raw: handle(raw, client, api, values, sessions, user_id, name))


def deploy(values: dict[str, str], name: str, duration: str, url: str | None) -> None:
    # no image build: nsc boots a stock uv container that downloads this file from github and runs it
    if url is None:
        slug = str(sh.git("remote", "get-url", "origin")).strip().removesuffix(".git").removeprefix("https://github.com/").removeprefix("git@github.com:")
        url = f"https://raw.githubusercontent.com/{slug}/{str(sh.git('rev-parse', 'HEAD')).strip()}/bot.py"
    if httpx.head(url, follow_redirects=True).is_error:
        raise SystemExit(f"unreachable: {url} (commit and push first, or pass --url)")
    passthrough = [part for field, value in values.items() if value for part in ("-e", f"{field.upper()}={value}")]
    sh.nsc("run", "--image", "ghcr.io/astral-sh/uv:0.12.8-python3.12-trixie-slim", "--name", name, "--duration", duration, "-e", "PYTHONUNBUFFERED=1", *passthrough, "--", "uv", "run", "--script", url, _fg=True)


@click.command()
@click.option("--zulip-email", envvar="ZULIP_EMAIL", required=True)
@click.option("--zulip-api-key", envvar="ZULIP_API_KEY", required=True)
@click.option("--zulip-site", envvar="ZULIP_SITE", required=True)
@click.option("--anthropic-api-key", envvar="ANTHROPIC_API_KEY", required=True)
@click.option("--shell-bot-agent-id", envvar="SHELL_BOT_AGENT_ID", required=True)
@click.option("--shell-bot-environment-id", envvar="SHELL_BOT_ENVIRONMENT_ID", required=True)
@click.option("--shell-bot-allowed-senders", envvar="SHELL_BOT_ALLOWED_SENDERS", required=True, help="emails or numeric zulip user ids, comma separated")
@click.option("--shell-bot-allowed-streams", envvar="SHELL_BOT_ALLOWED_STREAMS", default="", help="comma separated, empty means all")
@click.option("--shell-bot-allow-dms", envvar="SHELL_BOT_ALLOW_DMS", default="", help="1, true or yes to answer dms")
@click.option("--shell-bot-agent-workspace", envvar="SHELL_BOT_AGENT_WORKSPACE", default="default", help="only used to build session links")
@click.option("--deploy", "deploying", is_flag=True, help="run this script on a namespace instance instead of here")
@click.option("--name", default="zulip-claude-bot", envvar="IMAGE_NAME", help="instance name, with --deploy")
@click.option("--duration", default="12h", envvar="DURATION", help="instance lifetime, with --deploy")
@click.option("--url", envvar="SCRIPT_URL", default=None, help="script the instance runs, defaults to this commit on origin")
def cli(deploying: bool, name: str, duration: str, url: str | None, **values: str) -> None:
    # click fills every setting from its flag or $UPPERCASE env var and refuses to start without the required ones
    deploy(values, name, duration, url) if deploying else listen(values)


if __name__ == "__main__":
    cli()
