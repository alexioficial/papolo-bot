"""Lectura de archivos adjuntos enviados por el usuario en el thread.

La idea: dejar que el usuario tire un archivo (mayormente codigo, .txt o .md) en el
thread y que su contenido se inyecte como contexto/entrada del turno del agente.

Solo aceptamos **texto**: rechazamos binarios, audio, video, imagenes, comprimidos,
ejecutables y ofimatica. La validacion es en dos capas:
  1) blocklist de extensiones obviamente no-texto (rechazo rapido y con mensaje claro),
  2) sniff del contenido descargado (NUL byte o UTF-8 invalido => binario).

Con presupuesto de tamanio por archivo y total por mensaje para no reventar el
context del modelo.
"""

import logging

log = logging.getLogger("papolo-bot")

_MAX_FILE_BYTES = 1_000_000    # 1 MB por archivo
_MAX_TOTAL_BYTES = 400_000     # ~400 KB de texto inyectado por turno (todos los adjuntos)
_MAX_FILES = 10                # cuantos adjuntos como mucho procesamos por mensaje

# Extensiones que rechazamos de una: no son texto/codigo.
_BLOCKED_EXTS = {
    # imagenes
    "png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff", "tif", "ico",
    "heic", "heif", "psd", "ai", "eps", "raw", "cr2", "nef",
    # audio
    "mp3", "wav", "flac", "ogg", "oga", "m4a", "aac", "wma", "opus",
    "aiff", "aif", "mid", "midi", "amr",
    # video
    "mp4", "mov", "avi", "mkv", "webm", "flv", "wmv", "m4v", "mpeg",
    "mpg", "3gp", "ts", "vob",
    # comprimidos / paquetes
    "zip", "tar", "gz", "tgz", "bz2", "xz", "zst", "rar", "7z", "jar",
    "war", "apk", "aab", "deb", "rpm", "iso", "dmg", "pkg", "cab",
    # ejecutables / binarios de build
    "exe", "dll", "so", "dylib", "bin", "obj", "o", "a", "lib", "class",
    "pyc", "pyo", "wasm", "msi", "elf", "out",
    # ofimatica / documentos binarios
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "odt", "ods", "odp", "rtf",
    # fuentes
    "ttf", "otf", "woff", "woff2", "eot",
    # bases de datos / dumps binarios
    "db", "sqlite", "sqlite3", "mdb", "dat", "pak",
}


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _looks_binary(data: bytes) -> bool:
    """True si los bytes no parecen texto UTF-8 (NUL byte o decode invalido)."""
    if not data:
        return False
    if b"\x00" in data:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


async def collect_text_attachments(message) -> str | None:
    """Descarga los adjuntos de texto/codigo de un mensaje de Discord y devuelve un
    bloque de texto para inyectar en el turno del agente. Devuelve None si el mensaje
    no trae adjuntos procesables (ni validos ni rechazados que valga la pena avisar)."""
    atts = getattr(message, "attachments", None)
    if not atts:
        return None

    blocks: list[str] = []
    notes: list[str] = []
    total = 0

    for att in atts[:_MAX_FILES]:
        name = getattr(att, "filename", None) or "archivo"
        ext = _ext(name)
        size = getattr(att, "size", 0) or 0

        if ext in _BLOCKED_EXTS:
            notes.append(f"`{name}` — omitido (formato `{ext}` no es texto/codigo).")
            continue
        if size and size > _MAX_FILE_BYTES:
            notes.append(
                f"`{name}` — omitido (pesa {size/1024:.0f} KB, maximo "
                f"{_MAX_FILE_BYTES//1024} KB)."
            )
            continue
        if total >= _MAX_TOTAL_BYTES:
            notes.append(f"`{name}` — omitido (se alcanzo el limite total de adjuntos por mensaje).")
            continue

        try:
            data = await att.read()
        except Exception as e:  # descarga fallida
            log.warning("No se pudo leer el adjunto %s: %s", name, e)
            notes.append(f"`{name}` — no se pudo descargar ({e}).")
            continue

        if _looks_binary(data):
            notes.append(f"`{name}` — omitido (parece binario, no texto plano).")
            continue

        text = data.decode("utf-8", errors="replace")
        remaining = _MAX_TOTAL_BYTES - total
        encoded = text.encode("utf-8")
        if len(encoded) > remaining:
            text = encoded[:remaining].decode("utf-8", errors="ignore")
            text += "\n…[truncado: el archivo supera el presupuesto de adjuntos del turno]"
        total += len(text.encode("utf-8"))

        blocks.append(
            f"--- archivo adjunto: {name} ({size or len(data)} bytes) ---\n"
            f"{text}\n"
            f"--- fin {name} ---"
        )

    if not blocks and not notes:
        return None

    out: list[str] = []
    if blocks:
        out.append("[Archivos adjuntos por el usuario en este mensaje — usalos como contexto/entrada:]")
        out.extend(blocks)
    if notes:
        out.append("[Adjuntos omitidos:]")
        out.extend(f"- {n}" for n in notes)
    return "\n".join(out)
