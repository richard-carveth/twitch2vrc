#!/usr/bin/env python3
import asyncio
import json
import os
import sys
import time
import webbrowser
from pythonosc import udp_client
from twitchio.ext import commands


DEFAULT_BLOCKED_BOTS = [
    "Nightbot",
    "Wizebot",
    "Streamelements",
    "Pokemoncommunitygame",
]
DEFAULT_BLOCKED_PREFIXES = ["!"]
TOKEN_GENERATOR_URL = "https://twitchtokengenerator.com/quick/a9IivPUewe"
DEFAULT_VRC_OSC_HOST = "127.0.0.1"
DEFAULT_VRC_OSC_PORT = 9000


def _config_path() -> str:
    base = (
        sys.executable
        if getattr(sys, "frozen", False)
        else os.path.abspath(__file__)
    )
    return os.path.join(os.path.dirname(base), "config.json")


def load_config() -> tuple[str, str, set[str], tuple[str, ...], str, int]:
    path = _config_path()
    if os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
        token = cfg.get("twitch_token", "")
        channel = cfg.get("twitch_channel", "")
        blocked_users = cfg.get("blocked_users", DEFAULT_BLOCKED_BOTS)
        blocked_prefixes = cfg.get(
            "blocked_prefixes", DEFAULT_BLOCKED_PREFIXES
        )
        vrc_osc_host = cfg.get("vrc_osc_host", DEFAULT_VRC_OSC_HOST)
        vrc_osc_port = cfg.get("vrc_osc_port", DEFAULT_VRC_OSC_PORT)
        if not isinstance(blocked_users, list):
            blocked_users = DEFAULT_BLOCKED_BOTS
        if not isinstance(blocked_prefixes, list):
            blocked_prefixes = DEFAULT_BLOCKED_PREFIXES
        if not isinstance(vrc_osc_host, str):
            vrc_osc_host = DEFAULT_VRC_OSC_HOST
        if not isinstance(vrc_osc_port, int):
            vrc_osc_port = DEFAULT_VRC_OSC_PORT
        if token and channel:
            cfg_changed = False
            if "blocked_users" not in cfg:
                cfg["blocked_users"] = blocked_users
                cfg_changed = True
            if "blocked_prefixes" not in cfg:
                cfg["blocked_prefixes"] = blocked_prefixes
                cfg_changed = True
            if "vrc_osc_host" not in cfg:
                cfg["vrc_osc_host"] = vrc_osc_host
                cfg_changed = True
            if "vrc_osc_port" not in cfg:
                cfg["vrc_osc_port"] = vrc_osc_port
                cfg_changed = True
            if cfg_changed:
                with open(path, "w") as f:
                    json.dump(cfg, f, indent=2)
            blocked = {
                u.strip().lower() for u in blocked_users if u.strip()
            }
            prefixes = tuple(
                p for p in (x.strip() for x in blocked_prefixes) if p
            )

            return (
                token, channel, blocked, prefixes, vrc_osc_host, vrc_osc_port
            )
        print("config.json is incomplete — please re-enter your details.\n")

    if not token:
        try:
            webbrowser.open(TOKEN_GENERATOR_URL, new=2)
            print(
                "Opened token generator in your browser: "
                f"{TOKEN_GENERATOR_URL}"
            )
        except Exception:
            print("Could not open your browser automatically.")

    print("──────────────────────── First-run setup ────────────────────────")
    print(f"Generate a token at {TOKEN_GENERATOR_URL}")
    print("Required scope: chat:read\n")
    token = input(
        "Paste your access token: "
    ).strip()
    channel = input("Twitch channel name to watch: ").strip().lower()

    if not token.startswith("oauth:"):
        token = "oauth:" + token

    with open(path, "w") as f:
        json.dump(
            {
                "twitch_token": token,
                "twitch_channel": channel,
                "blocked_users": DEFAULT_BLOCKED_BOTS,
                "blocked_prefixes": DEFAULT_BLOCKED_PREFIXES,
                "vrc_osc_host": DEFAULT_VRC_OSC_HOST,
                "vrc_osc_port": DEFAULT_VRC_OSC_PORT,
            },
            f,
            indent=2,
        )
    print(f"\nSaved to {path}\n")
    return (
        token,
        channel,
        {u.lower() for u in DEFAULT_BLOCKED_BOTS},
        tuple(DEFAULT_BLOCKED_PREFIXES),
        DEFAULT_VRC_OSC_HOST,
        DEFAULT_VRC_OSC_PORT,
    )


(
    TWITCH_TOKEN,
    TWITCH_CHANNEL,
    BLOCKED_USERS,
    BLOCKED_PREFIXES,
    VRC_OSC_HOST,
    VRC_OSC_PORT,
) = load_config()


MAX_CHARS = 144

T_MIN_DISPLAY = 5.0
T_REFRESH = 0.1
T_OSC_RATE_LIMIT = 1.1

_osc = udp_client.SimpleUDPClient(VRC_OSC_HOST, VRC_OSC_PORT)


