import asyncio
import json
import logging
import os
import subprocess
import tempfile
import uuid as uuid_lib
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from papolo import Agent
from papolo import deploy as papolo_deploy
from papolo.skills import list_skills
from papolo.subagents import list_subagents

from . import active_turns, attachments, confirmations, conversations, db, models
from .conversations import bind_bot, get_or_create_agent, persist_agent, workspace_path
from .discord_helpers import fetch_reply_context, format_user_turn, send_ephemeral_long, send_long
from .live_status import LiveStatus

log = logging.getLogger("papolo-bot")


def _build_transcript_events(conv: dict) -> list[dict]:
    """Transcripcion COMPLETA como lista de eventos JSON — nada truncado.

    Cada evento es un dict con clave `type` que lo discrimina; no todos comparten
    la misma estructura (por diseno). Se vuelca TODO:
      - `meta`: datos de la conversacion + conteos.
      - `discord_message`: cada mensaje del lado Discord con todas sus columnas.
      - `agent_message`: cada mensaje openai-style del loop interno del agente,
        con sus `tool_calls` (argumentos enteros) y, para los mensajes `tool`, el
        resultado completo del tool en `content` (+ `tool_name` resuelto).
      - `deployment`: cada fila del ledger de deployments de la conversacion.

    Pensado para analisis maquina-legible (p.ej. reconstruir que hizo cada
    subagente y en que orden), no para leer a ojo.
    """
    short = conv["uuid"].split("-")[0]
    discord_msgs = db.get_messages(conv["uuid"])
    agent_state = db.load_agent_state(conv["uuid"]) or []
    deployments = db.get_deployments_by_conv(conv["uuid"])

    events: list[dict] = []
    _seq = 0

    def emit(ev: dict) -> None:
        nonlocal _seq
        events.append({"seq": _seq, **ev})
        _seq += 1

    emit({
        "type": "meta",
        "short": short,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "conversation": conv,
        "counts": {
            "discord_messages": len(discord_msgs),
            "agent_messages": len(agent_state),
            "deployments": len(deployments),
        },
    })

    for i, m in enumerate(discord_msgs):
        emit({"type": "discord_message", "stream_index": i, **m})

    # Los mensajes 'tool' se guardan sin 'name'; mapeamos tool_call_id -> name
    # desde los tool_calls de los mensajes assistant para poder rotularlos.
    id_to_name: dict[str, str] = {}
    for msg in agent_state:
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if tc.get("id"):
                id_to_name[tc["id"]] = fn.get("name", "?")

    for i, msg in enumerate(agent_state):
        ev = {"type": "agent_message", "index": i, **msg}
        if msg.get("role") == "tool":
            ev["tool_name"] = id_to_name.get(msg.get("tool_call_id"), msg.get("name"))
        emit(ev)

    for i, d in enumerate(deployments):
        emit({"type": "deployment", "stream_index": i, **d})

    return events


def _purge_field(lines: list[str], empty: str = "—") -> str:
    """Une lineas respetando el limite de 1024 chars por field de embed."""
    if not lines:
        return empty
    text = "\n".join(lines)
    if len(text) > 1000:
        text = text[:1000].rsplit("\n", 1)[0] + f"\n… (+{len(lines)} en total)"
    return text or empty


def _purge_preview_embed(targets: dict) -> discord.Embed:
    emb = discord.Embed(
        title="🧹 Limpieza total de Papolo",
        description=(
            "Voy a borrar **de forma irreversible** SOLO lo que Papolo creó en tu "
            "GitHub, Coolify y MongoDB. Nada más tuyo se toca. Revisá la lista y confirmá."
        ),
        color=0xED4245,
    )

    def svc_lines(enabled, items, err, fmt):
        if not enabled:
            return ["(no configurado)"]
        if err:
            return [f"⚠️ {err}"]
        if not items:
            return ["(nada)"]
        return [fmt(x) for x in items]

    gh, cf, mo = targets["github"], targets["coolify"], targets["mongo"]
    emb.add_field(
        name=f"GitHub · {len(gh['repos'])} repos",
        value=_purge_field(svc_lines(gh["enabled"], gh["repos"], gh["error"], lambda n: f"• {n}")),
        inline=False,
    )
    emb.add_field(
        name=f"Coolify · {len(cf['apps'])} apps",
        value=_purge_field(svc_lines(cf["enabled"], cf["apps"], cf["error"],
                                     lambda a: f"• {a['name']} ({a['fqdn']})")),
        inline=False,
    )
    emb.add_field(
        name=f"MongoDB · {len(mo['dbs'])} bases",
        value=_purge_field(svc_lines(mo["enabled"], mo["dbs"], mo["error"], lambda d: f"• {d}")),
        inline=False,
    )
    return emb


