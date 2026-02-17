#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
MONITOR DE RESOLUCIONES CSJN - DAJUDECO
================================================================================
Script de monitoreo automático de resoluciones de la Corte Suprema de Justicia
de la Nación (CSJN) que mencionen "Dirección de Asistencia Judicial".

Envía notificaciones por Telegram cuando se detectan nuevas resoluciones.

Autor: Generado para DAJUDECO (Dirección de Asistencia Judicial en Delitos
       Complejos y Crimen Organizado)
Fecha: Febrero 2026

REQUISITOS:
    pip install requests

CÓMO CREAR EL BOT DE TELEGRAM:
    1. Abrí Telegram y buscá @BotFather
    2. Enviá /newbot y seguí las instrucciones (nombre y username del bot)
    3. BotFather te dará un TOKEN (algo como 123456:ABC-DEF...). Copialo.
    4. Buscá tu bot en Telegram y enviále cualquier mensaje (ej: "hola")
    5. Para obtener tu CHAT_ID, abrí en el navegador:
       https://api.telegram.org/bot<TU_TOKEN>/getUpdates
    6. Buscá "chat":{"id": XXXXXXX} → ese número es tu CHAT_ID
    7. Completá las variables TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID abajo

    ALTERNATIVA para obtener CHAT_ID:
    - Buscá @userinfobot en Telegram y enviále /start → te dice tu ID
    - O buscá @RawDataBot y enviále /start

    PARA GRUPOS: agregá el bot al grupo, enviá un mensaje en el grupo,
    y consultá getUpdates. El chat_id del grupo será negativo (ej: -100123456)

USO:
    # Ejecución única (para testing):
    python monitor_csjn_dajudeco.py --once

    # Modo loop continuo (monitoreo permanente cada 4 horas):
    python monitor_csjn_dajudeco.py

    # Con intervalo personalizado (en segundos):
    python monitor_csjn_dajudeco.py --interval 7200

ENDPOINT DESCUBIERTO:
    POST https://www.csjn.gov.ar/resoluciones/data
    - Usa DataTables server-side processing
    - Retorna JSON con campo "data" conteniendo las resoluciones
    - Cada resolución tiene: docId, fecha, nroDoc, detalle, fechaCompleta, etc.
    - PDFs en: https://www.csjn.gov.ar/documentos/descargar?ID={docId}
