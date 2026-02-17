#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
MONITOR DE RESOLUCIONES CSJN - DAJUDECO (version Playwright)
=============================================================================
Usa Chromium headless para buscar resoluciones en la CSJN.
Intercepta el request POST al endpoint de datos y fuerza los parametros
de busqueda con comillas para garantizar busqueda exacta.

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
from datetime import datetime

import requests

# =============================================================================
# CONFIGURACION
# =============================================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "TU_TOKEN_AQUI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "TU_CHAT_ID_AQUI")
CHECK_INTERVAL_SECONDS = 7200  # 2 horas
SEARCH_TERM = '"Dirección de Asistencia Judicial"'
SEEN_FILE = "seen_resoluciones.json"
LOG_FILE = "monitor_csjn.log"
FECHA_DESDE = "10/02/2026"

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

def buscar_con_playwright():
    """
    Estrategia:
    1. Abrir la pagina con Playwright (sesion real)
    2. Llenar el formulario y hacer click en Buscar
    3. Interceptar el request POST saliente al endpoint /resoluciones/data
       y MODIFICAR el payload para forzar las comillas en q y qa
    4. Capturar la respuesta JSON
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
            Intercepta el POST al endpoint de datos y modifica el payload
            para asegurar que q y qa tengan las comillas.
            """
            if "/resoluciones/data" in request.url and request.method == "POST":
                try:
                    body = json.loads(request.post_data)
                    # Forzar comillas en los campos de busqueda
                    if "formBusqueda" in body:
                        body["formBusqueda"]["q"] = SEARCH_TERM
                        body["formBusqueda"]["qa"] = SEARCH_TERM
                        body["formBusqueda"]["fechaDesde"] = FECHA_DESDE
                        body["formBusqueda"]["fechaDesde_a"] = FECHA_DESDE
                    logger.info(f"Request interceptado - q: {body.get('formBusqueda',{}).get('q','?')}")
                    route.continue_(post_data=json.dumps(body))
                except Exception as e:
                    logger.error(f"Error al interceptar request: {e}")
                    route.continue_()
            else:
                route.continue_()

        page.route("**/resoluciones/data", interceptar_request)

        # --- Interceptar la respuesta para capturar los datos ---
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
        page.goto(URL_PAGINA, wait_until="networkidle", timeout=60000)
        logger.info("Pagina cargada")
        page.wait_for_timeout(3000)

        # --- Paso 2: Llenar el campo de busqueda ---
        # Buscar el campo "cualquier dato disponible"
        campo_busqueda = None
        selectores = [
            'input[name="formBusqueda.q"]',
            'input#q',
            'input[placeholder*="cualquier"]',
            'input[placeholder*="dato"]',
        ]

        for selector in selectores:
            try:
                elem = page.query_selector(selector)
                if elem and elem.is_visible():
                    campo_busqueda = elem
                    logger.info(f"Campo encontrado: {selector}")
                    break
            except:
                continue

        if not campo_busqueda:
            logger.info("Buscando campo de texto en formulario...")
            inputs = page.query_selector_all('input[type="text"]')
            visibles = []
            for inp in inputs:
                try:
                    if inp.is_visible():
                        visibles.append(inp)
                except:
                    pass

            if len(visibles) >= 3:
                campo_busqueda = visibles[2]
                logger.info(f"Usando tercer input visible de {len(visibles)}")
            elif visibles:
                campo_busqueda = visibles[-1]
                logger.info(f"Usando ultimo input visible de {len(visibles)}")

        if not campo_busqueda:
            page.screenshot(path="debug_formulario.png")
            raise Exception("No se encontro el campo de busqueda")

        # Llenar con el termino (las comillas se fuerzan en el interceptor)
        campo_busqueda.click()
        campo_busqueda.fill(SEARCH_TERM)
        logger.info(f"Campo completado con: {SEARCH_TERM}")

        # --- Paso 3: Click en Buscar ---
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
        logger.info("Click en Buscar")

        # --- Paso 4: Esperar respuesta ---
        logger.info("Esperando resultados...")
        page.wait_for_timeout(8000)

        try:
            page.wait_for_selector("table tbody tr", timeout=15000)
            logger.info("Tabla con resultados detectada")
        except:
            logger.info("No se detecto tabla (puede no haber resultados)")

        # --- Paso 5: Extraer datos ---
        if respuesta_capturada:
            data = respuesta_capturada[-1]
            resoluciones_data = data.get("data", [])
            total = data.get("recordsTotal", 0)
            filtradas = data.get("recordsFiltered", 0)
            logger.info(f"JSON: {len(resoluciones_data)} resoluciones "
                         f"(total: {total}, filtradas: {filtradas})")
        else:
            # Fallback: extraer del DOM
            logger.info("Sin JSON interceptado, extrayendo del DOM...")
            filas = page.query_selector_all("table tbody tr")
            for fila in filas:
                celdas = fila.query_selector_all("td")
                if len(celdas) >= 3:
                    link = fila.query_selector("a[href*='descargar']")
                    href = link.get_attribute("href") if link else ""
                    doc_id = href.split("ID=")[-1] if "ID=" in href else ""
                    resoluciones_data.append({
                        "docId": doc_id,
                        "nroDoc": celdas[0].inner_text().strip(),
                        "fechaCompleta": celdas[1].inner_text().strip() if len(celdas) > 1 else "",
                        "detalle": celdas[2].inner_text().strip() if len(celdas) > 2 else "",
                    })
            logger.info(f"DOM: {len(resoluciones_data)} resoluciones")

        # Log primeros resultados
        for i, item in enumerate(resoluciones_data[:3]):
            logger.info(f"  [{i+1}] N\u00B0{item.get('nroDoc','?')} - "
                         f"{item.get('fechaCompleta','?')} - "
                         f"{item.get('detalle','?')[:80]}")

        browser.close()
        logger.info("Navegador cerrado")

    return resoluciones_data


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
    logger.info(f"Pagina: {URL_PAGINA}")
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