def _purge_results_embed(results: dict) -> discord.Embed:
    c = results["counts"]
    any_fail = any(not x["ok"] for k in ("github", "coolify", "mongo") for x in results[k])
    emb = discord.Embed(
        title="🧹 Limpieza con avisos" if any_fail else "🧹 Limpieza completada",
        color=0xFEE75C if any_fail else 0x57F287,
    )

    def svc_block(items):
        oks = sum(1 for x in items if x["ok"])
        fails = [x for x in items if not x["ok"]]
        lines = [f"✅ {oks} borrados"]
        for f in fails[:8]:
            lines.append(f"❌ {f['target']}: {f['msg']}")
        return _purge_field(lines)

    emb.add_field(name=f"GitHub ({c['github']})", value=svc_block(results["github"]), inline=False)
    emb.add_field(name=f"Coolify ({c['coolify']})", value=svc_block(results["coolify"]), inline=False)
    emb.add_field(name=f"MongoDB ({c['mongo']})", value=svc_block(results["mongo"]), inline=False)
    return emb


class _PurgeConfirmView(discord.ui.View):
    """Confirmacion de doble paso para /papolo-purge (op destructiva e irreversible)."""

    def __init__(self, invoker_id: int, targets: dict):
        super().__init__(timeout=120)
        self.invoker_id = invoker_id
        self.targets = targets
        self.done = False
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Solo quien invocó el comando puede confirmar.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Borrar todo", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="🧹 Borrando todo lo que Papolo creó… (puede tardar unos segundos)",
            embed=None, view=self,
        )
        try:
            results = await asyncio.to_thread(papolo_deploy.purge_execute, self.targets)
            await asyncio.to_thread(db.delete_all_deployments)
        except Exception as e:
            log.exception("purge error")
            await interaction.edit_original_response(
                content=f"ERROR durante la purga: {e}", embed=None, view=None
            )
            self.stop()
            return
        await interaction.edit_original_response(
            content=None, embed=_purge_results_embed(results), view=None
        )
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        await interaction.response.edit_message(
            content="Cancelado. No se borró nada.", embed=None, view=None
        )
        self.stop()

    async def on_timeout(self):
        if self.done or self.message is None:
            return
        try:
            await self.message.edit(
                content="Expiró la confirmación. No se borró nada.", embed=None, view=None
            )
        except discord.HTTPException:
            pass


async def _run_agent_turn(agent: Agent, prompt: str,
                           channel: discord.abc.Messageable | None = None,
                           conv_uuid: str | None = None) -> str:
    # Registrar el turno para que /papolo-stop pueda cancelarlo mientras corre.
    if conv_uuid:
        active_turns.register(conv_uuid, agent)
    try:
        if channel is None:
            return await asyncio.to_thread(agent.send, prompt)
        loop = asyncio.get_running_loop()
        live = LiveStatus(channel=channel, loop=loop)
        live.start()
        try:
            result = await asyncio.to_thread(agent.send, prompt, live.on_event)
        except Exception:
            await live.finalize("error")
            raise
        await live.finalize("done")
        return result
    finally:
        if conv_uuid:
            active_turns.unregister(conv_uuid)


