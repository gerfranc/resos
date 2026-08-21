# CLAUDE.md

## Proyecto

Monitor de resoluciones de la Corte Suprema de Justicia de la Nación (CSJN) Argentina. Script CLI en Python que scrapea resoluciones, filtra por palabras clave y notifica vía Telegram.

## Stack

- **Python 3.11**
- **Playwright** (headless Chromium) para scraping
- **requests** para API de Telegram
- **pdfplumber** para extracción de texto de PDFs
- **GitHub Actions** para ejecución programada (cron cada 2hs, lunes a viernes)

## Estructura

```
monitor_csjn_dajudeco.py   # Script principal
seen_resoluciones.json      # Estado de resoluciones procesadas (se commitea)
telegram_state.json         # Offset de getUpdates para el bot (se commitea)
telegram_subscribers.json   # Chats privados suscriptos vía /start (se commitea)
monitor_csjn.log            # Log de ejecución (rotado, se commitea capado a ~500KB)
.github/workflows/          # CI/CD con GitHub Actions
```

## Comandos

```bash
# Instalar dependencias
pip install playwright requests pdfplumber
playwright install chromium
playwright install-deps chromium

# Ejecutar una vez (modo CI/cron)
python monitor_csjn_dajudeco.py --once

# Ejecutar en loop continuo (cada 2 horas)
python monitor_csjn_dajudeco.py

# Sondear comandos del bot de Telegram (una pasada) — usado por el segundo workflow
python monitor_csjn_dajudeco.py --telegram-poll

# Registrar menú de comandos y descripción del bot en Telegram (una vez).
# Sin token local: correrlo desde Actions → "Setup bot Telegram" (usa el secret).
python monitor_csjn_dajudeco.py --telegram-setup
```

## Bot de Telegram

El bot solo **distribuye las alertas del monitor** (no tiene búsqueda; `/buscar` fue
discontinuado y responde un aviso). Atiende dos ámbitos; otros grupos donde lo agreguen
se ignoran.

**Grupo configurado** (`TELEGRAM_CHAT_ID`): recibe las alertas automáticas del monitor.
`/start`, `/stop` y `/ayuda` en el grupo responden la info sin suscribir (el grupo ya
recibe todo). Los comandos que empiezan con `/` se entregan al bot aunque el *privacy
mode* esté activo; el bot debe estar agregado al grupo.

**Chats privados (bot compartible)**: cualquiera que reciba el link `t.me/<usuario_del_bot>`
puede abrirlo; el botón **Iniciar/Start** que muestra Telegram manda `/start`. Comandos:

- `/start` → suscribe el chat a las alertas automáticas del monitor y responde la bienvenida
  (idempotente).
- `/stop` (o `/baja`) → da de baja la suscripción.
- `/ayuda` (o `/help`) → info del bot; también se responde ante texto privado que no es comando.

Los suscriptos se persisten en `telegram_subscribers.json` (se commitea). **Solo el flujo de
polling escribe ese archivo**; el monitor lo lee para mandar cada alerta al grupo + suscriptos
(`notificar_alerta`). Así cada workflow commitea archivos distintos y no se pisan.

Detalles operativos:

- La recepción es por **polling**: el workflow `buscar_telegram.yml` corre cada ~5 min,
  lee `getUpdates`, procesa los comandos y responde. Latencia esperada 5–20 min
  (los cron de GitHub Actions se atrasan). `workflow_dispatch` permite forzar una corrida.
  La descripción del bot (registrada con `--telegram-setup`) se ve al instante arriba del
  botón Start y avisa de la demora.
- El `offset` de Telegram se persiste en `telegram_state.json` (se commitea) para no
  reprocesar mensajes.
- Los mensajes usan `parse_mode=HTML`: todo texto de usuario o de la CSJN interpolado en
  mensajes se escapa con `html.escape()`.

## Variables de entorno

| Variable             | Descripción                  |
|----------------------|------------------------------|
| `TELEGRAM_BOT_TOKEN` | Token del bot de Telegram   |
| `TELEGRAM_CHAT_ID`   | ID del chat/grupo de Telegram |

Si no están configuradas, los mensajes se imprimen en consola.

## Configuración interna

Las constantes están al inicio de `monitor_csjn_dajudeco.py`:
- `FECHA_DESDE`: fecha de inicio de búsqueda
- `FILTRO_PALABRAS`: lista de palabras clave para filtrar resoluciones
- `CHECK_INTERVAL_SECONDS`: intervalo entre chequeos (default 7200s)

## CI/CD

GitHub Actions:

`monitor_csjn.yml` (monitoreo):
- Cron: `0 */2 * * 1-5` (cada 2 horas, lunes a viernes)
- Disparo manual con `workflow_dispatch`
- Después de ejecutar, commitea y pushea `seen_resoluciones.json` y `monitor_csjn.log`

`buscar_telegram.yml` (comandos del bot):
- Cron: `*/5 * * * *` (cada 5 min) + `workflow_dispatch`
- `concurrency` con `cancel-in-progress: false` para no cortar corridas a medias
- Ejecuta `--telegram-poll` y commitea `telegram_state.json` y `telegram_subscribers.json`

`telegram_setup.yml` (setup del bot):
- Solo `workflow_dispatch`: ejecuta `--telegram-setup` con el secret `TELEGRAM_BOT_TOKEN`
  (registra menú de comandos y descripción del bot). Se corre una vez; no commitea nada.

Ambos workflows hacen `git pull --rebase` antes del push para no pisarse entre sí.

## Testing

```bash
# Prueba rápida sin scraping (verificar lógica de pendientes y filtros)
python monitor_csjn_dajudeco.py --test

# Prueba local completa con scraping real (renombrar JSON antes)
mv seen_resoluciones.json seen_resoluciones.backup.json
python monitor_csjn_dajudeco.py --once
mv seen_resoluciones.backup.json seen_resoluciones.json

# Prueba en GitHub Actions desde el branch (sin mergear)
# Ir a Actions → Monitor CSJN → Run workflow → seleccionar branch
```

## Notas para desarrollo

- No hay test suite ni linter configurado
- El estado se persiste en `seen_resoluciones.json` (JSON plano, se commitea al repo)
- El script intercepta requests HTTP del navegador para modificar parámetros de búsqueda
- Filtrado en dos pasadas: primero por campo de detalle, luego por contenido del PDF
- Los comentarios en el código están en español
