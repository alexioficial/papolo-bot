"""
Live status en Discord mientras el agente corre.

Un mensaje-embed principal (el turno completo) + un embed propio por cada subagente
de nivel 1 que el orquestador spawnea (los que corren en paralelo). Cada panel muestra
su actividad y, al terminar, se pone verde con "Done".

Anti-rate-limit: un unico render loop hace COMO MUCHO una accion de Discord por tick
(crear un mensaje o editar uno), en round-robin entre el principal y los paneles. Nunca
se editan todos a la vez; se cicla. Se priorizan (1) crear paneles nuevos y (2) mostrar
los que recien terminaron; el resto de las actualizaciones ciclan.

Cuando un subagente termina (Done/Error) y su embed ya reflejo ese estado terminal, el
panel se "retira": queda congelado en pantalla y sale del ciclo de updates (no vuelve a
editarse, ni en el round-robin ni en el flush final), liberando ticks para lo que sigue
activo.

Uso:
    live = LiveStatus(channel=thread, loop=loop)
    live.start()
    result = await asyncio.to_thread(agent.send, prompt, live.on_event)
    await live.finalize("done")

on_event se llama desde threads worker (ThreadPoolExecutor del agente): solo muta estado
bajo _lock. Todo el I/O de Discord ocurre en el event loop (render loop / finalize).
"""

import asyncio
import logging
import threading
import time
from collections import Counter

import discord

log = logging.getLogger("papolo-bot")

EDIT_THROTTLE_SECONDS = 1.0   # como mucho 1 accion de Discord por este intervalo
MAX_LAST_EVENTS = 6
MAX_PANELS = 8                # tope de embeds de subagente para no inundar el canal

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


class _Panel:
    """Un mensaje-embed: el principal o el de un subagente."""

    def __init__(self, label: str, kind: str, start_time: float):
        self.label = label            # "Papolo" o el nombre del subagente
        self.kind = kind              # "main" | "subagent"
        self.start_time = start_time
        self.message: discord.Message | None = None
        self.wants_message = False    # hay que crear su mensaje
        self.dirty = False            # cambio el estado desde el ultimo edit
        self.final_pending = False    # transiciono a done/error y falta reflejarlo
        self.retired = False          # subagente terminal ya renderizado: fuera del ciclo
        self.state = "working"        # working | done | error
        self.events: list[str] = []
        self.tools_called: Counter = Counter()
        self.tools_finished: Counter = Counter()
        self.errors = 0
        self.final_preview = ""
        self.task = ""

    def push(self, line: str) -> None:
        self.events.append(line)
        if len(self.events) > MAX_LAST_EVENTS:
            self.events = self.events[-MAX_LAST_EVENTS:]
        self.dirty = True