def send_chatbox(text: str) -> None:
    _osc.send_message("/chatbox/input", [text, True])


def split_into_blocks(username: str, message: str) -> list[str]:
    prefix = f"{username}: "
    max_chunk = MAX_CHARS - len(prefix)

    if max_chunk <= 0:
        prefix = f"{username[:MAX_CHARS - 100]}: "
        max_chunk = MAX_CHARS - len(prefix)

    words = message.split()
    blocks = []
    chunk = ""

    for word in words:
        if not chunk:
            chunk = word
        elif len(chunk) + 1 + len(word) <= max_chunk:
            chunk += " " + word
        else:
            blocks.append(prefix + chunk)
            chunk = word

    if chunk:
        blocks.append(prefix + chunk)

    return blocks if blocks else [prefix.rstrip()]


def strip_emotes(content: str, emotes_tag: str | None) -> str:
    if not emotes_tag:
        return content

    ranges: list[tuple[int, int]] = []
    for emote_part in emotes_tag.split("/"):
        if ":" not in emote_part:
            continue
        _, positions = emote_part.split(":", 1)
        for pos in positions.split(","):
            if "-" not in pos:
                continue
            start, end = pos.split("-")
            ranges.append((int(start), int(end)))

    chars = list(content)
    for start, end in sorted(ranges, reverse=True):
        del chars[start:end + 1]

    return " ".join("".join(chars).split())


class DisplayItem:
    def __init__(self, text: str) -> None:
        self.text = text
        self._shown_at: float | None = None

    def mark_shown(self) -> None:
        if self._shown_at is None:
            self._shown_at = time.monotonic()

    @property
    def age(self) -> float:
        return (time.monotonic() - self._shown_at) if self._shown_at else 0.0

    @property
    def eligible_for_removal(self) -> bool:
        return self._shown_at is not None and self.age >= T_MIN_DISPLAY


class DisplayManager:
    MAX_QUEUE_SIZE = 25

    def __init__(self) -> None:
        self.queue:  list[DisplayItem] = []
        self.active: list[DisplayItem] = []

    def enqueue(self, username: str, message: str) -> None:
        if len(self.queue) >= self.MAX_QUEUE_SIZE:
            self.queue.pop(0)
        full = f"{username}: {message}"
        if len(full) <= MAX_CHARS:
            self.queue.append(DisplayItem(full))
        else:
            for block in split_into_blocks(username, message):
                self.queue.append(DisplayItem(block))

    @staticmethod
    def _render(items: list[DisplayItem]) -> str:
        return "\n---\n".join(i.text for i in items)

    def _fits(self, candidate: DisplayItem) -> bool:
        return len(self._render(self.active + [candidate])) <= MAX_CHARS

    def _try_advance(self) -> bool:
        if self.queue and self._fits(self.queue[0]):
            item = self.queue.pop(0)
            item.mark_shown()
            self.active.append(item)
            return True
        if self.active and self.active[0].eligible_for_removal:
            self.active.pop(0)
            return True
        return False

    def update(self) -> str | None:
        for item in self.active:
            item.mark_shown()
        if not self.queue:
            return None
        changed = False
        while self._try_advance():
            changed = True
        return self._render(self.active) if changed else None


manager = DisplayManager()


async def display_loop() -> None:
    last_sent: str | None = None
    last_sent_time: float = 0.0
    pending: str | None = None

    while True:
        result = manager.update()

        if result is not None:
            pending = result

        if pending is not None and pending != last_sent:
            now = time.monotonic()
            if now - last_sent_time >= T_OSC_RATE_LIMIT:
                send_chatbox(pending)
                last_sent = pending
                last_sent_time = now
                if pending:
                    print(f"[ChatBox]\n{pending}\n{'─' * 40}")
                else:
                    print("[ChatBox] <cleared>")

        await asyncio.sleep(T_REFRESH)


class TwitchBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            token=TWITCH_TOKEN,
            prefix="!",
            initial_channels=[TWITCH_CHANNEL],
        )

    async def event_ready(self) -> None:
        print(
            f"✓ Twitch connected as {self.nick} "
            f"— watching #{TWITCH_CHANNEL}"
        )
        print(f"✓ Sending OSC to {VRC_OSC_HOST}:{VRC_OSC_PORT}")
        print("─" * 40)

    async def event_message(self, message) -> None:
        if message.echo:
            return
        if message.author is None:
            return
        username = message.author.display_name
        content = message.content or ""

        if any(content.startswith(prefix) for prefix in BLOCKED_PREFIXES):
            return

        if username.strip().lower() in BLOCKED_USERS:
            return

        emotes_tag = (message.tags or {}).get("emotes")
        content = strip_emotes(content, emotes_tag)

        if not content.strip():
            return

        print(f"[Twitch] {username}: {content}")
        manager.enqueue(username, content)


async def main() -> None:
    bot = TwitchBot()
    try:
        await asyncio.gather(
            bot.start(),
            display_loop(),
        )
    finally:
        send_chatbox("")
        print("\n[ChatBox] Cleared on exit.")


if __name__ == "__main__":
    asyncio.run(main())
