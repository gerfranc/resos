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
from datetime import datetime, timedelta

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

        # --- Interceptar la respuesta (captura dual: auto + buscar) ---
        respuestas_auto = []
        respuestas_buscar = []
        click_realizado = False

        def capturar_respuesta(response):
            if "/resoluciones/data" in response.url:
                try:
                    data = response.json()
                    fuente = "buscar" if click_realizado else "auto"
                    items = data.get("data", [])
                    if fuente == "auto":
                        respuestas_auto.append(data)
                    else:
                        respuestas_buscar.append(data)
                    logger.info(f"Respuesta capturada ({fuente}): {len(items)} resultados")
                except Exception as e:
                    logger.error(f"Error al parsear respuesta: {e}")

        page.on("response", capturar_respuesta)

        # --- Paso 1: Navegar a la pagina (puede generar respuesta automatica) ---
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

        click_realizado = True
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

        # --- Paso 4: Mergear fuentes deduplicando por docId ---
        items_auto = []
        for data in respuestas_auto:
            items_auto.extend(data.get("data", []))

        items_buscar = []
        for data in respuestas_buscar:
            items_buscar.extend(data.get("data", []))

        if not items_auto:
            logger.warning("Fuente auto: 0 resultados (no llego respuesta automatica)")

        # Mergear deduplicando por docId (prioridad a fuente auto si hay duplicados)
        vistos_ids = set()
        for item in items_auto:
            doc_id = str(item.get("docId", ""))
            if doc_id:
                vistos_ids.add(doc_id)
                resoluciones_data.append(item)

        for item in items_buscar:
            doc_id = str(item.get("docId", ""))
            if doc_id and doc_id not in vistos_ids:
                vistos_ids.add(doc_id)
                resoluciones_data.append(item)

        logger.info(f"Fuente auto: {len(items_auto)} resultados | "
                     f"Fuente buscar: {len(items_buscar)} resultados | "
                     f"Merge: {len(resoluciones_data)} unicos")

        if not resoluciones_data:
            logger.warning("No se obtuvieron resoluciones de ninguna fuente")

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


def rechequear_pendientes(vistas):
    """
    Re-verifica resoluciones pendientes descargando sus PDFs.
    Actualiza el estado en vistas (modifica in-place).
    """
    ahora = datetime.now()
    pendientes = {
        rid: datos for rid, datos in vistas.items()
        if datos.get("estado") == "pendiente"
    }

    if not pendientes:
        return

    logger.info(f"Re-verificando {len(pendientes)} resoluciones pendientes...")
    nuevas_relevantes = 0
    expiradas = 0

    for rid, datos in pendientes.items():
        # Verificar si expiro
        fecha_expira = datetime.fromisoformat(datos["expira"])
        if ahora > fecha_expira:
            datos["estado"] = "no_relevante"
            expiradas += 1
            logger.info(f"  Expirada: N°{datos.get('numero', rid)}")
            continue

        # Re-verificar PDF
        url_pdf = datos.get("url_pdf", "")
        if not url_pdf:
            datos["intentos"] = datos.get("intentos", 0) + 1
            continue

        if verificar_pdf(url_pdf):
            datos["estado"] = "relevante"
            nuevas_relevantes += 1
            # Notificar con prefijo de re-verificacion
            r = {
                "numero": datos.get("numero", "S/N"),
                "fecha": datos.get("fecha", "Sin fecha"),
                "detalle": datos.get("detalle", ""),
                "expediente": datos.get("expediente", ""),
                "url_pdf": url_pdf,
            }
            mensaje = "🔄 <b>[Re-verificación]</b>\n\n" + formatear_mensaje(r)
            enviar_telegram(mensaje)
            time.sleep(1)
        else:
            datos["intentos"] = datos.get("intentos", 0) + 1

    logger.info(f"Pendientes re-verificadas: {len(pendientes)} | "
                f"Nuevas relevantes: {nuevas_relevantes} | Expiradas: {expiradas}")


