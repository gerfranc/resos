#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
MONITOR DE RESOLUCIONES CSJN - DAJUDECO (version Playwright)
=============================================================================
Estrategia: trae TODAS las resoluciones recientes (sin filtro de texto en
el buscador de la CSJN, porque el indice tarda en actualizarse) y luego
filtra localmente cuales mencionan "Asistencia Judicial" en el detalle
o en el contenido del PDF.

REQUISITOS:
  pip install playwright requests
  playwright install chromium

USO:
  python monitor_csjn_dajudeco.py --once    # Una sola vez (GitHub Actions/cron)
  python monitor_csjn_dajudeco.py            # Loop continuo
=============================================================================
"""

import os
import json
import time
import logging
import sys
import re
from datetime import datetime

import requests

# =============================================================================
# CONFIGURACION
# =============================================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "TU_TOKEN_AQUI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "TU_CHAT_ID_AQUI")
CHECK_INTERVAL_SECONDS = 7200  # 2 horas
SEEN_FILE = "seen_resoluciones.json"
LOG_FILE = "monitor_csjn.log"
FECHA_DESDE = "10/02/2026"

# Palabras clave para filtrar (se buscan en detalle + contenido PDF)
# Se busca que TODAS las palabras aparezcan (case insensitive)
FILTRO_PALABRAS = ["Delitos Complejos y Crimen Organizado"]

URL_PAGINA = "https://www.csjn.gov.ar/decisiones/resoluciones"
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

def obtener_todas_las_resoluciones():
    """
    Abre la pagina de resoluciones SIN filtro de texto.
    Solo aplica filtro de fecha. Captura la respuesta JSON
    con todas las resoluciones recientes.
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

        # --- Interceptar y modificar el request saliente ---
        def interceptar_request(route, request):
            """
            Intercepta el POST al endpoint de datos.
            Limpia los campos de busqueda de texto (q, qa) para traer TODO
            y solo aplica el filtro de fecha.
            Pide hasta 200 resultados para no perder ninguno.
            """
            if "/resoluciones/data" in request.url and request.method == "POST":
                try:
                    body = json.loads(request.post_data)
                    if "formBusqueda" in body:
                        # Sin filtro de texto - traer todas
                        body["formBusqueda"]["q"] = ""
                        body["formBusqueda"]["qa"] = ""
                        # Filtro de fecha
                        body["formBusqueda"]["fechaDesde"] = FECHA_DESDE
                        body["formBusqueda"]["fechaDesde_a"] = FECHA_DESDE
                    # Pedir mas resultados
                    body["length"] = 200
                    logger.info(f"Request interceptado - sin filtro texto, fechaDesde: {FECHA_DESDE}")
                    route.continue_(post_data=json.dumps(body))
                except Exception as e:
                    logger.error(f"Error al interceptar request: {e}")
                    route.continue_()
            else:
                route.continue_()

        page.route("**/resoluciones/data", interceptar_request)

        # --- Interceptar la respuesta ---
        respuesta_capturada = []

        def capturar_respuesta(response):
            if "/resoluciones/data" in response.url:
                try:
                    data = response.json()
                    respuesta_capturada.append(data)
                    logger.info(f"Respuesta capturada: {len(data.get('data', []))} resultados")
                except Exception as e:
                    logger.error(f"Error al parsear respuesta: {e}")

        page.on("response", capturar_respuesta)

        # --- Paso 1: Navegar a la pagina ---
        logger.info(f"Navegando a {URL_PAGINA}")
        page.goto(URL_PAGINA, wait_until="domcontentloaded", timeout=120000)
        logger.info("Pagina cargada")
        page.wait_for_timeout(3000)

        # --- Paso 2: Click en Buscar (sin llenar campo de texto) ---
        boton_buscar = None
        for selector in ['button:has-text("Buscar")', 'input[value="Buscar"]', 'a:has-text("Buscar")']:
            try:
                elems = page.query_selector_all(selector)
                for elem in elems:
                    if elem.is_visible():
                        boton_buscar = elem
                        logger.info(f"Boton encontrado: {selector}")
                        break
                if boton_buscar:
                    break
            except:
                continue

        if not boton_buscar:
            raise Exception("No se encontro el boton Buscar")

        boton_buscar.click()
        logger.info("Click en Buscar (sin filtro de texto)")

        # --- Paso 3: Esperar respuesta ---
        logger.info("Esperando resultados...")
        page.wait_for_timeout(8000)

        try:
            page.wait_for_selector("table tbody tr", timeout=15000)
            logger.info("Tabla con resultados detectada")
        except:
            logger.info("No se detecto tabla")

        # --- Paso 4: Extraer datos ---
        if respuesta_capturada:
            data = respuesta_capturada[-1]
            resoluciones_data = data.get("data", [])
            total = data.get("recordsTotal", 0)
            filtradas = data.get("recordsFiltered", 0)
            logger.info(f"JSON: {len(resoluciones_data)} resoluciones "
                         f"(total servidor: {total}, filtradas: {filtradas})")
        else:
            logger.warning("No se intercepto respuesta JSON")

        browser.close()
        logger.info("Navegador cerrado")

    return resoluciones_data


def contiene_palabras_clave(texto):
    """Verifica si el texto contiene TODAS las palabras clave del filtro."""
    texto_lower = texto.lower()
    return all(palabra.lower() in texto_lower for palabra in FILTRO_PALABRAS)


