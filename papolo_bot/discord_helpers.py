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