class LiveStatus:
    def __init__(self, channel: discord.abc.Messageable,
                 loop: asyncio.AbstractEventLoop):
        self.channel = channel
        self.loop = loop

        self._lock = threading.Lock()       # protege el estado mutado desde workers
        self._io_lock = asyncio.Lock()      # serializa I/O de Discord (tick vs finalize)
        self._task: asyncio.Task | None = None
        self._rr = 0                        # cursor de round-robin

        now = time.time()
        self.main = _Panel(label="Papolo", kind="main", start_time=now)
        self.panels: dict[str, _Panel] = {}  # sub_id -> panel
        self.panel_order: list[str] = []

        # Roster compacto para el embed principal (solo display; puede juntar homonimos).
        self.subagents_active: dict[str, int] = {}
        self.subagents_done: set[str] = set()

    # ── arranque / parada ───────────────────────────────────────────────

    def start(self) -> None:
        """Arranca el render loop. Llamar desde el event loop (handler async)."""
        with self._lock:
            self.main.wants_message = True
        self._task = self.loop.create_task(self._render_loop())

    async def _render_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(EDIT_THROTTLE_SECONDS)
                await self._tick()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("live_status: render loop murio")

    # ── eventos (desde threads worker) ──────────────────────────────────

    def on_event(self, kind: str, payload) -> None:
        with self._lock:
            self._update(kind, payload)

    def _update(self, kind: str, payload) -> None:
        if kind == "final":
            text = payload.get("content", "") if isinstance(payload, dict) else str(payload)
            self.main.final_preview = (text or "")[:1000]
            self.main.dirty = True
            return

        if not isinstance(payload, dict):
            return

        name = payload.get("name", "")
        subagent = payload.get("subagent")
        sub_id = payload.get("sub_id") or subagent  # fallback si el motor es viejo
        depth = payload.get("depth")

        if kind == "subagent_start":
            if subagent:
                self.subagents_active[subagent] = depth or 1
            self.main.push(f"🤖 subagent `{subagent}` (depth {depth}) start")
            # Panel propio solo para subagentes de nivel 1 (los paralelos del orquestador).
            if (depth == 1 and sub_id and sub_id not in self.panels
                    and len(self.panels) < MAX_PANELS):
                p = _Panel(label=subagent or "?", kind="subagent", start_time=time.time())
                p.task = str(payload.get("task", ""))[:180]
                p.wants_message = True
                p.dirty = True
                self.panels[sub_id] = p
                self.panel_order.append(sub_id)
            return

        if kind == "subagent_end":
            if subagent:
                self.subagents_active.pop(subagent, None)
                self.subagents_done.add(subagent)
            self.main.push(f"✅ subagent `{subagent}` done")
            p = self.panels.get(sub_id)
            if p:
                final = str(payload.get("final", ""))
                p.state = "error" if self._looks_like_error(final, "") else "done"
                p.final_preview = final[:500]
                p.final_pending = True
                p.dirty = True
            return

        # tool_call / tool_result — a su panel si esta tageado a uno conocido, sino al main.
        target = self.panels.get(sub_id) if sub_id in self.panels else None

        if kind == "tool_call":
            p = target or self.main
            p.tools_called[name] += 1
            args = payload.get("args") or {}
            summary = _summarize_args(name, args)
            icon = _TOOL_ICONS.get(name, "•")
            # En el panel del propio subagente no hace falta el prefijo [name@depth];
            # solo lo usamos cuando el evento cae en el main (subagente sin panel / anidado).
            prefix = "" if target else (f"`[{subagent}@{depth}]` " if subagent else "")
            line = f"{prefix}{icon} `{name}`"
            if summary:
                line += f" — {discord.utils.escape_markdown(summary)[:100]}"
            p.push(line)

        elif kind == "tool_result":
            p = target or self.main
            p.tools_finished[name] += 1
            result = str(payload.get("result", ""))
            if self._looks_like_error(result, name):
                p.errors += 1
                p.push(f"❌ `{name}` → ERROR")

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

    # ── render loop: UNA accion de Discord por tick ─────────────────────

    async def _tick(self) -> None:
        async with self._io_lock:
            action = self._next_action()
            if action is None:
                return
            kind, unit = action
            if kind == "create":
                await self._create_message(unit)
            else:
                await self._edit_message(unit)

    def _next_action(self):
        """Elige la proxima accion (create/edit) de UNA sola unidad. Round-robin con
        prioridad: (1) crear paneles nuevos, (2) reflejar los que terminaron, (3) ciclar
        los sucios. Devuelve (kind, panel) o None."""
        with self._lock:
            units = [self.main] + [self.panels[k] for k in self.panel_order]
            # 1. crear mensajes pendientes (paneles nuevos aparecen ~1/tick)
            for u in units:
                if u.message is None and u.wants_message and not u.retired:
                    return ("create", u)
            # 2. reflejar transiciones a done/error cuanto antes
            for u in units:
                if u.message is not None and u.final_pending and not u.retired:
                    return ("edit", u)
            # 3. ciclar los sucios — los subagentes ya retirados (Done/Error
            #    renderizado) quedan fuera del round-robin.
            active = [u for u in units if not u.retired]
            n = len(active)
            for i in range(n):
                u = active[(self._rr + i) % n]
                if u.message is not None and u.dirty:
                    self._rr = (self._rr + i + 1) % n
                    return ("edit", u)
        return None

    async def _create_message(self, u: _Panel) -> None:
        with self._lock:
            embed = self._build_embed(u)
            u.dirty = False
            u.final_pending = False
        try:
            u.message = await self.channel.send(embed=embed)
        except Exception:
            log.exception("live_status: send fallo")

    async def _edit_message(self, u: _Panel) -> None:
        if u.message is None:
            return
        with self._lock:
            embed = self._build_embed(u)
            u.dirty = False
            u.final_pending = False
        try:
            await u.message.edit(embed=embed)
        except discord.HTTPException as e:
            log.warning("live_status: edit fallo (%s)", e)
        except Exception:
            log.exception("live_status: edit excep")
        else:
            # Si el subagente ya mostro su estado terminal, sacarlo del ciclo:
            # su embed queda congelado en Done/Error y no se vuelve a tocar.
            if u.kind == "subagent" and u.state in ("done", "error"):
                with self._lock:
                    u.retired = True

    # ── finalize: parar el loop y volcar todo al estado terminal ────────

    async def finalize(self, state: str = "done") -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        with self._lock:
            self.main.state = state
            self.main.final_pending = True
            self.main.dirty = True
            # Subagentes que quedaron 'working' (no emitieron end) se dan por terminados.
            # Los ya retirados (Done/Error renderizado) no se vuelven a tocar.
            for p in self.panels.values():
                if p.retired:
                    continue
                if p.state == "working":
                    p.state = "done"
                p.final_pending = True
                p.dirty = True
        await self._flush_all()

    async def _flush_all(self) -> None:
        async with self._io_lock:
            units = [self.main] + [self.panels[k] for k in self.panel_order]
            for u in units:
                # discord.py maneja el 429 con backoff automatico, asi que este burst
                # secuencial es seguro aunque haya varios paneles.
                if u.retired:
                    continue
                if u.message is None and u.wants_message:
                    await self._create_message(u)
                elif u.message is not None:
                    await self._edit_message(u)

    # ── construccion del embed ──────────────────────────────────────────

    def _build_embed(self, p: _Panel) -> discord.Embed:
        """Construye el embed de un panel. Llamar con _lock tomado."""
        elapsed = int(time.time() - p.start_time)
        if p.state == "done":
            color = _COLOR_DONE
            title = f"✅ {p.label} · Done · {elapsed}s"
        elif p.state == "error":
            color = _COLOR_ERROR
            title = f"❌ {p.label} · Error · {elapsed}s"
        else:
            color = _COLOR_ERROR if p.errors else _COLOR_WORKING
            title = f"🔄 {p.label} · trabajando · {elapsed}s"

        embed = discord.Embed(title=title, color=color)

        if p.kind == "subagent" and p.task:
            embed.description = f"_{discord.utils.escape_markdown(p.task)[:200]}_"

        if p.events:
            body = "\n".join(p.events[-MAX_LAST_EVENTS:])
            embed.add_field(name="Actividad", value=body[:1024], inline=False)

        if p.tools_called:
            lines = []
            for name, n in p.tools_called.most_common(10):
                fin = p.tools_finished.get(name, 0)
                icon = _TOOL_ICONS.get(name, "•")
                if fin >= n:
                    lines.append(f"{icon} `{name}` ×{n}")
                else:
                    lines.append(f"{icon} `{name}` {fin}/{n}")
            embed.add_field(name="Tools", value="\n".join(lines)[:1024], inline=True)

        # Roster compacto de subagentes, solo en el embed principal.
        if p.kind == "main" and (self.subagents_active or self.subagents_done):
            lines = []
            for n, d in self.subagents_active.items():
                lines.append(f"🤖 `{n}` (depth {d}) — activo")
            for n in sorted(self.subagents_done):
                if n not in self.subagents_active:
                    lines.append(f"✓ `{n}`")
            embed.add_field(name="Subagentes", value="\n".join(lines)[:1024], inline=True)

        if p.errors:
            embed.add_field(name="Errores", value=str(p.errors), inline=True)

        if p.kind == "main" and p.state != "working" and p.final_preview:
            embed.add_field(name="Resultado", value=p.final_preview[:1024], inline=False)

        return embed