def chequear_resoluciones():
    logger.info("=" * 60)
    logger.info("Iniciando chequeo de resoluciones CSJN")
    logger.info(f"Filtro: {FILTRO_PALABRAS} | Desde: {FECHA_DESDE}")

    try:
        # Paso 0: Re-verificar pendientes antes del scraping
        vistas = cargar_vistas()
        rechequear_pendientes(vistas)
        guardar_vistas(vistas)

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

        # Marcar TODAS las no vistas como procesadas con estado
        ahora = datetime.now()
        for r in no_vistas:
            if r in relevantes:
                vistas[r["id"]] = {
                    "numero": r["numero"],
                    "fecha": r["fecha"],
                    "detalle": r["detalle"],
                    "url_pdf": r["url_pdf"],
                    "relevante": True,
                    "estado": "relevante",
                    "procesado": ahora.isoformat(),
                }
            elif r in no_relevantes:
                # No matcheo ni en detalle ni en PDF → pendiente para re-verificar
                vistas[r["id"]] = {
                    "numero": r["numero"],
                    "fecha": r["fecha"],
                    "detalle": r["detalle"],
                    "url_pdf": r["url_pdf"],
                    "relevante": False,
                    "estado": "pendiente",
                    "intentos": 1,
                    "primera_vez": ahora.isoformat(),
                    "expira": (ahora + timedelta(days=5)).isoformat(),
                    "procesado": ahora.isoformat(),
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


def ejecutar_test():
    """
    Modo --test: ejecuta sin scraping real usando fixtures hardcodeados.
    Telegram no envia mensajes reales, solo loguea.
    """
    import tempfile
    import shutil

    logger.info("=" * 60)
    logger.info("MODO TEST - Sin scraping real, Telegram simulado")

    # Monkey-patch enviar_telegram para no enviar mensajes reales
    global enviar_telegram
    _enviar_original = enviar_telegram
    def enviar_telegram_test(mensaje):
        logger.info(f"TEST - Telegram simulado: {mensaje}")
        return True
    enviar_telegram = enviar_telegram_test

    # Backup del archivo de vistas actual
    backup_vistas = None
    if os.path.exists(SEEN_FILE):
        backup_vistas = SEEN_FILE + ".test_backup"
        shutil.copy2(SEEN_FILE, backup_vistas)

    try:
        # Preparar seen_resoluciones.json con datos de test
        ahora = datetime.now()
        vistas_test = {
            # Caso 3: resolucion ya vista (debe ser ignorada)
            "99999": {
                "numero": "99999/2026",
                "fecha": "01/04/2026",
                "detalle": "Resolucion ya procesada anteriormente",
                "url_pdf": "",
                "relevante": False,
                "estado": "no_relevante",
                "procesado": ahora.isoformat(),
            },
            # Caso 4: pendiente expirada (hace 10 dias)
            "88888": {
                "numero": "88888/2026",
                "fecha": "20/03/2026",
                "detalle": "Resolucion pendiente que expiro",
                "url_pdf": "",
                "relevante": False,
                "estado": "pendiente",
                "intentos": 3,
                "primera_vez": (ahora - timedelta(days=15)).isoformat(),
                "expira": (ahora - timedelta(days=10)).isoformat(),
                "procesado": (ahora - timedelta(days=15)).isoformat(),
            },
        }
        guardar_vistas(vistas_test)

        # Fixtures que simulan la respuesta de la CSJN
        fixtures = [
            # Caso 1: match en detalle por palabra clave
            {
                "docId": "11111",
                "nroDoc": "11111/2026",
                "fechaCompleta": "05/04/2026",
                "detalle": "Designar personal - Delitos Complejos y Crimen Organizado",
                "descripcionTipo": "Resolucion",
                "nroExpe": "EXP-2026-001",
                "pathTemas": "",
            },
            # Caso 2: sin match en detalle, sin PDF → debe quedar pendiente
            {
                "docId": "22222",
                "nroDoc": "22222/2026",
                "fechaCompleta": "05/04/2026",
                "detalle": "Resolucion administrativa general sin palabras clave",
                "descripcionTipo": "Resolucion",
                "nroExpe": "EXP-2026-002",
                "pathTemas": "",
            },
            # Caso 3: docId ya existente en vistas → debe ser ignorada
            {
                "docId": "99999",
                "nroDoc": "99999/2026",
                "fechaCompleta": "01/04/2026",
                "detalle": "Resolucion ya procesada anteriormente",
                "descripcionTipo": "Resolucion",
                "nroExpe": "",
                "pathTemas": "",
            },
        ]

        # Paso 0: Re-verificar pendientes (caso 4: la expirada)
        vistas = cargar_vistas()
        rechequear_pendientes(vistas)
        guardar_vistas(vistas)

        # Procesar fixtures como si vinieran del scraping
        logger.info(f"Total resoluciones obtenidas (fixtures): {len(fixtures)}")
        resoluciones = [parsear_resolucion(item) for item in fixtures]

        # Caso 2: forzar url_pdf vacia (simula resolucion sin PDF disponible)
        for r in resoluciones:
            if r["id"] == "22222":
                r["url_pdf"] = ""

        no_vistas = [r for r in resoluciones if r["id"] and r["id"] not in vistas]
        logger.info(f"{len(no_vistas)} resoluciones no procesadas, filtrando por contenido...")

        relevantes = []
        no_relevantes = []

        for r in no_vistas:
            if contiene_palabras_clave(r["detalle"]):
                logger.info(f"  MATCH en detalle: N°{r['numero']} - {r['detalle'][:60]}")
                relevantes.append(r)
            elif r["url_pdf"]:
                if verificar_pdf(r["url_pdf"]):
                    logger.info(f"  MATCH en PDF: N°{r['numero']} - {r['detalle'][:60]}")
                    relevantes.append(r)
                else:
                    logger.info(f"  No relevante: N°{r['numero']} - {r['detalle'][:60]}")
                    no_relevantes.append(r)
            else:
                logger.info(f"  Sin PDF, no relevante: N°{r['numero']} - {r['detalle'][:60]}")
                no_relevantes.append(r)

        ahora_save = datetime.now()
        for r in no_vistas:
            if r in relevantes:
                vistas[r["id"]] = {
                    "numero": r["numero"],
                    "fecha": r["fecha"],
                    "detalle": r["detalle"],
                    "url_pdf": r["url_pdf"],
                    "relevante": True,
                    "estado": "relevante",
                    "procesado": ahora_save.isoformat(),
                }
            elif r in no_relevantes:
                vistas[r["id"]] = {
                    "numero": r["numero"],
                    "fecha": r["fecha"],
                    "detalle": r["detalle"],
                    "url_pdf": r["url_pdf"],
                    "relevante": False,
                    "estado": "pendiente",
                    "intentos": 1,
                    "primera_vez": ahora_save.isoformat(),
                    "expira": (ahora_save + timedelta(days=5)).isoformat(),
                    "procesado": ahora_save.isoformat(),
                }

        for r in relevantes:
            enviar_telegram(formatear_mensaje(r))

        guardar_vistas(vistas)

        # --- Resumen de resultados ---
        vistas_final = cargar_vistas()

        logger.info("")
        logger.info("=" * 60)
        logger.info("RESUMEN TEST - Resultados esperados vs obtenidos")
        logger.info("=" * 60)

        # Caso 1
        caso1 = vistas_final.get("11111", {})
        estado1 = caso1.get("estado", "NO ENCONTRADO")
        ok1 = estado1 == "relevante"
        logger.info(f"Caso 1 (match en detalle):    esperado=relevante    | obtenido={estado1}    | {'OK' if ok1 else 'FALLO'}")

        # Caso 2
        caso2 = vistas_final.get("22222", {})
        estado2 = caso2.get("estado", "NO ENCONTRADO")
        ok2 = estado2 == "pendiente"
        logger.info(f"Caso 2 (sin match, sin PDF):  esperado=pendiente    | obtenido={estado2}    | {'OK' if ok2 else 'FALLO'}")

        # Caso 3
        caso3 = vistas_final.get("99999", {})
        estado3 = caso3.get("estado", "NO ENCONTRADO")
        ok3 = estado3 == "no_relevante"  # no se re-proceso, mantiene estado original
        logger.info(f"Caso 3 (ya vista, ignorada):  esperado=no_relevante | obtenido={estado3}    | {'OK' if ok3 else 'FALLO'}")

        # Caso 4
        caso4 = vistas_final.get("88888", {})
        estado4 = caso4.get("estado", "NO ENCONTRADO")
        ok4 = estado4 == "no_relevante"
        logger.info(f"Caso 4 (pendiente expirada):  esperado=no_relevante | obtenido={estado4}    | {'OK' if ok4 else 'FALLO'}")

        total_ok = sum([ok1, ok2, ok3, ok4])
        logger.info(f"\nResultado: {total_ok}/4 casos correctos")
        logger.info("=" * 60)

    finally:
        # Restaurar archivo de vistas original
        if backup_vistas and os.path.exists(backup_vistas):
            shutil.move(backup_vistas, SEEN_FILE)
            logger.info("Archivo seen_resoluciones.json restaurado desde backup")
        elif backup_vistas is None:
            # No habia archivo original, borrar el de test
            if os.path.exists(SEEN_FILE):
                os.remove(SEEN_FILE)

        # Restaurar enviar_telegram original
        enviar_telegram = _enviar_original


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("MONITOR CSJN - DAJUDECO (Playwright)")
    logger.info(f"Pagina: {URL_PAGINA}")
    logger.info(f"Filtro: {FILTRO_PALABRAS}")

    if "--test" in sys.argv:
        logger.info("Modo: TEST (sin scraping real)")
        ejecutar_test()
    elif "--once" in sys.argv:
        logger.info("Modo: ejecucion unica")
        chequear_resoluciones()
    else:
        logger.info(f"Modo: loop cada {CHECK_INTERVAL_SECONDS // 3600} horas")
        while True:
            chequear_resoluciones()
            logger.info(f"Proximo chequeo en {CHECK_INTERVAL_SECONDS // 3600} horas...")
            time.sleep(CHECK_INTERVAL_SECONDS)
