import asyncio
import logging
import os
import uuid as uuid_lib

import discord
from discord import app_commands
from discord.ext import commands

from papolo import Agent
from papolo.skills import list_skills
from papolo.subagents import list_subagents

from . import db
from .conversations import get_or_create_agent, persist_agent
from .discord_helpers import fetch_reply_context, format_user_turn, send_long

log = logging.getLogger("papolo-bot")


async def _run_agent_turn(agent: Agent, prompt: str) -> str:
    return await asyncio.to_thread(agent.send, prompt)


def setup(bot: commands.Bot) -> None:
    @bot.event
    async def on_ready():
        log.info("Logueado como %s (id=%s)", bot.user, bot.user.id if bot.user else "?")
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
            result = await _run_agent_turn(agent, formatted)
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
        skills = list_skills()
        if not skills:
            await interaction.response.send_message("(sin skills instaladas)", ephemeral=True)
            return
        body = "\n".join(f"- **{s['name']}**: {s['description']}" for s in skills)
        await interaction.response.send_message(body, ephemeral=True)

    @bot.tree.command(name="papolo-subagents", description="Lista los subagentes disponibles")
    async def papolo_subs(interaction: discord.Interaction):
        subs = list_subagents()
        if not subs:
            await interaction.response.send_message("(sin subagentes definidos)", ephemeral=True)
            return
        body = "\n".join(f"- **{s['name']}**: {s['description']}" for s in subs)
        await interaction.response.send_message(body, ephemeral=True)

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
                result = await _run_agent_turn(agent, formatted)
            except Exception as e:
                log.exception("Agent error en thread")
                await message.channel.send(f"ERROR: {e}")
                return

        persist_agent(conv_uuid, agent)
        db.save_message(conv_uuid, role="assistant", content=result)

        await send_long(message.channel, result)