================================================================================
"""

import requests
import json
import time
import logging
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# ==============================================================================
# CONFIGURACIÓN — Modificá estas variables según tu entorno
# ==============================================================================

# Token del bot de Telegram (obtener de @BotFather)
# Si hay variable de entorno, usarla (para GitHub Actions); si no, usar el valor directo
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "TU_TOKEN_AQUI")

# Chat ID de destino (tu usuario personal, o un grupo)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "TU_CHAT_ID_AQUI")

# Intervalo entre chequeos en segundos (default: 4 horas = 14400 segundos)
CHECK_INTERVAL_SECONDS = 14400

# Término de búsqueda (con comillas para búsqueda exacta)
SEARCH_TERM = '"Dirección de Asistencia Judicial"'

# Archivo donde se guardan las resoluciones ya vistas
SEEN_FILE = "seen_resoluciones.json"

# Archivo de log
LOG_FILE = "monitor_csjn.log"

# URL del endpoint de la CSJN (descubierto por ingeniería inversa)
CSJN_ENDPOINT = "https://www.csjn.gov.ar/resoluciones/data"

# URL base para descargar PDFs
CSJN_PDF_BASE = "https://www.csjn.gov.ar/documentos/descargar?ID="

# URL de la página de resoluciones (para referencia en el mensaje)
CSJN_PAGE_URL = "https://www.csjn.gov.ar/decisiones/resoluciones"

# Fecha mínima: solo resoluciones desde esta fecha (formato dd/mm/aaaa)
FECHA_DESDE = "01/02/2026"

# Cantidad máxima de resultados a solicitar por request
MAX_RESULTS = 50

# Timeout para requests HTTP en segundos
REQUEST_TIMEOUT = 30

# ==============================================================================
# CONFIGURACIÓN DE LOGGING
# ==============================================================================

def configurar_logging():
    """Configura el sistema de logging con salida a archivo y consola."""
    # Obtener directorio del script para poner el log ahí
    script_dir = Path(__file__).parent.resolve()
    log_path = script_dir / LOG_FILE

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger(__name__)


logger = configurar_logging()

# ==============================================================================
# FUNCIONES DE ACCESO AL ENDPOINT CSJN
# ==============================================================================

def construir_payload(termino_busqueda, start=0, length=MAX_RESULTS):
    """
    Construye el payload JSON para el endpoint de DataTables de la CSJN.

    El formulario de búsqueda usa DataTables server-side processing.
    El campo 'q' y 'qa' reciben el término de búsqueda.
    Los campos con sufijo '_a' son los valores "anteriores" que el frontend
    usa internamente para detectar cambios.

    Args:
        termino_busqueda: Texto a buscar (con comillas si es frase exacta)
        start: Offset de resultados (para paginación)
        length: Cantidad de resultados por página

    Returns:
        dict con el payload completo para el POST
    """
    return {
        "draw": 1,
        "columns": [
            {
                "data": "function",
                "name": "",
                "searchable": True,
                "orderable": False,
                "search": {"value": "", "regex": False, "fixed": []}
            }
        ],
        "order": [],
        "start": start,
        "length": length,
        "search": {"value": "", "regex": False, "fixed": []},
        "formBusqueda": {
            "qa": termino_busqueda,
            "nroDoca": "",
            "anioDoca": "",
            "tipoDoc_a": "",
            "temaPrincipal_a": "",
            "subTema_a": "0",
            "fechaDesde_a": FECHA_DESDE,
            "fechaHasta_a": "",
            "nroDoc": "",
            "anioDoc": "",
            "q": termino_busqueda,
            "fechaDesde": FECHA_DESDE,
            "fechaHasta": "",
            "temaPrincipal": "",
            "subTema": "0"
        }
    }


def obtener_headers():
    """
    Headers HTTP necesarios para que el servidor acepte el request.
    Simulan un navegador real accediendo desde la página de resoluciones.
    """
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": CSJN_PAGE_URL,
        "Origin": "https://www.csjn.gov.ar",
        "X-Requested-With": "XMLHttpRequest"
    }


def buscar_resoluciones(termino=SEARCH_TERM):
    """
    Consulta el endpoint de la CSJN buscando resoluciones que contengan
    el término especificado.

    Args:
        termino: Texto de búsqueda

    Returns:
        Lista de dicts con las resoluciones encontradas, cada una con:
        - doc_id (int): ID único del documento
        - numero (str): Número de resolución
        - fecha (str): Fecha en formato dd/mm/aaaa
        - fecha_completa (str): Fecha en texto largo
        - detalle (str): Descripción/título de la resolución
        - tipo (str): Tipo de documento (ej: "Resolución")
        - expediente (str): Número de expediente
        - temas (str): Clasificación temática
        - url_pdf (str): URL directa al PDF

    Raises:
        requests.RequestException: Si hay error de conexión o HTTP
    """
    logger.info(f"Buscando resoluciones con término: {termino}")

    payload = construir_payload(termino)
    headers = obtener_headers()

    # Usar Session para reutilizar conexión y manejar cookies
    session = requests.Session()

    # Primero visitar la página principal para obtener cookies/sesión
    try:
        session.get(CSJN_PAGE_URL, headers={
            "User-Agent": headers["User-Agent"]
        }, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        # Si falla, intentamos igual el endpoint directo
        logger.warning("No se pudo acceder a la página principal, intentando endpoint directo")

    response = session.post(
        CSJN_ENDPOINT,
        json=payload,
        headers=headers,
        timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()

    data = response.json()

    total = data.get("recordsFiltered", 0)
    items = data.get("data", [])

    logger.info(f"Encontradas {total} resoluciones (recibidas {len(items)} en esta página)")

    resoluciones = []
    for item in items:
        doc_id = item.get("docId")
        resolucion = {
            "doc_id": doc_id,
            "numero": item.get("nroDoc", "S/N"),
            "fecha": item.get("fecha", ""),
            "fecha_completa": item.get("fechaCompleta", ""),
            "detalle": item.get("detalle", "Sin detalle"),
            "tipo": item.get("descripcionTipo", "Resolución"),
            "expediente": item.get("nroExpe", ""),
            "temas": item.get("pathTemas", ""),
            "url_pdf": f"{CSJN_PDF_BASE}{doc_id}" if doc_id else ""
        }
        resoluciones.append(resolucion)

    return resoluciones


# ==============================================================================
# FUNCIONES DE PERSISTENCIA (resoluciones ya vistas)
# ==============================================================================

def obtener_ruta_seen():
    """Retorna la ruta absoluta del archivo de resoluciones vistas."""
    return Path(__file__).parent.resolve() / SEEN_FILE


def cargar_vistas():
    """
    Carga el registro de resoluciones ya vistas desde el archivo JSON.

    Returns:
        dict con estructura:
        {
            "resoluciones": {
                "<doc_id>": {
                    "numero": ...,
                    "fecha": ...,
                    "detalle": ...,
                    "primera_vez_visto": ...,
                    "url_pdf": ...
                }
            },
            "ultima_verificacion": "timestamp"
        }
    """
    ruta = obtener_ruta_seen()

    if not ruta.exists():
        logger.info(f"Archivo {SEEN_FILE} no existe, creando nuevo registro")
        return {"resoluciones": {}, "ultima_verificacion": None}

    try:
        with open(ruta, "r", encoding="utf-8") as f:
            datos = json.load(f)
            # Validar estructura mínima
            if "resoluciones" not in datos:
                datos["resoluciones"] = {}
            return datos
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Archivo {SEEN_FILE} corrupto ({e}). Creando backup y empezando de cero.")
        # Hacer backup del archivo corrupto
        backup = ruta.with_suffix(f".backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        ruta.rename(backup)
        logger.info(f"Backup guardado en: {backup}")
        return {"resoluciones": {}, "ultima_verificacion": None}


def guardar_vistas(datos):
    """
    Guarda el registro de resoluciones vistas en el archivo JSON.

    Args:
        datos: dict con la estructura de cargar_vistas()
    """
    ruta = obtener_ruta_seen()
    datos["ultima_verificacion"] = datetime.now().isoformat()

    try:
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
        logger.info(f"Registro actualizado: {len(datos['resoluciones'])} resoluciones guardadas")
    except (IOError, OSError) as e:
        logger.error(f"Error al guardar {SEEN_FILE}: {e}")


def filtrar_nuevas(resoluciones, vistas):
    """
    Filtra las resoluciones que no han sido vistas anteriormente.

    Usa doc_id como identificador único (es el ID interno de la CSJN).

    Args:
        resoluciones: Lista de dicts de buscar_resoluciones()
        vistas: dict cargado con cargar_vistas()

    Returns:
        Lista de resoluciones nuevas (no presentes en vistas)
    """
    nuevas = []
    for r in resoluciones:
        clave = str(r["doc_id"])
        if clave not in vistas["resoluciones"]:
            nuevas.append(r)
    return nuevas


def registrar_vistas(resoluciones, vistas):
    """
    Agrega las resoluciones al registro de vistas.

    Args:
        resoluciones: Lista de resoluciones nuevas
        vistas: dict de cargar_vistas() (se modifica in-place)
    """
    for r in resoluciones:
        clave = str(r["doc_id"])
        vistas["resoluciones"][clave] = {
            "numero": r["numero"],
            "fecha": r["fecha"],
            "detalle": r["detalle"][:200],
            "url_pdf": r["url_pdf"],
            "primera_vez_visto": datetime.now().isoformat()
        }


# ==============================================================================
# FUNCIONES DE NOTIFICACIÓN POR TELEGRAM
# ==============================================================================

def enviar_telegram(mensaje, parse_mode="HTML"):
    """
    Envía un mensaje por Telegram usando la Bot API.

    Args:
        mensaje: Texto del mensaje (soporta HTML básico)
        parse_mode: Formato del mensaje ("HTML" o "Markdown")

    Returns:
        True si se envió correctamente, False en caso de error
    """
    if TELEGRAM_BOT_TOKEN == "TU_TOKEN_AQUI" or TELEGRAM_CHAT_ID == "TU_CHAT_ID_AQUI":
        logger.warning("⚠️  Token o Chat ID de Telegram no configurados. Mensaje NO enviado.")
        logger.info(f"Mensaje que se habría enviado:\n{mensaje}")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False
    }

    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        result = resp.json()

        if result.get("ok"):
            logger.info("✅ Notificación enviada por Telegram exitosamente")
            return True
        else:
            logger.error(f"❌ Telegram respondió con error: {result}")
            return False

    except requests.RequestException as e:
        logger.error(f"❌ Error al enviar notificación por Telegram: {e}")
        return False


def formatear_mensaje(resolucion):
    """
    Formatea una resolución como mensaje de Telegram con HTML.

    Args:
        resolucion: dict con los datos de la resolución

    Returns:
        str con el mensaje formateado
    """
    # Escapar caracteres especiales de HTML en el detalle
    detalle = (resolucion["detalle"]
               .replace("&", "&amp;")
               .replace("<", "&lt;")
               .replace(">", "&gt;"))

    # Truncar detalle si es muy largo (Telegram tiene límite de 4096 chars)
    if len(detalle) > 500:
        detalle = detalle[:497] + "..."

    mensaje = (
        f"🔔 <b>Nueva Resolución CSJN - DAJUDECO</b>\n\n"
        f"📄 <b>Resolución N° {resolucion['numero']}</b> — {resolucion['fecha_completa']}\n\n"
        f"📝 {detalle}\n\n"
        f"📁 Exp. {resolucion['expediente']}\n"
        f"🏷️ {resolucion['temas']}\n\n"
        f"🔗 <a href=\"{resolucion['url_pdf']}\">Descargar PDF</a>\n\n"
        f"<i>Búsqueda: {SEARCH_TERM}</i>"
    )
    return mensaje


def notificar_nuevas(resoluciones):
    """
    Envía una notificación por Telegram para cada resolución nueva.

    Si hay muchas, agrupa para no hacer spam.

    Args:
        resoluciones: Lista de resoluciones nuevas

    Returns:
        int cantidad de mensajes enviados exitosamente
    """
    if not resoluciones:
        return 0

    enviados = 0

    if len(resoluciones) <= 5:
        # Enviar una notificación individual por cada resolución
        for r in resoluciones:
            mensaje = formatear_mensaje(r)
            if enviar_telegram(mensaje):
                enviados += 1
            time.sleep(1)  # Pausa entre mensajes para no superar rate limit
    else:
        # Muchas resoluciones: enviar un resumen + las 3 más recientes individuales
        resumen = (
            f"🔔 <b>CSJN - DAJUDECO: {len(resoluciones)} nuevas resoluciones</b>\n\n"
            f"Se encontraron {len(resoluciones)} resoluciones nuevas que mencionan "
            f"{SEARCH_TERM}.\n\n"
        )
        for r in resoluciones:
            resumen += f"• Res. {r['numero']} ({r['fecha']}) — {r['detalle'][:80]}...\n"

        resumen += f"\n🔗 <a href=\"{CSJN_PAGE_URL}\">Ver en sitio CSJN</a>"

        if enviar_telegram(resumen):
            enviados += 1

        # Las 3 más recientes con detalle completo
        for r in resoluciones[:3]:
            time.sleep(1)
            mensaje = formatear_mensaje(r)
            if enviar_telegram(mensaje):
                enviados += 1

    return enviados


# ==============================================================================
# FUNCIÓN PRINCIPAL DE CHEQUEO
# ==============================================================================

def chequear():
    """
    Ejecuta un ciclo completo de chequeo:
    1. Busca resoluciones en la CSJN
    2. Compara contra las ya vistas
    3. Notifica las nuevas por Telegram
    4. Actualiza el registro de vistas

    Returns:
        int cantidad de resoluciones nuevas encontradas
    """
    logger.info("=" * 60)
    logger.info("Iniciando chequeo de resoluciones CSJN")
    logger.info(f"Término de búsqueda: {SEARCH_TERM}")

    try:
        # 1. Buscar resoluciones
        resoluciones = buscar_resoluciones()

        if not resoluciones:
            logger.info("No se encontraron resoluciones para el término buscado")
            return 0

        # 2. Cargar registro de vistas
        vistas = cargar_vistas()

        # 3. Filtrar nuevas
        nuevas = filtrar_nuevas(resoluciones, vistas)

        if not nuevas:
            logger.info(
                f"Sin novedades. {len(resoluciones)} resoluciones encontradas, "
                f"todas ya registradas."
            )
            # Igual actualizar timestamp de última verificación
            guardar_vistas(vistas)
            return 0

        logger.info(f"🆕 {len(nuevas)} resoluciones NUEVAS detectadas!")

        # 4. Notificar por Telegram
        enviados = notificar_nuevas(nuevas)
        logger.info(f"Notificaciones enviadas: {enviados}")

        # 5. Registrar como vistas y guardar
        registrar_vistas(nuevas, vistas)
        guardar_vistas(vistas)

        return len(nuevas)

    except requests.ConnectionError as e:
        logger.error(f"Error de conexión con el servidor de la CSJN: {e}")
        return -1
    except requests.Timeout:
        logger.error(f"Timeout al conectar con la CSJN (>{REQUEST_TIMEOUT}s)")
        return -1
    except requests.HTTPError as e:
        logger.error(f"Error HTTP del servidor CSJN: {e}")
        return -1
    except json.JSONDecodeError as e:
        logger.error(f"Respuesta inválida del servidor (no es JSON): {e}")
        return -1
    except Exception as e:
        logger.error(f"Error inesperado: {type(e).__name__}: {e}")
        return -1


# ==============================================================================
# MODOS DE EJECUCIÓN
# ==============================================================================

def ejecutar_una_vez():
    """Ejecuta un solo chequeo y muestra resultados."""
    logger.info("Modo: ejecución única")
    resultado = chequear()
    if resultado > 0:
        logger.info(f"✅ Se encontraron {resultado} resoluciones nuevas")
    elif resultado == 0:
        logger.info("✅ No hay resoluciones nuevas")
    else:
        logger.error("❌ El chequeo terminó con errores")
    return resultado


def ejecutar_loop(intervalo=CHECK_INTERVAL_SECONDS):
    """
    Ejecuta chequeos periódicos indefinidamente.

    Args:
        intervalo: Segundos entre cada chequeo
    """
    horas = intervalo / 3600
    logger.info(f"Modo: loop continuo (cada {horas:.1f} horas / {intervalo} segundos)")
    logger.info("Presioná Ctrl+C para detener")

    ciclo = 0
    while True:
        ciclo += 1
        logger.info(f"--- Ciclo #{ciclo} ---")
        try:
            chequear()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error(f"Error en ciclo #{ciclo}: {e}")

        logger.info(f"Próximo chequeo en {horas:.1f} horas...")
        try:
            time.sleep(intervalo)
        except KeyboardInterrupt:
            logger.info("Detenido por el usuario (Ctrl+C)")
            break

    logger.info("Monitor detenido.")


# ==============================================================================
# ENTRY POINT
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Monitor de resoluciones CSJN para DAJUDECO",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python monitor_csjn_dajudeco.py --once          # Un solo chequeo
  python monitor_csjn_dajudeco.py                  # Loop cada 4 horas
  python monitor_csjn_dajudeco.py --interval 3600  # Loop cada 1 hora
        """
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Ejecutar un solo chequeo y salir"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=CHECK_INTERVAL_SECONDS,
        help=f"Intervalo entre chequeos en segundos (default: {CHECK_INTERVAL_SECONDS})"
    )

    args = parser.parse_args()

    # Validar configuración
    logger.info("=" * 60)
    logger.info("MONITOR CSJN - DAJUDECO")
    logger.info(f"Endpoint: {CSJN_ENDPOINT}")
    logger.info(f"Búsqueda: {SEARCH_TERM}")
    logger.info(f"Archivo de registro: {obtener_ruta_seen()}")

    if TELEGRAM_BOT_TOKEN == "TU_TOKEN_AQUI":
        logger.warning(
            "⚠️  TELEGRAM_BOT_TOKEN no configurado. "
            "Las notificaciones se mostrarán solo en consola/log."
        )

    if args.once:
        ejecutar_una_vez()
    else:
        ejecutar_loop(args.interval)


if __name__ == "__main__":
    main()