def verificar_pdf(url_pdf):
    """
    Descarga el PDF y busca las palabras clave en su contenido.
    Retorna True si las contiene, False si no.
    """
    try:
        logger.info(f"Descargando PDF: {url_pdf}")
        resp = requests.get(url_pdf, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        resp.raise_for_status()

        # Extraer texto del PDF de forma simple
        # Los PDFs de la CSJN suelen tener texto extraible
        contenido = resp.content

        # Intentar con pdfplumber si esta disponible
        try:
            import pdfplumber
            import io
            texto = ""
            with pdfplumber.open(io.BytesIO(contenido)) as pdf:
                for pagina in pdf.pages:
                    texto += pagina.extract_text() or ""
            if texto:
                logger.info(f"PDF: {len(texto)} caracteres extraidos con pdfplumber")
                return contiene_palabras_clave(texto)
        except ImportError:
            pass

        # Fallback: buscar texto directamente en el binario del PDF
        # Los PDFs suelen tener el texto embebido como strings
        texto_raw = contenido.decode("latin-1", errors="ignore")
        if contiene_palabras_clave(texto_raw):
            logger.info("PDF: palabras clave encontradas en contenido raw")
            return True

        logger.info("PDF: palabras clave NO encontradas")
        return False

    except Exception as e:
        logger.warning(f"Error al verificar PDF {url_pdf}: {e}")
        # En caso de error, no filtrar (incluir por las dudas)
        return True


def parsear_resolucion(item):
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
    msg += f"\n\U0001F50D Filtro: Asistencia Judicial"
    return msg


def chequear_resoluciones():
    logger.info("=" * 60)
    logger.info("Iniciando chequeo de resoluciones CSJN")
    logger.info(f"Filtro: {FILTRO_PALABRAS} | Desde: {FECHA_DESDE}")

    try:
        # Paso 1: Traer TODAS las resoluciones recientes
        resultados = obtener_todas_las_resoluciones()

        if not resultados:
            logger.info("No se encontraron resoluciones")
           # enviar_telegram("\u2705 Chequeo completado. No se encontraron resoluciones.")
            return

        logger.info(f"Total resoluciones obtenidas: {len(resultados)}")

        # Paso 2: Parsear
        resoluciones = [parsear_resolucion(item) for item in resultados]

        # Paso 3: Filtrar solo las nuevas (no vistas antes)
        vistas = cargar_vistas()
        no_vistas = [r for r in resoluciones if r["id"] and r["id"] not in vistas]

        if not no_vistas:
            logger.info(f"Sin novedades. {len(resoluciones)} resoluciones, todas ya procesadas.")
            #enviar_telegram("\u2705 Chequeo completado. No hay resoluciones nuevas.")
            return

        logger.info(f"{len(no_vistas)} resoluciones no procesadas, filtrando por contenido...")

        # Paso 4: Filtrar por palabras clave (primero en detalle, luego en PDF)
        relevantes = []
        no_relevantes = []

        for r in no_vistas:
            # Primero chequear en el campo detalle
            if contiene_palabras_clave(r["detalle"]):
                logger.info(f"  MATCH en detalle: N\u00B0{r['numero']} - {r['detalle'][:60]}")
                relevantes.append(r)
            elif r["url_pdf"]:
                # Si no esta en el detalle, buscar dentro del PDF
                if verificar_pdf(r["url_pdf"]):
                    logger.info(f"  MATCH en PDF: N\u00B0{r['numero']} - {r['detalle'][:60]}")
                    relevantes.append(r)
                else:
                    logger.info(f"  No relevante: N\u00B0{r['numero']} - {r['detalle'][:60]}")
                    no_relevantes.append(r)
            else:
                no_relevantes.append(r)

        # Marcar TODAS las no vistas como procesadas (relevantes y no relevantes)
        for r in no_vistas:
            vistas[r["id"]] = {
                "numero": r["numero"],
                "fecha": r["fecha"],
                "detalle": r["detalle"],
                "url_pdf": r["url_pdf"],
                "relevante": r in relevantes,
                "procesado": datetime.now().isoformat(),
            }

        # Paso 5: Notificar solo las relevantes
        if relevantes:
            logger.info(f"{len(relevantes)} resoluciones RELEVANTES encontradas")
            for r in relevantes:
                enviar_telegram(formatear_mensaje(r))
                time.sleep(1)
        else:
            logger.info(f"Ninguna de las {len(no_vistas)} nuevas es relevante")
            enviar_telegram(
                f"\u2705 Chequeo completado. {len(no_vistas)} resoluciones nuevas "
                f"procesadas, ninguna menciona Asistencia Judicial."
            )

        # Paso 6: Guardar registro
        guardar_vistas(vistas)
        logger.info(f"Completado: {len(relevantes)} relevantes de {len(no_vistas)} nuevas")

    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        enviar_telegram(f"\u26A0\uFE0F <b>Error en Monitor CSJN</b>\n\n{str(e)[:500]}")


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("MONITOR CSJN - DAJUDECO (Playwright)")
    logger.info(f"Pagina: {URL_PAGINA}")
    logger.info(f"Filtro: {FILTRO_PALABRAS}")

    if "--once" in sys.argv:
        logger.info("Modo: ejecucion unica")
        chequear_resoluciones()
    else:
        logger.info(f"Modo: loop cada {CHECK_INTERVAL_SECONDS // 3600} horas")
        while True:
            chequear_resoluciones()
            logger.info(f"Proximo chequeo en {CHECK_INTERVAL_SECONDS // 3600} horas...")
            time.sleep(CHECK_INTERVAL_SECONDS)
