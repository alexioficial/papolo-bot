# papolo-bot

Bot de Discord que expone el agente Papolo. Cada conversacion vive en un **thread** propio, identificado por un UUID y persistido en SQLite.

## Comportamiento

- `/papolo <prompt>` en un canal → crea un thread nuevo, postea el UUID y responde al prompt inicial.
- Dentro del thread, **todo mensaje** se envia al agente. No hace falta mencionarlo.
- Si haces *reply* a un mensaje del thread, ese mensaje se inyecta como contexto explicito.
- Multi-usuario: varios humanos pueden conversar en el mismo thread. El agente ve `@username: ...` y diferencia.
- El estado del agente se persiste en SQLite, asi sobrevive a restarts del contenedor.

## Comandos slash

| Comando | Donde | Que hace |
|---|---|---|
| `/papolo <prompt>` | canal | crea thread y arranca conversacion |
| `/papolo-reset` | thread | limpia memoria del agente (no borra historial) |
| `/papolo-stop` | thread | cancela el prompt que Papolo esta procesando ahora (corta en el proximo checkpoint) |
| `/papolo-uuid` | thread | postea el UUID de la conversacion |
| `/papolo-skills` | cualquiera | lista skills disponibles |
| `/papolo-subagents` | cualquiera | lista subagentes disponibles |
| `/papolo-model [model] [scope]` | cualquiera | muestra o cambia el modelo de DeepSeek (autocompleta con los disponibles). `scope:orquestador` (default) = agente principal; `scope:subagentes` = subagentes (default `deepseek-chat`). Se guarda en SQLite y aplica a todos los threads |

## Setup local

```powershell
cd papolo-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ../papolo
pip install -r requirements.txt
copy .env.example .env
# editar .env con DISCORD_BOT_TOKEN, DEEPSEEK_API_KEY
python bot.py
```

## Configurar el bot en Discord

1. https://discord.com/developers/applications → New Application
2. Bot → Reset Token → copia a `.env` como `DISCORD_BOT_TOKEN`
3. Activar **Privileged Gateway Intents**:
   - Message Content Intent
   - Server Members Intent
4. OAuth2 → URL Generator:
   - scopes: `bot`, `applications.commands`
   - bot permissions: Send Messages, Create Public Threads, Send Messages in Threads, Read Message History, Embed Links, Use Slash Commands
5. Pega la URL en el browser e invita al server

## Probar el Docker build localmente

Antes de hacer push a Coolify, asegurate de que `vendor/papolo/` existe (ahi mira el Dockerfile). Para dev local se vendoriza con copia simple desde el repo hermano:

```powershell
# desde papolo-bot/
Remove-Item -Recurse -Force vendor\papolo -ErrorAction SilentlyContinue
robocopy ..\papolo vendor\papolo /E /XD .git .venv __pycache__ /XF .env | Out-Null
docker compose up --build
```

`vendor/` esta en `.gitignore` para que esa copia local no se commitee. Para el deploy real usas submodule (ver abajo).

## Deploy en VPS con Coolify

### 1. Vendorizar `papolo` como submodule

Antes de pushear, **saca `vendor/` del `.gitignore`** (o cambia `vendor/` por algo mas especifico como `vendor/papolo/*.local`) y agrega papolo como submodule:

```bash
cd papolo-bot
git submodule add <url-del-repo-papolo> vendor/papolo
git submodule update --init --recursive
git add .gitmodules vendor/papolo
git commit -m "vendor papolo as submodule"
git push
```

Si no queres usar submodule, alternativas:
- Reemplazar `COPY vendor/papolo /app/vendor/papolo` en el Dockerfile por `RUN pip install git+https://github.com/<vos>/papolo.git@main` (privado: necesita deploy key o PAT).

### 2. Configurar el proyecto en Coolify

1. New Resource → Docker Compose
2. Source: tu repo papolo-bot. Marcar **Submodules: enabled**.
3. Compose file: `docker-compose.yml`
4. Environment variables (desde el dashboard de Coolify, no commiteado):
   - `DISCORD_BOT_TOKEN`
   - `DEEPSEEK_API_KEY`
   - `DEEPSEEK_MODEL` (opcional, default `deepseek-chat`)
   - dejar `PAPOLO_DB_PATH` como esta en el Dockerfile (`/data/papolo.sqlite`)
5. Deploy

El volumen `papolo_data` persiste el SQLite entre redeploys.

## Estructura

```
papolo-bot/
├── bot.py                     # arranque
├── papolo_bot/
│   ├── db.py                  # SQLite (schema + queries + settings)
│   ├── conversations.py       # bridge agente <-> sqlite
│   ├── models.py              # modelos DeepSeek disponibles + modelo configurado
│   ├── discord_helpers.py     # chunks, formateo, fetch reply
│   └── handlers.py            # slash commands + on_message
├── vendor/papolo/             # submodule del motor (en deploy)
├── data/                      # local: SQLite vive aca (gitignored)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
