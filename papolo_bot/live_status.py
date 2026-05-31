"""
Live status embed que se actualiza en el thread mientras el agente corre.

Uso:
    live = LiveStatus(channel=thread, loop=loop)
    result = await asyncio.to_thread(agent.send, prompt, live.on_event)
    await live.finalize("done")

El on_event se llama desde threads worker (ThreadPoolExecutor del agente).
Mutaciones de estado bajo lock, scheduling de edits via run_coroutine_threadsafe.
"""

import asyncio
import logging
import threading
import time
from collections import Counter

import discord

log = logging.getLogger("papolo-bot")

EDIT_THROTTLE_SECONDS = 1.0
MAX_LAST_EVENTS = 6

_TOOL_ICONS = {
    "read_file": "📖",
    "write_file": "📝",
    "list_dir": "📂",
    "shell": "⚙️",
    "load_skill": "🧠",
    "spawn_subagent": "🤖",
    "github_create_repo": "🐙",
    "github_push_workspace": "⬆️",
    "github_delete_repo": "🗑️",
    "coolify_create_app": "🚀",
    "coolify_set_env": "🔧",
    "coolify_deploy": "▶️",
    "coolify_status": "📡",
    "coolify_destroy_app": "💥",
}

_COLOR_WORKING = 0xFEE75C  # amarillo discord
_COLOR_DONE    = 0x57F287  # verde
_COLOR_ERROR   = 0xED4245  # rojo


def _summarize_args(name: str, args: dict) -> str:
    if not args:
        return ""
    if name == "shell":
        cmd = str(args.get("command", ""))
        cmd = cmd.replace("\n", " ⏎ ")
        return cmd[:120]
    if name in ("read_file", "write_file", "list_dir"):
        return str(args.get("path", ""))[:120]
    if name == "load_skill":
        return str(args.get("name", ""))
    if name == "spawn_subagent":
        return str(args.get("name", ""))
    if name == "github_create_repo":
        return str(args.get("name", ""))
    if name == "github_push_workspace":
        return str(args.get("repo_url", "")).rsplit("/", 1)[-1][:80]
    if name == "coolify_create_app":
        return str(args.get("repo_url", "")).rsplit("/", 1)[-1][:80]
    if name in ("coolify_deploy", "coolify_status", "coolify_set_env",
                "coolify_destroy_app"):
        return str(args.get("app_uuid", ""))[:24]
    return ""


