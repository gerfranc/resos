#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
MONITOR DE RESOLUCIONES CSJN - DAJUDECO (version Playwright)
=============================================================================
Usa un navegador headless real (Chromium) para buscar resoluciones en la
pagina de la CSJN. Intercepta el request de busqueda y fuerza los
parametros correctos con comillas para busqueda exacta.

REQUISITOS:
  pip install playwright requests
  playwright install chromium

USO:
  python monitor_csjn_dajudeco.py --once    # Una sola vez (GitHub Actions/cron)
  python monitor_csjn_dajudeco.py            # Loop continuo cada 4 horas
=============================================================================
"""

import os
import json
import time
import logging
import sys
from datetime import datetime

import requests

# =============================================================================
# CONFIGURACION
# =============================================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "TU_TOKEN_AQUI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "TU_CHAT_ID_AQUI")
CHECK_INTERVAL_SECONDS = 14400  # 4 horas
SEARCH_TERM = '"Dirección de Asistencia Judicial"'
SEEN_FILE = "seen_resoluciones.json"
LOG_FILE = "monitor_csjn.log"
FECHA_DESDE = "10/02/2026"

URL_PAGINA = "https://www.csjn.gov.ar/decisiones/resoluciones"
URL_ENDPOINT = "https://www.csjn.gov.ar/resoluciones/data"
URL_BASE_PDF = "https://www.csjn.gov.ar/documentos/descargar?ID="

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# =============================================================================
# FUNCIONES
# =============================================================================

def buscar_con_playwright():
    """
    Estrategia: abrir la pagina de la CSJN para obtener una sesion valida,
    luego interceptar el request de DataTables y forzar los parametros
    de busqueda correctos (con comillas) directamente en el payload JSON.
    Esto garantiza que la busqueda sea exacta.
    """
    from playwright.sync_api import sync_playwright

    resoluciones_data = []

    with sync_playwright() as p:
        logger.info("Iniciando navegador Chromium headless...")
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36",
            locale="es-AR",
        )
        page = context.new_page()

        # --- Paso 1: Navegar a la pagina para establecer sesion ---
        logger.info(f"Navegando a {URL_PAGINA}")
        page.goto(URL_PAGINA, wait_until="networkidle", timeout=60000)
        logger.info("Pagina cargada, sesion establecida")
        page.wait_for_timeout(2000)

        # --- Paso 2: Hacer el request directo al endpoint usando la sesion del navegador ---
        # Usamos page.evaluate() para hacer un fetch desde el contexto del navegador,
        # aprovechando las cookies de sesion ya establecidas.
        logger.info(f"Ejecutando busqueda: {SEARCH_TERM}")

        payload = {
            "draw": 1,
            "start": 0,
            "length": 100,
            "formBusqueda": {
                "q": SEARCH_TERM,
                "qa": SEARCH_TERM,
                "nro": "",
                "anio": "",
                "tema": "",
                "subtema": "",
                "fechaDesde": FECHA_DESDE,
                "fechaDesde_a": FECHA_DESDE,
                "fechaHasta": "",
                "fechaHasta_a": ""
            }
        }

        # Ejecutar fetch desde el navegador (con cookies de sesion)
        result = page.evaluate("""
            async (payload) => {
                try {
                    const resp = await fetch('/resoluciones/data', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json;charset=UTF-8',
                            'X-Requested-With': 'XMLHttpRequest',
                        },
                        body: JSON.stringify(payload)
                    });
                    if (!resp.ok) {
                        return { error: `HTTP ${resp.status}: ${resp.statusText}`, url: resp.url };
                    }
                    const text = await resp.text();
                    // Verificar si es una redireccion a accesoDenegado
                    if (text.includes('accesoDenegado') || text.includes('<html')) {
                        return { error: 'Redireccion a accesoDenegado', body: text.substring(0, 200) };
                    }
                    return JSON.parse(text);
                } catch (e) {
                    return { error: e.toString() };
                }
            }
        """, payload)

        browser.close()
        logger.info("Navegador cerrado")

        # --- Paso 3: Procesar resultado ---
        if isinstance(result, dict) and "error" in result:
            raise Exception(f"Error en fetch: {result['error']}")

        resoluciones_data = result.get("data", [])
        total = result.get("recordsTotal", 0)
        filtradas = result.get("recordsFiltered", 0)

        logger.info(f"Resultados: {len(resoluciones_data)} resoluciones "
                     f"(total: {total}, filtradas: {filtradas})")

        # Log de los primeros resultados para verificar
        for i, item in enumerate(resoluciones_data[:3]):
            logger.info(f"  [{i+1}] N°{item.get('nroDoc','?')} - "
                         f"{item.get('fechaCompleta','?')} - "
                         f"{item.get('detalle','?')[:80]}")

    return resoluciones_data


def parsear_resolucion(item):
    """Extrae datos relevantes de cada resultado."""
    doc_id = str(item.get("docId", ""))
    return {
        "id": doc_id,
        "numero": item.get("nroDoc", "S/N"),
        "fecha": item.get("fechaCompleta", item.get("fecha", "Sin fecha")),
        "detalle": item.get("detalle", "Sin detalle"),
        "tipo": item.get("descripcionTipo", ""),
        "expediente": item.get("nroExpe", ""),
        "url_pdf": f"{URL_BASE_PDF}{doc_id}" if doc_id else "",
        "temas": item.get("pathTemas", ""),
    }


def cargar_vistas():
    if not os.path.exists(SEEN_FILE):
        return {}
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Error al leer {SEEN_FILE}: {e}")
        return {}


def guardar_vistas(vistas):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(vistas, f, ensure_ascii=False, indent=2)
    logger.info(f"Registro: {len(vistas)} resoluciones guardadas")


def enviar_telegram(mensaje):
    if TELEGRAM_BOT_TOKEN == "TU_TOKEN_AQUI" or TELEGRAM_CHAT_ID == "TU_CHAT_ID_AQUI":
        logger.warning("Telegram no configurado")
        logger.info(f"Mensaje:\n{mensaje}")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": mensaje,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }, timeout=15)
        resp.raise_for_status()
        if resp.json().get("ok"):
            logger.info("Telegram: mensaje enviado")
            return True
        logger.error(f"Telegram error: {resp.json()}")
        return False
    except Exception as e:
        logger.error(f"Error Telegram: {e}")
        return False


def formatear_mensaje(r):
    msg = (
        f"\U0001F514 <b>Nueva Resolucion CSJN - DAJUDECO</b>\n\n"
        f"\U0001F4C4 Resolucion N\u00B0 {r['numero']} - {r['fecha']}\n"
        f"\U0001F4DD {r['detalle']}\n"
    )
    if r.get("expediente"):
        msg += f"\U0001F4C1 Exp: {r['expediente']}\n"
    if r.get("url_pdf"):
        msg += f"\n\U0001F517 <a href=\"{r['url_pdf']}\">Descargar PDF</a>\n"
    msg += f"\n\U0001F50D Busqueda: {SEARCH_TERM}"
    return msg


def chequear_resoluciones():
    logger.info("=" * 60)
    logger.info("Iniciando chequeo de resoluciones CSJN")
    logger.info(f"Termino: {SEARCH_TERM} | Desde: {FECHA_DESDE}")

    try:
        resultados = buscar_con_playwright()

        if not resultados:
            logger.info("No se encontraron resoluciones")
            return

        resoluciones = [parsear_resolucion(item) for item in resultados]
        vistas = cargar_vistas()
        nuevas = [r for r in resoluciones if r["id"] and r["id"] not in vistas]

        if not nuevas:
            logger.info(f"Sin novedades. {len(resoluciones)} encontradas, todas ya vistas.")
            return

        logger.info(f"{len(nuevas)} resoluciones NUEVAS")

        for r in nuevas:
            enviar_telegram(formatear_mensaje(r))
            vistas[r["id"]] = {
                "numero": r["numero"],
                "fecha": r["fecha"],
                "detalle": r["detalle"],
                "url_pdf": r["url_pdf"],
                "notificado": datetime.now().isoformat(),
            }
            time.sleep(1)

        guardar_vistas(vistas)
        logger.info(f"Completado: {len(nuevas)} notificaciones enviadas")

    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        enviar_telegram(f"\u26A0\uFE0F <b>Error en Monitor CSJN</b>\n\n{str(e)[:500]}")


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("MONITOR CSJN - DAJUDECO (Playwright)")
    logger.info(f"Endpoint: {URL_ENDPOINT}")
    logger.info(f"Busqueda: {SEARCH_TERM}")

    if "--once" in sys.argv:
        logger.info("Modo: ejecucion unica")
        chequear_resoluciones()
    else:
        logger.info(f"Modo: loop cada {CHECK_INTERVAL_SECONDS // 3600} horas")
        while True:
            chequear_resoluciones()
            logger.info(f"Proximo chequeo en {CHECK_INTERVAL_SECONDS // 3600} horas...")
            time.sleep(CHECK_INTERVAL_SECONDS)
