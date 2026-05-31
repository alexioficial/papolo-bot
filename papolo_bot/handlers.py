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

from . import confirmations, conversations, db
from .conversations import bind_bot, get_or_create_agent, persist_agent, workspace_path
from .discord_helpers import fetch_reply_context, format_user_turn, send_ephemeral_long, send_long
from .live_status import LiveStatus

log = logging.getLogger("papolo-bot")


async def _run_agent_turn(agent: Agent, prompt: str,
                           channel: discord.abc.Messageable | None = None) -> str:
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
            result = await _run_agent_turn(agent, formatted, channel=thread)
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
            result = await _run_agent_turn(agent, instruction, channel=interaction.channel)
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
                result = await _run_agent_turn(agent, formatted, channel=message.channel)
            except Exception as e:
                log.exception("Agent error en thread")
                await message.channel.send(f"ERROR: {e}")
                return

        persist_agent(conv_uuid, agent)
        db.save_message(conv_uuid, role="assistant", content=result)

        await send_long(message.channel, result)
