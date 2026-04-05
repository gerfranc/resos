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
monitor_csjn_dajudeco.py   # Script principal (~410 líneas)
seen_resoluciones.json      # Estado de resoluciones procesadas (se commitea)
monitor_csjn.log            # Log de ejecución (se commitea)
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
```

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

GitHub Actions (`.github/workflows/monitor_csjn.yml`):
- Cron: `0 */2 * * 1-5` (cada 2 horas, lunes a viernes)
- Disparo manual con `workflow_dispatch`
- Después de ejecutar, commitea y pushea `seen_resoluciones.json` y `monitor_csjn.log`

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
