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
  python monitor_csjn_dajudeco.py --once             # Una sola vez (GitHub Actions/cron)
  python monitor_csjn_dajudeco.py                    # Loop continuo
  python monitor_csjn_dajudeco.py --telegram-poll    # Atiende comandos del bot (polling)
  python monitor_csjn_dajudeco.py --telegram-setup   # Registra comandos/descripcion del bot (una vez)
  python monitor_csjn_dajudeco.py --test             # Prueba sin scraping real
=============================================================================
"""

import os
import json
import time
import logging
import sys
import re
import html
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

import requests

# =============================================================================
# CONFIGURACION
# =============================================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "TU_TOKEN_AQUI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "TU_CHAT_ID_AQUI")
CHECK_INTERVAL_SECONDS = 7200  # 2 horas
SEEN_FILE = "seen_resoluciones.json"
LOG_FILE = "monitor_csjn.log"
LOG_MAX_BYTES = 512_000  # ~500 KB: evita que el log crezca sin limite
LOG_BACKUP_COUNT = 1     # se conserva un rotado (monitor_csjn.log.1, gitignoreado)
FECHA_DESDE = "10/02/2026"

# Palabras clave para filtrar (se buscan en detalle + contenido PDF)
# Se busca que TODAS las palabras aparezcan (case insensitive)
FILTRO_PALABRAS = ["Delitos Complejos y Crimen Organizado"]

URL_PAGINA = "https://www.csjn.gov.ar/decisiones/resoluciones"
URL_BASE_PDF = "https://www.csjn.gov.ar/documentos/descargar?ID="

# Bot de Telegram (modo --telegram-poll)
TELEGRAM_STATE_FILE = "telegram_state.json"  # persiste el offset de getUpdates
TELEGRAM_SUBSCRIBERS_FILE = "telegram_subscribers.json"  # chats suscriptos via /start (se commitea)
TELEGRAM_MAX_LEN = 3800          # margen bajo el limite real de 4096 de Telegram
MAX_RESULTADOS_RESPUESTA = 20    # tope de resultados listados por respuesta /buscar
BUSCAR_MAX_RESULTADOS = 200      # length pedido al servidor en /buscar
MAX_BUSQUEDAS_POR_CORRIDA = 5    # tope de /buscar atendidos por pasada de polling (anti-abuso)

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        RotatingFileHandler(
            LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        ),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# =============================================================================
# FUNCIONES
# =============================================================================

def obtener_todas_las_resoluciones(texto="", aplicar_fecha=True, max_resultados=200):
    """
    Abre la pagina de resoluciones y captura la respuesta JSON.

    Parametros:
      texto          -> texto a buscar en el campo 'q' de la CSJN.
                        "" (default) = traer TODO (usado por el monitoreo).
      aplicar_fecha  -> True (default): filtra desde FECHA_DESDE (monitoreo).
                        False: sin filtro de fecha (todo el historial, usado
                        por el comando /buscar).
      max_resultados -> tope de resultados a pedir (length del DataTables).

    El comportamiento por defecto (texto="", aplicar_fecha=True) reproduce
    exactamente el monitoreo original.
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
            Intercepta el POST al endpoint de datos y ajusta el body segun los
            parametros de la busqueda:
              - q      = texto (vacio => trae todo)
              - fecha  = FECHA_DESDE solo si aplicar_fecha; si no, vacio (historial)
              - length = max_resultados
            """
            if "/resoluciones/data" in request.url and request.method == "POST":
                try:
                    body = json.loads(request.post_data)
                    if "formBusqueda" in body:
                        # Filtro de texto (q). qa siempre vacio.
                        body["formBusqueda"]["q"] = texto
                        body["formBusqueda"]["qa"] = ""
                        # Filtro de fecha: solo si se pide (monitoreo). Para
                        # /buscar se vacia explicitamente = todo el historial.
                        if aplicar_fecha:
                            body["formBusqueda"]["fechaDesde"] = FECHA_DESDE
                            body["formBusqueda"]["fechaDesde_a"] = FECHA_DESDE
                        else:
                            body["formBusqueda"]["fechaDesde"] = ""
                            body["formBusqueda"]["fechaDesde_a"] = ""
                    # Pedir mas resultados
                    body["length"] = max_resultados
                    logger.info(
                        f"Request interceptado - q='{texto}', "
                        f"fecha={'FECHA_DESDE' if aplicar_fecha else 'sin filtro'}, "
                        f"length={max_resultados}"
                    )
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
                    total = data.get("recordsFiltered", data.get("recordsTotal", "?"))
                    logger.info(
                        f"Respuesta capturada ({fuente}): {len(items)} resultados "
                        f"(total segun servidor: {total})"
                    )
                except Exception as e:
                    logger.error(f"Error al parsear respuesta: {e}")

        page.on("response", capturar_respuesta)

        # --- Paso 1: Navegar a la pagina (puede generar respuesta automatica) ---
        logger.info(f"Navegando a {URL_PAGINA}")
        page.goto(URL_PAGINA, wait_until="domcontentloaded", timeout=120000)
        logger.info("Pagina cargada")
        # Esperar a que la red quede inactiva (captura la respuesta automatica
        # si la hay) en vez de un timeout fijo. Si nunca queda idle, seguimos.
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            logger.info("networkidle no alcanzado tras carga (se continua)")

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

        # --- Paso 2b: Si hay texto, escribirlo en el input de busqueda ---
        # Algunos front DataTables reconstruyen formBusqueda.q desde el DOM al
        # hacer submit y pisarian lo inyectado en el intercept. El intercept
        # queda igualmente como red de seguridad.
        if texto:
            input_encontrado = False
            for selector in ['input[name="q"]', 'input#q', 'input[name*="q"]',
                             'input[type="search"]', 'input[type="text"]']:
                try:
                    campo = page.query_selector(selector)
                    if campo and campo.is_visible():
                        campo.fill(texto)
                        logger.info(f"Texto '{texto}' escrito en input: {selector}")
                        input_encontrado = True
                        break
                except Exception:
                    continue
            if not input_encontrado:
                logger.warning(
                    "No se encontro el input de busqueda; se confia solo en el "
                    "intercept del request para el filtro de texto."
                )

        # --- Paso 3: Click en Buscar y esperar la respuesta de datos ---
        # En vez de un timeout fijo, esperamos explicitamente la respuesta del
        # endpoint /resoluciones/data que dispara el click.
        click_realizado = True
        logger.info("Esperando resultados...")
        try:
            with page.expect_response(
                lambda r: "/resoluciones/data" in r.url,
                timeout=30000,
            ) as resp_info:
                boton_buscar.click()
                logger.info(f"Click en Buscar (q='{texto}')")
            resp_info.value  # bloquea hasta que llega la respuesta
            logger.info("Respuesta de busqueda recibida")
        except Exception as e:
            logger.warning(f"No se recibio respuesta tras click en Buscar: {e}")

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


PDF_REINTENTOS = 3          # cantidad de intentos de descarga
PDF_TIMEOUT = 30            # timeout por intento (segundos)
PDF_BACKOFF_BASE = 2        # espera exponencial: 2s, 4s, 8s...


def _descargar_pdf(url_pdf):
    """
    Descarga el PDF con reintentos y backoff exponencial ante errores de red.
    Retorna el contenido en bytes o lanza la ultima excepcion si agota intentos.
    """
    ultimo_error = None
    for intento in range(1, PDF_REINTENTOS + 1):
        try:
            resp = requests.get(url_pdf, timeout=PDF_TIMEOUT, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            ultimo_error = e
            if intento < PDF_REINTENTOS:
                espera = PDF_BACKOFF_BASE ** intento
                logger.warning(
                    f"Descarga PDF fallo (intento {intento}/{PDF_REINTENTOS}): {e}. "
                    f"Reintentando en {espera}s..."
                )
                time.sleep(espera)
    raise ultimo_error


def verificar_pdf(url_pdf):
    """
    Descarga el PDF y busca las palabras clave en su contenido.
    Retorna:
      True  -> el PDF contiene TODAS las palabras clave
      False -> el PDF NO contiene las palabras clave
      None  -> no se pudo verificar (error de red/descarga): reintentar luego

    IMPORTANTE: ante un error NO se asume relevante. Si se devolviera True
    en caso de error, una caida del sitio de la CSJN durante el reproceso
    marcaria TODAS las pendientes como relevantes de golpe (falsos positivos
    masivos). Devolver None deja la resolucion como pendiente para reintento.
    """
    try:
        logger.info(f"Descargando PDF: {url_pdf}")
        contenido = _descargar_pdf(url_pdf)

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
        # No se pudo verificar: NO asumir relevante. Se reintentara luego.
        return None


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


def enviar_telegram(mensaje, chat_id=None, reply_to_message_id=None):
    """
    Envia un mensaje por Telegram.
      chat_id             -> destino; default a TELEGRAM_CHAT_ID (retrocompatible).
      reply_to_message_id -> si se pasa, el mensaje responde a ese mensaje
                             (util en grupos para colgar la respuesta del /buscar).
    """
    if TELEGRAM_BOT_TOKEN == "TU_TOKEN_AQUI" or TELEGRAM_CHAT_ID == "TU_CHAT_ID_AQUI":
        logger.warning("Telegram no configurado")
        logger.info(f"Mensaje:\n{mensaje}")
        return False

    destino = chat_id if chat_id is not None else TELEGRAM_CHAT_ID
    payload = {
        "chat_id": destino,
        "text": mensaje,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        if resp.json().get("ok"):
            logger.info("Telegram: mensaje enviado")
            return True
        logger.error(f"Telegram error: {resp.json()}")
        return False
    except Exception as e:
        logger.error(f"Error Telegram: {e}")
        return False


def enviar_telegram_largo(mensaje, chat_id=None, reply_to_message_id=None):
    """
    Envia un mensaje potencialmente largo respetando el limite de Telegram
    (~4096 chars). Parte por LINEAS (nunca corta en medio de una linea, para
    no romper etiquetas HTML) en trozos de <= TELEGRAM_MAX_LEN y los envia en
    secuencia. El reply_to_message_id se aplica solo al primer trozo.
    """
    lineas = mensaje.split("\n")
    trozos = []
    buffer = ""
    for linea in lineas:
        # +1 por el salto de linea que reune las lineas
        if buffer and len(buffer) + 1 + len(linea) > TELEGRAM_MAX_LEN:
            trozos.append(buffer)
            buffer = linea
        else:
            buffer = f"{buffer}\n{linea}" if buffer else linea
    if buffer:
        trozos.append(buffer)

    enviado_ok = True
    for i, trozo in enumerate(trozos):
        reply = reply_to_message_id if i == 0 else None
        ok = enviar_telegram(trozo, chat_id=chat_id, reply_to_message_id=reply)
        enviado_ok = enviado_ok and ok
        if len(trozos) > 1:
            time.sleep(0.5)  # respetar rate limits al enviar varios trozos
    return enviado_ok


# =============================================================================
# COMANDO /buscar VIA TELEGRAM (polling de getUpdates)
# =============================================================================

def cargar_telegram_state():
    """Lee el offset persistido. Devuelve {} si no existe o esta corrupto."""
    if not os.path.exists(TELEGRAM_STATE_FILE):
        return {}
    try:
        with open(TELEGRAM_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Error al leer {TELEGRAM_STATE_FILE}: {e}")
        return {}


def guardar_telegram_state(state):
    with open(TELEGRAM_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    logger.info(f"Telegram state guardado: offset={state.get('offset')}")


# -----------------------------------------------------------------------------
# Suscriptores: chats privados que hicieron /start y reciben las alertas del
# monitor ademas del grupo. El archivo lo escribe SOLO el flujo de polling
# (--telegram-poll); el monitor lo lee. Asi cada workflow commitea archivos
# distintos y no se pisan entre si.
# -----------------------------------------------------------------------------

def cargar_suscriptores():
    """Lee los chats suscriptos. Devuelve estructura vacia si no existe o esta corrupto."""
    if not os.path.exists(TELEGRAM_SUBSCRIBERS_FILE):
        return {"chats": {}}
    try:
        with open(TELEGRAM_SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("chats"), dict):
            return {"chats": {}}
        return data
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Error al leer {TELEGRAM_SUBSCRIBERS_FILE}: {e}")
        return {"chats": {}}


def guardar_suscriptores(data):
    with open(TELEGRAM_SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Suscriptores guardados: {len(data.get('chats', {}))}")


def suscribir(chat_id, nombre=None):
    """
    Alta idempotente de un chat. Devuelve True solo si es un alta nueva.
    El grupo (TELEGRAM_CHAT_ID) nunca se suscribe: ya recibe las alertas directo.
    """
    cid = str(chat_id)
    if cid == str(TELEGRAM_CHAT_ID):
        return False
    data = cargar_suscriptores()
    if cid in data["chats"]:
        return False
    data["chats"][cid] = {
        "nombre": nombre or "",
        "desde": datetime.now().isoformat(),
    }
    guardar_suscriptores(data)
    logger.info(f"Suscripto chat {cid} ({nombre})")
    return True


def desuscribir(chat_id):
    """Baja idempotente. Devuelve True solo si el chat estaba suscripto."""
    cid = str(chat_id)
    data = cargar_suscriptores()
    if cid not in data["chats"]:
        return False
    del data["chats"][cid]
    guardar_suscriptores(data)
    logger.info(f"Desuscripto chat {cid}")
    return True


def obtener_updates_telegram(offset=None):
    """
    Consulta getUpdates (short polling, timeout=0 porque el proceso es efimero).
    Devuelve la lista de updates (result) o [] ante error o falta de config.
    """
    if TELEGRAM_BOT_TOKEN == "TU_TOKEN_AQUI":
        logger.warning("Telegram no configurado; no se leen updates")
        return []

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            logger.error(f"getUpdates error: {data}")
            return []
        return data.get("result", [])
    except Exception as e:
        logger.error(f"Error en getUpdates: {e}")
        return []


def parsear_comando_buscar(texto_mensaje):
    """
    Reconoce un comando /buscar y extrae el termino.
      /buscar "frase con espacios"  -> 'frase con espacios'
      /buscar palabra1 palabra2      -> 'palabra1 palabra2'
      /buscar@MiBot texto            -> 'texto' (soporta sufijo del bot)
    Devuelve:
      - el termino (str) si es un /buscar con texto
      - ""   si es /buscar sin texto (para responder ayuda de uso)
      - None si el mensaje no es un comando /buscar
    """
    if not texto_mensaje:
        return None
    texto = texto_mensaje.strip()
    # Debe empezar con /buscar (opcionalmente /buscar@bot), como palabra
    m = re.match(r"^/buscar(?:@\w+)?(?:\s+(.*))?$", texto, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    resto = (m.group(1) or "").strip()
    if not resto:
        return ""
    # Si viene entre comillas (simples o dobles), tomar el contenido
    comillas = re.match(r'^["“](.*?)["”]\s*$', resto, re.DOTALL) \
        or re.match(r"^'(.*?)'\s*$", resto, re.DOTALL)
    if comillas:
        return comillas.group(1).strip()
    return resto


def parsear_comando_simple(texto_mensaje):
    """
    Reconoce los comandos sin argumentos del bot (soporta sufijo @bot):
      /start [payload]  -> "start" (el payload de deep-link se ignora)
      /stop | /baja     -> "stop"
      /ayuda | /help    -> "ayuda"
    Devuelve el nombre canonico del comando o None si no es ninguno.
    """
    if not texto_mensaje:
        return None
    m = re.match(r"^/(start|stop|baja|ayuda|help)(?:@\w+)?(?:\s|$)",
                 texto_mensaje.strip(), re.IGNORECASE)
    if not m:
        return None
    comando = m.group(1).lower()
    return {"baja": "stop", "help": "ayuda"}.get(comando, comando)


def texto_ayuda():
    """Texto de ayuda comun a /start en el grupo, /ayuda y mensajes no reconocidos."""
    return (
        "\U0001F50E Buscá resoluciones de la CSJN en todo el historial:\n"
        "<code>/buscar \"texto a buscar\"</code>\n"
        "Ej: <code>/buscar \"German Silva\"</code>\n\n"
        "⏱ El bot funciona por tandas: las respuestas pueden demorar "
        "entre 5 y 20 minutos.\n\n"
        "/stop para dejar de recibir alertas · /ayuda para ver este mensaje"
    )


def texto_bienvenida():
    """Respuesta al /start en un chat privado (el boton Start manda /start)."""
    return (
        "\U0001F44B ¡Hola! Soy el monitor de resoluciones de la CSJN (DAJUDECO).\n\n"
        f"✅ Quedaste suscripto: te aviso cuando salga una resolución nueva "
        f"que mencione «{html.escape(', '.join(FILTRO_PALABRAS))}».\n\n"
        + texto_ayuda()
    )


def formatear_encabezado_busqueda(termino, cantidad, truncado=False, solicitante=None):
    """Encabezado de la respuesta a un /buscar."""
    # Escapar texto controlado por el usuario: los mensajes van con parse_mode=HTML
    # y un termino con '<' haria que Telegram rechace el mensaje completo.
    termino = html.escape(termino)
    quien = f" (pedido por {html.escape(solicitante)})" if solicitante else ""
    if cantidad == 0:
        return f"\U0001F50E Búsqueda: «{termino}»{quien}\n\nSin resultados."
    msg = (f"\U0001F50E Búsqueda: «{termino}»{quien}\n"
           f"Resultados: {cantidad}")
    if truncado:
        msg += (f" (mostrando los primeros {MAX_RESULTADOS_RESPUESTA}; "
                f"refiná la búsqueda para acotar)")
    return msg


def formatear_resultado_compacto(r):
    """Formato de una linea/resolucion para listas del /buscar."""
    detalle = (r.get("detalle") or "").strip()
    if len(detalle) > 160:
        detalle = detalle[:157] + "..."
    linea = (f"\n\U0001F4C4 <b>N° {html.escape(str(r['numero']))}</b> - "
             f"{html.escape(str(r['fecha']))}\n{html.escape(detalle)}")
    if r.get("url_pdf"):
        linea += f"\n\U0001F517 <a href=\"{r['url_pdf']}\">PDF</a>"
    return linea


def formatear_mensaje(r):
    msg = (
        f"\U0001F514 <b>Nueva Resolucion CSJN - DAJUDECO</b>\n\n"
        f"\U0001F4C4 Resolucion N\u00B0 {html.escape(str(r['numero']))} - {html.escape(str(r['fecha']))}\n"
        f"\U0001F4DD {html.escape(str(r['detalle']))}\n"
    )
    if r.get("expediente"):
        msg += f"\U0001F4C1 Exp: {html.escape(str(r['expediente']))}\n"
    if r.get("url_pdf"):
        msg += f"\n\U0001F517 <a href=\"{r['url_pdf']}\">Descargar PDF</a>\n"
    msg += f"\n\U0001F50D Filtro: {', '.join(FILTRO_PALABRAS)}"
    return msg


def notificar_alerta(mensaje):
    """
    Envia una alerta de resolucion relevante al grupo y a todos los chats
    suscriptos via /start. Los fallos por suscriptor (p. ej. usuario que
    bloqueo el bot) solo se loguean: el archivo de suscriptores no se
    modifica desde aca porque solo lo escribe el flujo de polling.
    """
    enviar_telegram(mensaje)  # grupo (destino default)
    for cid in cargar_suscriptores()["chats"]:
        time.sleep(1)  # respetar rate limits al enviar a varios chats
        if not enviar_telegram(mensaje, chat_id=cid):
            logger.warning(f"No se pudo notificar al suscriptor {cid}")


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
    no_verificables = 0

    for rid, datos in pendientes.items():
        # Verificar si expiro (tolerante a entradas sin "expira")
        expira_str = datos.get("expira")
        if expira_str:
            try:
                if ahora > datetime.fromisoformat(expira_str):
                    datos["estado"] = "no_relevante"
                    expiradas += 1
                    logger.info(f"  Expirada: N°{datos.get('numero', rid)}")
                    continue
            except ValueError:
                logger.warning(f"  Fecha 'expira' invalida en N°{datos.get('numero', rid)}: {expira_str}")

        # Re-verificar PDF
        url_pdf = datos.get("url_pdf", "")
        if not url_pdf:
            datos["intentos"] = datos.get("intentos", 0) + 1
            continue

        resultado = verificar_pdf(url_pdf)
        if resultado is True:
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
            notificar_alerta(mensaje)
            time.sleep(1)
        elif resultado is False:
            # Verificado: no contiene las palabras clave
            datos["intentos"] = datos.get("intentos", 0) + 1
        else:
            # resultado is None: no se pudo verificar (error de red).
            # Se mantiene pendiente sin contar intento, para reintentar luego.
            no_verificables += 1
            logger.info(f"  No verificable (se reintenta): N°{datos.get('numero', rid)}")

    logger.info(f"Pendientes re-verificadas: {len(pendientes)} | "
                f"Nuevas relevantes: {nuevas_relevantes} | Expiradas: {expiradas} | "
                f"No verificables (reintento): {no_verificables}")


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
                resultado = verificar_pdf(r["url_pdf"])
                if resultado is True:
                    logger.info(f"  MATCH en PDF: N\u00B0{r['numero']} - {r['detalle'][:60]}")
                    relevantes.append(r)
                elif resultado is False:
                    logger.info(f"  No relevante: N\u00B0{r['numero']} - {r['detalle'][:60]}")
                    no_relevantes.append(r)
                else:
                    # None: no se pudo verificar (error de red). Queda pendiente
                    # para reintentar en el proximo ciclo, no se asume relevante.
                    logger.info(f"  No verificable (queda pendiente): N\u00B0{r['numero']} - {r['detalle'][:60]}")
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
                notificar_alerta(formatear_mensaje(r))
                time.sleep(1)
        else:
            logger.info(f"Ninguna de las {len(no_vistas)} nuevas es relevante")
            enviar_telegram(
                f"\u2705 Chequeo completado. {len(no_vistas)} resoluciones nuevas "
                f"procesadas, ninguna menciona {', '.join(FILTRO_PALABRAS)}."
            )

        # Paso 6: Guardar registro
        guardar_vistas(vistas)
        logger.info(f"Completado: {len(relevantes)} relevantes de {len(no_vistas)} nuevas")

    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        enviar_telegram(f"\u26A0\uFE0F <b>Error en Monitor CSJN</b>\n\n{str(e)[:500]}")


def _ejecutar_busqueda(termino, chat_id, message_id, solicitante=None):
    """
    Ejecuta un /buscar: scrapea la CSJN con q=termino (todo el historial) y
    responde al chat de origen. Aisla los errores para no cortar el resto.
    """
    logger.info(f"Ejecutando /buscar '{termino}' para chat {chat_id}")
    try:
        crudos = obtener_todas_las_resoluciones(
            texto=termino, aplicar_fecha=False, max_resultados=BUSCAR_MAX_RESULTADOS
        )
        resoluciones = [parsear_resolucion(item) for item in crudos]
        total = len(resoluciones)
        truncado = total > MAX_RESULTADOS_RESPUESTA or total >= BUSCAR_MAX_RESULTADOS
        encabezado = formatear_encabezado_busqueda(
            termino, total, truncado=truncado, solicitante=solicitante
        )
        cuerpo = "".join(
            formatear_resultado_compacto(r)
            for r in resoluciones[:MAX_RESULTADOS_RESPUESTA]
        )
        enviar_telegram_largo(
            encabezado + ("\n" + cuerpo if cuerpo else ""),
            chat_id=chat_id,
            reply_to_message_id=message_id,
        )
    except Exception as e:
        logger.error(f"Error en /buscar '{termino}': {e}")
        import traceback
        logger.error(traceback.format_exc())
        enviar_telegram(
            "\u26A0\uFE0F La b\u00FAsqueda fall\u00F3. Intent\u00E1 de nuevo en unos minutos.",
            chat_id=chat_id,
            reply_to_message_id=message_id,
        )


def procesar_comandos_telegram():
    """
    Lee comandos de Telegram via getUpdates y responde.
      - Grupo configurado (TELEGRAM_CHAT_ID): /buscar como siempre; /start,
        /stop y /ayuda responden la ayuda sin suscribir (el grupo ya recibe
        las alertas directo).
      - Chats privados (bot compartido por link): /start suscribe a las
        alertas, /stop da de baja, /ayuda y /buscar abiertos a cualquiera.
      - Otros grupos donde agreguen al bot: ignorados.
    Persiste el offset en TELEGRAM_STATE_FILE para no reprocesar.
    """
    logger.info("=" * 60)
    logger.info("Polling de comandos Telegram")

    if TELEGRAM_BOT_TOKEN == "TU_TOKEN_AQUI" or TELEGRAM_CHAT_ID == "TU_CHAT_ID_AQUI":
        logger.warning("Telegram no configurado; nada que procesar")
        return

    state = cargar_telegram_state()
    offset = state.get("offset")
    updates = obtener_updates_telegram(offset)
    if not updates:
        logger.info("Sin updates nuevos de Telegram")
        return

    logger.info(f"{len(updates)} updates recibidos")
    max_update_id = offset - 1 if offset is not None else -1
    comandos = 0
    busquedas = 0

    for update in updates:
        update_id = update.get("update_id", -1)
        if update_id > max_update_id:
            max_update_id = update_id

        mensaje = update.get("message")
        if not mensaje:
            continue

        chat = mensaje.get("chat", {})
        chat_id = chat.get("id")
        es_grupo = str(chat_id) == str(TELEGRAM_CHAT_ID)
        es_privado = chat.get("type") == "private"
        # Autorizacion: el grupo configurado o cualquier chat privado (bot
        # compartible). Otros grupos donde agreguen al bot se ignoran.
        if not (es_grupo or es_privado):
            continue
        # Ignorar mensajes de bots
        if mensaje.get("from", {}).get("is_bot"):
            continue

        texto = mensaje.get("text", "")
        message_id = mensaje.get("message_id")
        remitente = mensaje.get("from", {}).get("first_name")

        # --- Comandos simples: /start, /stop, /ayuda ---
        simple = parsear_comando_simple(texto)
        if simple is not None:
            comandos += 1
            if simple == "start" and es_privado:
                # El boton Start de Telegram manda /start: alta + bienvenida.
                alta_nueva = suscribir(chat_id, remitente)
                enviar_telegram(texto_bienvenida(), chat_id=chat_id)
                logger.info(f"/start de chat {chat_id} (alta nueva: {alta_nueva})")
            elif simple == "stop" and es_privado:
                if desuscribir(chat_id):
                    enviar_telegram(
                        "Listo, no vas a recibir más alertas. "
                        "Podés volver a suscribirte con /start.",
                        chat_id=chat_id,
                    )
                else:
                    enviar_telegram(
                        "No estabas suscripto. Con /start te suscribís a las alertas.",
                        chat_id=chat_id,
                    )
            else:
                # /ayuda en cualquier chat; /start y /stop en el grupo solo
                # muestran la ayuda (el grupo no se suscribe).
                enviar_telegram(
                    texto_ayuda(),
                    chat_id=chat_id,
                    reply_to_message_id=message_id if es_grupo else None,
                )
            continue

        # --- /buscar ---
        termino = parsear_comando_buscar(texto)
        if termino is None:
            # En privado, un mensaje de texto que no es comando recibe la ayuda
            # (quien recibe el bot compartido suele escribir sin la barra).
            # En el grupo se ignora, como siempre.
            if es_privado and texto.strip():
                enviar_telegram(texto_ayuda(), chat_id=chat_id)
            continue

        if termino == "":
            enviar_telegram(
                "Uso: <code>/buscar \"texto a buscar\"</code>\n"
                "Ej: <code>/buscar \"German Silva\"</code>",
                chat_id=chat_id,
                reply_to_message_id=message_id,
            )
            continue

        # Tope anti-abuso: con acceso abierto, limitar los scrapes por corrida.
        if busquedas >= MAX_BUSQUEDAS_POR_CORRIDA:
            enviar_telegram(
                "⏳ Hay muchas búsquedas en esta pasada. "
                "Intentá de nuevo en unos minutos.",
                chat_id=chat_id,
                reply_to_message_id=message_id,
            )
            continue

        comandos += 1
        busquedas += 1
        # En privado no tiene sentido el "pedido por X" del encabezado.
        _ejecutar_busqueda(termino, chat_id, message_id,
                           remitente if es_grupo else None)

    # Confirmar todos los updates leidos (incluso ignorados) para no repetir.
    if max_update_id >= 0:
        guardar_telegram_state({"offset": max_update_id + 1})
    logger.info(f"Comandos procesados: {comandos} (búsquedas: {busquedas})")


def configurar_bot_telegram():
    """
    Modo --telegram-setup: registra en Telegram el menu de comandos y las
    descripciones del bot. Se corre UNA vez a mano (es idempotente) con
    TELEGRAM_BOT_TOKEN en el entorno; no toca el estado del repo.

    La descripcion (setMyDescription) es clave para compartir el bot: Telegram
    la muestra al instante arriba del boton Iniciar/Start, asi el usuario lee
    que hace el bot antes de que llegue la bienvenida (que puede demorar por
    el polling cada ~5 min).
    """
    if TELEGRAM_BOT_TOKEN == "TU_TOKEN_AQUI":
        logger.error("TELEGRAM_BOT_TOKEN no configurado; no se puede hacer el setup")
        return

    filtro = ", ".join(FILTRO_PALABRAS)
    llamadas = [
        ("setMyCommands", {"commands": json.dumps([
            {"command": "buscar", "description": "Buscar resoluciones: /buscar \"texto\""},
            {"command": "ayuda", "description": "Cómo usar el bot"},
            {"command": "stop", "description": "Dejar de recibir alertas"},
        ])}),
        ("setMyDescription", {"description": (
            f"Monitor de resoluciones de la CSJN. Apretá Iniciar y quedás "
            f"suscripto a las alertas de resoluciones que mencionen {filtro}. "
            f"Con /buscar \"texto\" buscás en todo el historial. Las respuestas "
            f"pueden demorar 5-20 minutos (el bot corre por tandas)."
        )}),
        ("setMyShortDescription", {"short_description": (
            "Alertas y búsqueda de resoluciones de la CSJN (DAJUDECO)."
        )}),
    ]
    for metodo, params in llamadas:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{metodo}",
                data=params, timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"{metodo}: {'OK' if data.get('ok') else data}")
        except Exception as e:
            logger.error(f"{metodo}: {e}")


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
    def enviar_telegram_test(mensaje, chat_id=None, reply_to_message_id=None):
        logger.info(f"TEST - Telegram simulado (chat={chat_id or 'grupo'}): {mensaje}")
        return True
    enviar_telegram = enviar_telegram_test

    # Monkey-patch verificar_pdf para simular respuestas controladas sin red.
    # Caso 5: URL con "ERROR_TRANSITORIO" simula caida del sitio -> None.
    global verificar_pdf
    _verificar_original = verificar_pdf
    def verificar_pdf_test(url_pdf):
        if "ERROR_TRANSITORIO" in url_pdf:
            logger.info("TEST - verificar_pdf simulado: error transitorio (None)")
            return None
        return False
    verificar_pdf = verificar_pdf_test

    # Backup del archivo de vistas actual
    backup_vistas = None
    if os.path.exists(SEEN_FILE):
        backup_vistas = SEEN_FILE + ".test_backup"
        shutil.copy2(SEEN_FILE, backup_vistas)

    # Backup de suscriptores: el test corre aislado de los suscriptos reales
    backup_subs = None
    if os.path.exists(TELEGRAM_SUBSCRIBERS_FILE):
        backup_subs = TELEGRAM_SUBSCRIBERS_FILE + ".test_backup"
        shutil.copy2(TELEGRAM_SUBSCRIBERS_FILE, backup_subs)
    guardar_suscriptores({"chats": {}})

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
            # Caso 5: sin match en detalle, PDF con error transitorio de red.
            # Debe quedar PENDIENTE (no relevante) para reintentar. Es el bug
            # que antes marcaba estas resoluciones como relevantes por error.
            {
                "docId": "77777",
                "nroDoc": "77777/2026",
                "fechaCompleta": "05/04/2026",
                "detalle": "Resolucion sin match, PDF caido momentaneamente",
                "descripcionTipo": "Resolucion",
                "nroExpe": "EXP-2026-005",
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
        # Caso 5: url que dispara el error transitorio simulado
        for r in resoluciones:
            if r["id"] == "22222":
                r["url_pdf"] = ""
            if r["id"] == "77777":
                r["url_pdf"] = "https://example.com/ERROR_TRANSITORIO"

        no_vistas = [r for r in resoluciones if r["id"] and r["id"] not in vistas]
        logger.info(f"{len(no_vistas)} resoluciones no procesadas, filtrando por contenido...")

        relevantes = []
        no_relevantes = []

        for r in no_vistas:
            if contiene_palabras_clave(r["detalle"]):
                logger.info(f"  MATCH en detalle: N°{r['numero']} - {r['detalle'][:60]}")
                relevantes.append(r)
            elif r["url_pdf"]:
                resultado = verificar_pdf(r["url_pdf"])
                if resultado is True:
                    logger.info(f"  MATCH en PDF: N°{r['numero']} - {r['detalle'][:60]}")
                    relevantes.append(r)
                elif resultado is False:
                    logger.info(f"  No relevante: N°{r['numero']} - {r['detalle'][:60]}")
                    no_relevantes.append(r)
                else:
                    logger.info(f"  No verificable (queda pendiente): N°{r['numero']} - {r['detalle'][:60]}")
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

        # Caso 5 (bug de reproceso): error transitorio de red NO debe marcar relevante
        caso5 = vistas_final.get("77777", {})
        estado5 = caso5.get("estado", "NO ENCONTRADO")
        ok5 = estado5 == "pendiente"
        logger.info(f"Caso 5 (PDF error transit.):  esperado=pendiente    | obtenido={estado5}    | {'OK' if ok5 else 'FALLO'}")

        # --- Casos Telegram: parser de comandos, suscripcion y escapado HTML ---
        casos_tg = [
            ("parser /start", parsear_comando_simple("/start") == "start"),
            ("parser /start@Bot payload", parsear_comando_simple("/start@MiBot ref123") == "start"),
            ("parser /STOP", parsear_comando_simple("/STOP") == "stop"),
            ("parser /baja", parsear_comando_simple("/baja") == "stop"),
            ("parser /help", parsear_comando_simple("/help") == "ayuda"),
            ("parser /buscar no es simple", parsear_comando_simple("/buscar algo") is None),
            ("parser texto comun", parsear_comando_simple("hola /start") is None),
            ("parser prefijo invalido", parsear_comando_simple("/startx") is None),
            ("alta suscriptor", suscribir("111", "Test") is True),
            ("alta idempotente", suscribir("111", "Test") is False),
            ("grupo nunca se suscribe", suscribir(TELEGRAM_CHAT_ID) is False),
            ("suscriptor persistido", "111" in cargar_suscriptores()["chats"]),
            ("baja suscriptor", desuscribir("111") is True),
            ("baja idempotente", desuscribir("111") is False),
            ("escape HTML en /buscar", "&lt;b&gt;" in formatear_encabezado_busqueda("<b>x</b>", 0)),
        ]
        ok_tg = 0
        for nombre, ok in casos_tg:
            ok_tg += 1 if ok else 0
            logger.info(f"Caso TG ({nombre}): {'OK' if ok else 'FALLO'}")

        total_ok = sum([ok1, ok2, ok3, ok4, ok5]) + ok_tg
        total_casos = 5 + len(casos_tg)
        logger.info(f"\nResultado: {total_ok}/{total_casos} casos correctos")
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

        # Restaurar suscriptores originales (o borrar el archivo de test)
        if backup_subs and os.path.exists(backup_subs):
            shutil.move(backup_subs, TELEGRAM_SUBSCRIBERS_FILE)
            logger.info("Archivo telegram_subscribers.json restaurado desde backup")
        elif os.path.exists(TELEGRAM_SUBSCRIBERS_FILE):
            os.remove(TELEGRAM_SUBSCRIBERS_FILE)

        # Restaurar funciones monkey-patcheadas
        enviar_telegram = _enviar_original
        verificar_pdf = _verificar_original


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("MONITOR CSJN - DAJUDECO (Playwright)")
    logger.info(f"Pagina: {URL_PAGINA}")
    logger.info(f"Filtro: {FILTRO_PALABRAS}")

    if "--test" in sys.argv:
        logger.info("Modo: TEST (sin scraping real)")
        ejecutar_test()
    elif "--telegram-poll" in sys.argv:
        logger.info("Modo: polling de comandos Telegram")
        procesar_comandos_telegram()
    elif "--telegram-setup" in sys.argv:
        logger.info("Modo: setup del bot de Telegram (comandos y descripcion)")
        configurar_bot_telegram()
    elif "--once" in sys.argv:
        logger.info("Modo: ejecucion unica")
        chequear_resoluciones()
    else:
        logger.info(f"Modo: loop cada {CHECK_INTERVAL_SECONDS // 3600} horas")
        while True:
            chequear_resoluciones()
            logger.info(f"Proximo chequeo en {CHECK_INTERVAL_SECONDS // 3600} horas...")
            time.sleep(CHECK_INTERVAL_SECONDS)
