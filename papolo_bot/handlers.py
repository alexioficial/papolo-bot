import asyncio
import logging
import os
import subprocess
import tempfile
import uuid as uuid_lib
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from papolo import Agent
from papolo.skills import list_skills
from papolo.subagents import list_subagents

from . import active_turns, confirmations, conversations, db, models
from .conversations import bind_bot, get_or_create_agent, persist_agent, workspace_path
from .discord_helpers import fetch_reply_context, format_user_turn, send_ephemeral_long, send_long
from .live_status import LiveStatus

log = logging.getLogger("papolo-bot")


def _build_transcript_md(conv: dict) -> str:
    """Construye el .md con la conversacion Discord + el estado interno del agente."""
    import json as _json
    short = conv["uuid"].split("-")[0]
    out: list[str] = []
    out.append(f"# Transcripcion · papolo · {short}")
    out.append("")
    out.append(f"- UUID: `{conv['uuid']}`")
    out.append(f"- Thread ID: `{conv['thread_id']}`")
    out.append(f"- Creado: {conv['created_at']}")
    out.append("")
    out.append("---")
    out.append("")
    out.append("## Conversacion (lado Discord)")
    out.append("")

    msgs = db.get_messages(conv["uuid"])
    for m in msgs:
        author = m.get("author_name") or m["role"]
        role = m["role"]
        ts = m["created_at"]
        icon = "🧑" if role == "user" else ("🤖" if role == "assistant" else "•")
        out.append(f"### {icon} {author} ({role}) — {ts}")
        out.append("")
        content = (m.get("content") or "").strip()
        if not content:
            out.append("_(vacio)_")
        else:
            out.append(content)
        out.append("")

    out.append("---")
    out.append("")
    out.append("## Estado interno del agente (mensajes openai-style con tool calls/results)")
    out.append("")

    state = db.load_agent_state(conv["uuid"]) or []
    # Los tool-result messages se guardan sin 'name'; mapeamos tool_call_id -> name
    # desde los tool_calls de los mensajes assistant para poder rotularlos.
    id_to_name: dict[str, str] = {}
    for msg in state:
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if tc.get("id"):
                id_to_name[tc["id"]] = fn.get("name", "?")
    for i, msg in enumerate(state):
        role = msg.get("role", "?")
        out.append(f"### [{i}] {role}")
        out.append("")
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            preview = content if len(content) <= 6000 else (content[:6000] + f"\n…[truncated {len(content)-6000} chars]")
            out.append("```")
            out.append(preview)
            out.append("```")
            out.append("")

        tool_calls = msg.get("tool_calls") or []
        for tc in tool_calls:
            fn = (tc.get("function") or {})
            tname = fn.get("name", "?")
            targs = fn.get("arguments", "")
            try:
                parsed = _json.loads(targs) if isinstance(targs, str) else targs
                targs_pretty = _json.dumps(parsed, indent=2, ensure_ascii=False)
            except Exception:
                targs_pretty = str(targs)
            if len(targs_pretty) > 4000:
                targs_pretty = targs_pretty[:4000] + f"\n…[truncated {len(targs_pretty)-4000} chars]"
            out.append(f"**tool_call:** `{tname}` (id: `{tc.get('id','?')}`)")
            out.append("```json")
            out.append(targs_pretty)
            out.append("```")
            out.append("")

        if role == "tool":
            tcid = msg.get("tool_call_id", "?")
            tname = id_to_name.get(tcid, msg.get("name", "?"))
            out.append(f"**tool result** for `{tname}` (call_id: `{tcid}`)")
            out.append("")

    return "\n".join(out)


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
        description="Muestra o cambia el modelo de DeepSeek que usa Papolo",
    )
    @app_commands.describe(
        model="Modelo a usar (autocompleta con los disponibles). Vacio = muestra el actual."
    )
    @app_commands.autocomplete(model=_model_autocomplete)
    async def papolo_model(interaction: discord.Interaction, model: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        avail = await asyncio.to_thread(models.available_models)

        # Sin argumento: mostrar el modelo actual + los disponibles.
        if not model:
            cur = models.current_model()
            listado = "\n".join(
                f"- `{m}`" + ("  ← actual" if m == cur else "") for m in avail
            )
            if cur not in avail:
                listado = f"- `{cur}`  ← actual (no listado por la API)\n" + listado
            await interaction.followup.send(
                f"**Modelo actual:** `{cur}`\n\n**Disponibles:**\n{listado}\n\n"
                f"Para cambiarlo: `/papolo-model model:<nombre>`.",
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

        models.set_model(model)
        await interaction.followup.send(
            f"Modelo de Papolo actualizado a `{model}`. "
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
        description="Exporta la conversacion + estado interno del agente como .md",
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
        md = _build_transcript_md(conv)
        out_path = Path(tempfile.gettempdir()) / f"papolo-{short}-transcript.md"
        out_path.write_text(md, encoding="utf-8")
        try:
            size_mb = out_path.stat().st_size / (1024 * 1024)
            if size_mb > 24.5:
                await interaction.followup.send(
                    f"Transcripcion supera el limite de Discord ({size_mb:.1f}MB)."
                )
                return
            await interaction.followup.send(
                content=f"Transcripcion ({size_mb:.2f}MB, {len(md):,} chars):",
                file=discord.File(str(out_path), filename=f"papolo-{short}-transcript.md"),
            )
        finally:
            try:
                out_path.unlink()
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