async def _model_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Sugiere los modelos de DeepSeek disponibles mientras el usuario escribe.
    available_models() cachea, asi que salvo el primer fetch responde al instante;
    igual lo corremos en un thread con timeout para no colgar el callback (~3s de budget)."""
    try:
        avail = await asyncio.wait_for(asyncio.to_thread(models.available_models), timeout=2.5)
    except Exception:
        avail = models.KNOWN_MODELS
    cur = (current or "").lower()
    matches = [m for m in avail if cur in m.lower()] or avail
    return [app_commands.Choice(name=m, value=m) for m in matches[:25]]


def setup(bot: commands.Bot) -> None:
    @bot.event
    async def on_ready():
        log.info("Logueado como %s (id=%s)", bot.user, bot.user.id if bot.user else "?")
        bind_bot(bot, asyncio.get_running_loop())
        guild_id = os.environ.get("DISCORD_GUILD_ID")
        try:
            if guild_id:
                guild = discord.Object(id=int(guild_id))
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                log.info("Slash commands sincronizados en guild %s: %d", guild_id, len(synced))
            else:
                synced = await bot.tree.sync()
                log.info("Slash commands sincronizados globalmente: %d", len(synced))
        except Exception:
            log.exception("Error sincronizando slash commands")

    @bot.tree.command(name="papolo", description="Inicia una conversacion con Papolo en un thread")
    @app_commands.describe(prompt="Pregunta o tarea inicial")
    async def papolo_cmd(interaction: discord.Interaction, prompt: str):
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "Este comando solo funciona en canales de texto regulares.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        conv_uuid = str(uuid_lib.uuid4())
        short = conv_uuid.split("-")[0]
        thread_name = f"papolo · {short}"

        try:
            thread = await interaction.channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "No tengo permiso para crear threads en este canal.", ephemeral=True
            )
            return

        db.create_conversation(
            uuid=conv_uuid,
            thread_id=thread.id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            created_by=interaction.user.id,
        )

        embed = discord.Embed(
            title="Conversacion iniciada",
            description=f"Todos los mensajes en este thread van a Papolo.\nUUID: `{conv_uuid}`",
            color=0x5865F2,
        )
        await thread.send(embed=embed)

        formatted = format_user_turn(interaction.user.display_name, prompt, None)
        db.save_message(
            conversation_uuid=conv_uuid,
            role="user",
            content=formatted,
            author_id=interaction.user.id,
            author_name=interaction.user.display_name,
        )

        agent = get_or_create_agent(conv_uuid)
        try:
            result = await _run_agent_turn(agent, formatted, channel=thread, conv_uuid=conv_uuid)
        except Exception as e:
            log.exception("Agent error en /papolo")
            await thread.send(f"ERROR: {e}")
            await interaction.followup.send(f"Thread creado: {thread.mention}")
            return

        persist_agent(conv_uuid, agent)
        db.save_message(conv_uuid, role="assistant", content=result)

        await send_long(thread, result)
        await interaction.followup.send(f"Thread creado: {thread.mention}")

    @bot.tree.command(name="papolo-reset", description="Limpia la memoria del agente en este thread")
    async def papolo_reset(interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Usalo dentro de un thread de Papolo.", ephemeral=True
            )
            return
        conv = db.get_conversation_by_thread(interaction.channel.id)
        if not conv:
            await interaction.response.send_message(
                "Este thread no es de Papolo.", ephemeral=True
            )
            return
        db.delete_agent_state(conv["uuid"])
        await interaction.response.send_message("Memoria del thread reseteada.")

    @bot.tree.command(
        name="papolo-stop",
        description="Cancela el prompt que Papolo esta procesando ahora en este thread",
    )
    async def papolo_stop(interaction: discord.Interaction):
        # Igual que el resto: solo dentro de un thread de Papolo; afuera tira error.
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Usalo dentro de un thread de Papolo.", ephemeral=True
            )
            return
        conv = db.get_conversation_by_thread(interaction.channel.id)
        if not conv:
            await interaction.response.send_message(
                "Este thread no es de Papolo.", ephemeral=True
            )
            return
        if active_turns.cancel(conv["uuid"]):
            await interaction.response.send_message(
                "Cancelando el prompt en curso… Papolo para en el proximo checkpoint "
                "(no corta una tool que ya esta ejecutandose)."
            )
        else:
            await interaction.response.send_message(
                "No hay ningun prompt corriendo en este thread ahora mismo.",
                ephemeral=True,
            )

    @bot.tree.command(name="papolo-uuid", description="Muestra el UUID de esta conversacion")
    async def papolo_uuid(interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Usalo dentro de un thread de Papolo.", ephemeral=True
            )
            return
        conv = db.get_conversation_by_thread(interaction.channel.id)
        if not conv:
            await interaction.response.send_message(
                "Este thread no es de Papolo.", ephemeral=True
            )
            return
        await interaction.response.send_message(f"`{conv['uuid']}`")

    @bot.tree.command(name="papolo-skills", description="Lista las skills disponibles")
    async def papolo_skills(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        skills = list_skills()
        if not skills:
            await interaction.followup.send("(sin skills instaladas)", ephemeral=True)
            return
        body = "\n".join(f"- **{s['name']}**: {s['description']}" for s in skills)
        await send_ephemeral_long(interaction, body)

    @bot.tree.command(name="papolo-subagents", description="Lista los subagentes disponibles")
    async def papolo_subs(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        subs = list_subagents()
        if not subs:
            await interaction.followup.send("(sin subagentes definidos)", ephemeral=True)
            return
        body = "\n".join(f"- **{s['name']}**: {s['description']}" for s in subs)
        await send_ephemeral_long(interaction, body)

    @bot.tree.command(
        name="papolo-model",
        description="Muestra o cambia el modelo de DeepSeek (orquestador o subagentes)",
    )
    @app_commands.describe(
        model="Modelo a usar (autocompleta con los disponibles). Vacio = muestra los actuales.",
        scope="A que capa aplica el cambio (default: orquestador).",
    )
    @app_commands.autocomplete(model=_model_autocomplete)
    async def papolo_model(
        interaction: discord.Interaction,
        model: str | None = None,
        scope: Literal["orquestador", "subagentes"] = "orquestador",
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        avail = await asyncio.to_thread(models.available_models)

        # Sin argumento: mostrar ambos modelos actuales + los disponibles.
        if not model:
            cur_main = models.current_model()
            cur_sub = models.current_subagent_model()
            listado = "\n".join(f"- `{m}`" for m in avail)
            await interaction.followup.send(
                f"**Orquestador (agente principal):** `{cur_main}`\n"
                f"**Subagentes:** `{cur_sub}`\n\n"
                f"**Disponibles:**\n{listado}\n\n"
                f"Cambiar: `/papolo-model model:<nombre>` (orquestador) · "
                f"`/papolo-model model:<nombre> scope:subagentes`.",
                ephemeral=True,
            )
            return

        model = model.strip()
        # Validar contra los disponibles (si pudimos listarlos). El fallback ya
        # incluye los modelos publicos, asi que uno valido no queda bloqueado.
        if avail and model not in avail:
            opts = ", ".join(f"`{m}`" for m in avail)
            await interaction.followup.send(
                f"`{model}` no esta entre los modelos disponibles ({opts}). "
                f"Elegi uno de la lista.",
                ephemeral=True,
            )
            return

        if scope == "subagentes":
            models.set_subagent_model(model)
            capa = "de los subagentes"
        else:
            models.set_model(model)
            capa = "del orquestador"
        await interaction.followup.send(
            f"Modelo {capa} actualizado a `{model}`. "
            f"Aplica desde el proximo turno en todos los threads.",
            ephemeral=True,
        )

    @bot.tree.command(
        name="papolo-download",
        description="Descarga el workspace del thread como zip",
    )
    async def papolo_download(interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Usalo dentro de un thread de Papolo.", ephemeral=True
            )
            return
        conv = db.get_conversation_by_thread(interaction.channel.id)
        if not conv:
            await interaction.response.send_message(
                "Este thread no es de Papolo.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        ws = workspace_path(conv["uuid"])
        if not ws.exists():
            await interaction.followup.send("El workspace todavia no existe.")
            return

        short = conv["uuid"].split("-")[0]
        out_path = Path(tempfile.gettempdir()) / f"papolo-{short}.zip"
        try:
            # git archive respeta .gitignore y es rapido. Fallback a zip si falla.
            r = subprocess.run(
                ["git", "archive", "HEAD", "--format=zip", "-o", str(out_path)],
                cwd=ws, capture_output=True, text=True, timeout=60,
            )
            if r.returncode != 0:
                import shutil
                stem = str(out_path.with_suffix(""))
                shutil.make_archive(stem, "zip", ws)
                out_path = Path(stem + ".zip")
        except Exception as e:
            await interaction.followup.send(f"Fallo armando el zip: {e}")
            return

        size_mb = out_path.stat().st_size / (1024 * 1024)
        limit_mb = 24.5  # Discord free server cap
        if size_mb > limit_mb:
            try:
                out_path.unlink()
            except OSError:
                pass
            dep = db.get_active_deployment(conv["uuid"])
            extra = f"\nGitHub: {dep['github_repo_url']}" if dep and dep.get("github_repo_url") else ""
            await interaction.followup.send(
                f"Workspace pesa {size_mb:.1f}MB, supera el limite de Discord "
                f"({limit_mb:.0f}MB). Borra node_modules/target/.venv del workspace "
                f"o usa GitHub.{extra}"
            )
            return

        try:
            await interaction.followup.send(
                content=f"Workspace ({size_mb:.1f}MB):",
                file=discord.File(str(out_path), filename=f"papolo-{short}.zip"),
            )
        finally:
            try:
                out_path.unlink()
            except OSError:
                pass

    @bot.tree.command(
        name="papolo-transcript",
        description="Exporta TODO (Discord + estado interno del agente + deployments) como .json",
    )
    async def papolo_transcript(interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Usalo dentro de un thread de Papolo.", ephemeral=True
            )
            return
        conv = db.get_conversation_by_thread(interaction.channel.id)
        if not conv:
            await interaction.response.send_message(
                "Este thread no es de Papolo.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        short = conv["uuid"].split("-")[0]
        events = _build_transcript_events(conv)
        payload = json.dumps(events, ensure_ascii=False, indent=2)
        out_path = Path(tempfile.gettempdir()) / f"papolo-{short}-transcript.json"
        out_path.write_text(payload, encoding="utf-8")
        gz_path: Path | None = None
        try:
            size_mb = out_path.stat().st_size / (1024 * 1024)
            send_path, send_name = out_path, out_path.name
            # Si el JSON completo supera el limite de Discord, lo comprimimos en
            # vez de truncar: la transcripcion tiene que quedar entera.
            if size_mb > 24.5:
                import gzip
                import shutil

                gz_path = out_path.with_name(out_path.name + ".gz")
                with open(out_path, "rb") as fi, gzip.open(gz_path, "wb") as fo:
                    shutil.copyfileobj(fi, fo)
                gz_mb = gz_path.stat().st_size / (1024 * 1024)
                if gz_mb > 24.5:
                    await interaction.followup.send(
                        f"Transcripcion demasiado grande incluso comprimida "
                        f"({gz_mb:.1f}MB, {len(events):,} eventos)."
                    )
                    return
                send_path, send_name, size_mb = gz_path, gz_path.name, gz_mb
            await interaction.followup.send(
                content=f"Transcripcion JSON ({len(events):,} eventos, {size_mb:.2f}MB):",
                file=discord.File(str(send_path), filename=send_name),
            )
        finally:
            for p in (out_path, gz_path):
                if p is None:
                    continue
                try:
                    p.unlink()
                except OSError:
                    pass

    @bot.tree.command(
        name="papolo-status",
        description="Muestra el estado del deployment del thread",
    )
    async def papolo_status(interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Usalo dentro de un thread de Papolo.", ephemeral=True
            )
            return
        conv = db.get_conversation_by_thread(interaction.channel.id)
        if not conv:
            await interaction.response.send_message(
                "Este thread no es de Papolo.", ephemeral=True
            )
            return
        dep = db.get_active_deployment(conv["uuid"])
        if not dep:
            await interaction.response.send_message(
                "No hay deployment activo en este thread.", ephemeral=True
            )
            return
        body = (
            f"**Status:** {dep['status']}\n"
            f"**Repo:** {dep.get('github_repo_url') or '-'}\n"
            f"**Preview:** {dep.get('preview_url') or '-'}\n"
            f"**App UUID:** `{dep.get('coolify_app_uuid') or '-'}`\n"
            f"**Ultimo error:** {dep.get('last_error') or '-'}"
        )
        await interaction.response.send_message(body)

    @bot.tree.command(
        name="papolo-destroy",
        description="Destruye el deployment activo (repo + app Coolify)",
    )
    async def papolo_destroy(interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Usalo dentro de un thread de Papolo.", ephemeral=True
            )
            return
        conv = db.get_conversation_by_thread(interaction.channel.id)
        if not conv:
            await interaction.response.send_message(
                "Este thread no es de Papolo.", ephemeral=True
            )
            return
        dep = db.get_active_deployment(conv["uuid"])
        if not dep:
            await interaction.response.send_message(
                "No hay deployment activo.", ephemeral=True
            )
            return

        # Le pedimos al agente que se encargue (asi pasa por el flujo de confirmacion).
        instruction = (
            "El usuario pidio destruir el deployment activo. "
        )
        if dep.get("coolify_app_uuid"):
            instruction += (
                f"Llama coolify_destroy_app(app_uuid='{dep['coolify_app_uuid']}', confirm_token='ASK'). "
            )
        if dep.get("github_repo_name"):
            instruction += (
                f"Llama github_delete_repo(repo_name='{dep['github_repo_name']}', confirm_token='ASK'). "
            )
        instruction += "Una vez confirmado por el usuario, reintenta con el codigo dado."

        await interaction.response.defer(thinking=True)
        agent = get_or_create_agent(conv["uuid"])
        try:
            result = await _run_agent_turn(agent, instruction, channel=interaction.channel, conv_uuid=conv["uuid"])
        except Exception as e:
            log.exception("destroy error")
            await interaction.followup.send(f"ERROR: {e}")
            return
        persist_agent(conv["uuid"], agent)
        await send_long(interaction.channel, result)
        await interaction.followup.send("Listo.")

    @bot.tree.command(
        name="papolo-purge",
        description="Borra TODO lo que Papolo creó (GitHub, Coolify, MongoDB). Solo lo suyo. Irreversible.",
    )
    async def papolo_purge(interaction: discord.Interaction):
        # Op destructiva global: la limitamos a admins del server.
        perms = getattr(interaction.user, "guild_permissions", None)
        if not (perms and perms.administrator):
            await interaction.response.send_message(
                "Solo un administrador del servidor puede usar esto.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        repo_names, app_uuids = db.all_papolo_resources()
        try:
            targets = await asyncio.to_thread(
                papolo_deploy.purge_targets, repo_names, app_uuids
            )
        except Exception as e:
            log.exception("purge_targets error")
            await interaction.followup.send(
                f"ERROR reuniendo recursos: {e}", ephemeral=True
            )
            return

        embed = _purge_preview_embed(targets)
        if papolo_deploy.purge_total(targets) == 0:
            embed.title = "🧹 Nada para borrar"
            embed.color = 0x57F287
            embed.description = (
                "No encontré recursos creados por Papolo en GitHub, Coolify ni MongoDB."
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        view = _PurgeConfirmView(interaction.user.id, targets)
        msg = await interaction.followup.send(
            embed=embed, view=view, ephemeral=True, wait=True
        )
        view.message = msg

    @bot.event
    async def on_message(message: discord.Message):
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.Thread):
            return

        conv = db.get_conversation_by_thread(message.channel.id)
        if not conv:
            return

        conv_uuid = conv["uuid"]
        reply_ctx = await fetch_reply_context(message)
        formatted = format_user_turn(
            message.author.display_name,
            message.content,
            reply_ctx,
        )

        # Adjuntos de texto/codigo enviados en el thread: inyectarlos como contexto.
        try:
            attach_block = await attachments.collect_text_attachments(message)
        except Exception:
            log.exception("Error procesando adjuntos del mensaje %s", message.id)
            attach_block = None
        if attach_block:
            formatted = f"{formatted}\n\n{attach_block}"

        db.save_message(
            conversation_uuid=conv_uuid,
            role="user",
            content=formatted,
            discord_msg_id=message.id,
            author_id=message.author.id,
            author_name=message.author.display_name,
            reply_to_msg_id=(message.reference.message_id if message.reference else None),
        )

        agent = get_or_create_agent(conv_uuid)
        async with message.channel.typing():
            try:
                result = await _run_agent_turn(agent, formatted, channel=message.channel, conv_uuid=conv_uuid)
            except Exception as e:
                log.exception("Agent error en thread")
                await message.channel.send(f"ERROR: {e}")
                return

        persist_agent(conv_uuid, agent)
        db.save_message(conv_uuid, role="assistant", content=result)

        await send_long(message.channel, result)