class LiveStatus:
    def __init__(self, channel: discord.abc.Messageable,
                 loop: asyncio.AbstractEventLoop):
        self.channel = channel
        self.loop = loop
        self.message: discord.Message | None = None

        self._lock = threading.Lock()
        self._async_lock = asyncio.Lock()
        self._dirty = False
        self._last_edit = 0.0
        self._creating = False

        self.start_time = time.time()
        self.tools_called: Counter = Counter()
        self.tools_finished: Counter = Counter()
        self.errors = 0
        self.last_events: list[str] = []
        self.subagents_active: dict[str, int] = {}
        self.subagents_done: set[str] = set()
        self.final_preview: str = ""

    # --- llamado desde threads worker ---

    def on_event(self, kind: str, payload: dict) -> None:
        with self._lock:
            self._update_state(kind, payload)
        if self.message is None:
            if not self._creating:
                self._creating = True
                asyncio.run_coroutine_threadsafe(self._ensure_message(), self.loop)
            return
        now = time.time()
        if now - self._last_edit >= EDIT_THROTTLE_SECONDS and not self._dirty:
            self._dirty = True
            asyncio.run_coroutine_threadsafe(self._do_edit(), self.loop)

    def _update_state(self, kind: str, payload) -> None:
        if kind == "final":
            text = payload.get("content", "") if isinstance(payload, dict) else str(payload)
            self.final_preview = (text or "")[:200]
            return

        if not isinstance(payload, dict):
            return

        name = payload.get("name", "")
        subagent = payload.get("subagent")
        depth = payload.get("depth")

        if kind == "tool_call":
            self.tools_called[name] += 1
            args = payload.get("args") or {}
            summary = _summarize_args(name, args)
            icon = _TOOL_ICONS.get(name, "•")
            prefix = f"`[{subagent}@{depth}]` " if subagent else ""
            line = f"{prefix}{icon} `{name}`"
            if summary:
                line += f" — {discord.utils.escape_markdown(summary)[:120]}"
            self._push_event(line)

            if name == "spawn_subagent":
                sub_name = args.get("name") or "?"
                self.subagents_active[sub_name] = (depth or 0) + 1
        elif kind == "tool_result":
            self.tools_finished[name] += 1
            result = str(payload.get("result", ""))
            err = self._looks_like_error(result, name)
            if err:
                self.errors += 1
                icon = "❌"
                self._push_event(f"{icon} `{name}` → ERROR")
            # exito de tool no merece linea propia (ya se ve el call)
        elif kind == "subagent_start":
            sub_name = subagent or "?"
            self.subagents_active[sub_name] = depth or 1
            self._push_event(f"🤖 subagent `{sub_name}` (depth {depth}) start")
        elif kind == "subagent_end":
            sub_name = subagent or "?"
            self.subagents_active.pop(sub_name, None)
            self.subagents_done.add(sub_name)
            self._push_event(f"✅ subagent `{sub_name}` done")
        # 'final' ya manejado arriba

    def _push_event(self, line: str) -> None:
        self.last_events.append(line)
        if len(self.last_events) > MAX_LAST_EVENTS:
            self.last_events = self.last_events[-MAX_LAST_EVENTS:]

    @staticmethod
    def _looks_like_error(result: str, tool_name: str) -> bool:
        head = result[:80].upper()
        if head.startswith("ERROR") or head.startswith("FAIL"):
            return True
        if tool_name.startswith("coolify_") or tool_name.startswith("github_"):
            return "ERROR" in head
        if tool_name == "shell" and "exit_code:" in result[:30]:
            try:
                code = int(result.split("exit_code:")[1].strip().split()[0])
                return code != 0
            except (ValueError, IndexError):
                return False
        return False

    # --- corre en el event loop ---

    async def _ensure_message(self) -> None:
        try:
            async with self._async_lock:
                if self.message is not None:
                    return
                embed = self._build_embed("working")
                self.message = await self.channel.send(embed=embed)
                self._last_edit = time.time()
        except Exception:
            log.exception("live_status: fallo creando mensaje")
        finally:
            self._creating = False

    async def _do_edit(self) -> None:
        async with self._async_lock:
            if self.message is None:
                self._dirty = False
                return
            self._dirty = False
            self._last_edit = time.time()
            embed = self._build_embed("working")
            try:
                await self.message.edit(embed=embed)
            except discord.HTTPException as e:
                log.warning("live_status: edit fallo (%s)", e)
            except Exception:
                log.exception("live_status: edit excep")

    async def finalize(self, state: str = "done") -> None:
        async with self._async_lock:
            if self.message is None:
                # nunca hubo eventos worth posting
                return
            embed = self._build_embed(state)
            try:
                await self.message.edit(embed=embed)
            except Exception:
                log.exception("live_status: finalize edit fallo")

    # --- construccion del embed ---

    def _build_embed(self, state: str) -> discord.Embed:
        with self._lock:
            elapsed = int(time.time() - self.start_time)
            if state == "done":
                color = _COLOR_DONE
                title = f"✅ Listo · {elapsed}s"
            elif state == "error":
                color = _COLOR_ERROR
                title = f"❌ Error · {elapsed}s"
            else:
                color = _COLOR_ERROR if self.errors else _COLOR_WORKING
                title = f"🔄 Papolo trabajando · {elapsed}s"

            embed = discord.Embed(title=title, color=color)

            if self.last_events:
                body = "\n".join(self.last_events[-MAX_LAST_EVENTS:])
                embed.add_field(name="Actividad", value=body[:1024], inline=False)

            if self.tools_called:
                lines = []
                for name, n in self.tools_called.most_common(12):
                    fin = self.tools_finished.get(name, 0)
                    icon = _TOOL_ICONS.get(name, "•")
                    if fin == n:
                        lines.append(f"{icon} `{name}` ×{n}")
                    else:
                        lines.append(f"{icon} `{name}` {fin}/{n}")
                embed.add_field(name="Tools", value="\n".join(lines)[:1024], inline=True)

            if self.subagents_active or self.subagents_done:
                lines = []
                for n, d in self.subagents_active.items():
                    lines.append(f"🤖 `{n}` (depth {d}) — activo")
                for n in sorted(self.subagents_done):
                    if n not in self.subagents_active:
                        lines.append(f"✓ `{n}`")
                embed.add_field(name="Subagentes", value="\n".join(lines)[:1024], inline=True)

            if self.errors:
                embed.add_field(name="Errores", value=str(self.errors), inline=True)

            return embed
