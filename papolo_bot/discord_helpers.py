import discord

MAX_DISCORD_LEN = 1900


def chunks(text: str, size: int = MAX_DISCORD_LEN):
    if not text:
        return
    for i in range(0, len(text), size):
        yield text[i : i + size]


async def send_long(channel, text: str) -> None:
    if not text.strip():
        await channel.send("(respuesta vacia)")
        return
    for c in chunks(text):
        await channel.send(c)


def chunk_lines(lines: list[str], size: int = MAX_DISCORD_LEN) -> list[str]:
    """Une lineas en bloques <= size sin partir lineas a la mitad."""
    out: list[str] = []
    cur = ""
    for line in lines:
        if not cur:
            cur = line
        elif len(cur) + 1 + len(line) <= size:
            cur += "\n" + line
        else:
            out.append(cur)
            cur = line
    if cur:
        out.append(cur)
    return out


async def send_ephemeral_long(interaction, text: str) -> None:
    """Manda texto largo como ephemeral. Asume que interaction ya fue defer'd."""
    parts = chunk_lines(text.split("\n")) or ["(vacio)"]
    for p in parts:
        await interaction.followup.send(p, ephemeral=True)


async def fetch_reply_context(message: discord.Message) -> str | None:
    ref = message.reference
    if not ref or not ref.message_id:
        return None
    try:
        if ref.cached_message:
            referenced = ref.cached_message
        else:
            referenced = await message.channel.fetch_message(ref.message_id)
    except (discord.NotFound, discord.HTTPException):
        return None
    author = referenced.author.display_name if referenced.author else "desconocido"
    content = referenced.content or "(mensaje sin texto)"
    return f"[En respuesta a @{author}: {content}]"


def format_user_turn(display_name: str, content: str, reply_context: str | None) -> str:
    parts = []
    if reply_context:
        parts.append(reply_context)
    parts.append(f"@{display_name}: {content}")
    return "\n".join(parts)
