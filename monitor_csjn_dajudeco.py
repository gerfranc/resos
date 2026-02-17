#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
MONITOR DE RESOLUCIONES CSJN - DAJUDECO
=============================================================================
Monitorea la página de resoluciones de la Corte Suprema de Justicia de la
Nación buscando menciones a "Dirección de Asistencia Judicial" y envía
notificaciones por Telegram cuando encuentra resoluciones nuevas.

IMPORTANTE: Usa requests.Session() para obtener cookies de sesión antes
de consultar el endpoint de datos, simulando un navegador real.

INSTRUCCIONES PARA TELEGRAM:
1. Abrir Telegram y buscar @BotFather
2. Enviar /newbot y seguir las instrucciones
3. Copiar el token que te da y pegarlo en TELEGRAM_BOT_TOKEN
4. Buscar @userinfobot en Telegram, enviar /start para obtener tu chat_id
5. Pegar tu chat_id en TELEGRAM_CHAT_ID

USO:
  python monitor_csjn_dajudeco.py --once    # Ejecutar una sola vez (testing/cron/GitHub Actions)
  python monitor_csjn_dajudeco.py            # Modo loop continuo cada CHECK_INTERVAL_SECONDS
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
# CONFIGURACIÓN (editar estos valores o usar variables de entorno)
# =============================================================================

# Telegram - se leen de variables de entorno (GitHub Actions) o se usan los defaults
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "TU_TOKEN_AQUI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "TU_CHAT_ID_AQUI")

# Intervalo entre chequeos en modo loop (4 horas = 14400 segundos)
CHECK_INTERVAL_SECONDS = 14400

# Término de búsqueda (con comillas internas)
SEARCH_TERM = '"Dirección de Asistencia Judicial"'

# Archivo donde se guardan las resoluciones ya vistas
SEEN_FILE = "seen_resoluciones.json"

# Archivo de log
LOG_FILE = "monitor_csjn.log"

# Fecha mínima de búsqueda (dd/mm/yyyy) - solo trae resoluciones desde esta fecha
FECHA_DESDE = "01/02/2026"

# URLs del sitio CSJN
URL_PAGINA_PRINCIPAL = "https://www.csjn.gov.ar/decisiones/resoluciones"
URL_ENDPOINT_DATOS = "https://www.csjn.gov.ar/resoluciones/data"
URL_BASE_PDF = "https://www.csjn.gov.ar/documentos/descargar?ID="

# =============================================================================
# CONFIGURACIÓN DE LOGGING
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
# FUNCIONES PRINCIPALES
# =============================================================================

def obtener_sesion():
    """
    Crea una sesión HTTP y visita la página principal de resoluciones
    para obtener las cookies de sesión (JSESSIONID) necesarias.
    Sin este paso, el servidor redirige a 'accesoDenegado'.
    """
    session = requests.Session()
    
    # Headers que simulan un navegador real
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    })
    
    # Paso 1: Visitar la página principal para obtener cookies
    logger.info(f"Obteniendo sesion desde: {URL_PAGINA_PRINCIPAL}")
    resp_pagina = session.get(URL_PAGINA_PRINCIPAL, timeout=30, allow_redirects=True)
    resp_pagina.raise_for_status()
    
    cookies = dict(session.cookies)
    logger.info(f"Cookies obtenidas: {list(cookies.keys())}")
    
    if "JSESSIONID" not in cookies:
        logger.warning("No se obtuvo JSESSIONID - el request podria fallar")
    
    return session


def buscar_resoluciones(session):
    """
    Consulta el endpoint de datos de la CSJN usando DataTables server-side.
    Retorna la lista de resoluciones encontradas.
    """
    logger.info(f"Buscando resoluciones con termino: {SEARCH_TERM}")
    
    # Payload que simula el request de DataTables (descubierto por ingeniería inversa)
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
    
    # Headers específicos para el POST JSON
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": URL_PAGINA_PRINCIPAL,
        "Origin": "https://www.csjn.gov.ar",
    }
    
    resp = session.post(
        URL_ENDPOINT_DATOS,
        json=payload,
        headers=headers,
        timeout=30,
        allow_redirects=False  # No seguir redirecciones - detectar accesoDenegado
    )
    
    # Si redirige, es acceso denegado
    if resp.status_code in (301, 302, 303):
        location = resp.headers.get("Location", "desconocida")
        raise Exception(f"Redireccion a {location} - sesion invalida o acceso denegado")
    
    resp.raise_for_status()
    
    data = resp.json()
    resoluciones = data.get("data", [])
    total = data.get("recordsTotal", 0)
    filtradas = data.get("recordsFiltered", 0)
    
    logger.info(f"Resultados: {len(resoluciones)} resoluciones (total: {total}, filtradas: {filtradas})")
    
    return resoluciones


def parsear_resolucion(item):
    """
    Extrae los datos relevantes de cada item del resultado.
    Campos disponibles: docId, nroDoc, fecha, fechaCompleta, detalle,
    descripcionTipo, nroExpe, pathTemas
    """
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
    """Carga el archivo de resoluciones ya vistas. Retorna un dict."""
    if not os.path.exists(SEEN_FILE):
        return {}
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Error al leer {SEEN_FILE}, creando nuevo: {e}")
        return {}


def guardar_vistas(vistas):
    """Guarda el diccionario de resoluciones vistas en el archivo JSON."""
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(vistas, f, ensure_ascii=False, indent=2)
    logger.info(f"Registro actualizado: {len(vistas)} resoluciones guardadas")


def enviar_telegram(mensaje):
    """Envía un mensaje por Telegram usando la Bot API."""
    if TELEGRAM_BOT_TOKEN == "TU_TOKEN_AQUI" or TELEGRAM_CHAT_ID == "TU_CHAT_ID_AQUI":
        logger.warning("Token o Chat ID de Telegram no configurados - mensaje NO enviado")
        logger.info(f"Mensaje que se habria enviado:\n{mensaje}")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        result = resp.json()
        if result.get("ok"):
            logger.info("Notificacion Telegram enviada correctamente")
            return True
        else:
            logger.error(f"Telegram respondio con error: {result}")
            return False
    except Exception as e:
        logger.error(f"Error al enviar mensaje Telegram: {e}")
        return False


def formatear_mensaje(resolucion):
    """Formatea el mensaje de notificación para Telegram."""
    msg = (
        f"\U0001F514 <b>Nueva Resolucion CSJN - DAJUDECO</b>\n\n"
        f"\U0001F4C4 Resolucion N\u00B0 {resolucion['numero']} - {resolucion['fecha']}\n"
        f"\U0001F4DD {resolucion['detalle']}\n"
    )
    if resolucion.get("expediente"):
        msg += f"\U0001F4C1 Exp: {resolucion['expediente']}\n"
    if resolucion.get("url_pdf"):
        msg += f"\n\U0001F517 <a href=\"{resolucion['url_pdf']}\">Descargar PDF</a>\n"
    msg += f"\n\U0001F50D Busqueda: {SEARCH_TERM}"
    return msg


def chequear_resoluciones():
    """
    Función principal: obtiene sesión, busca resoluciones, detecta nuevas
    y envía notificaciones.
    """
    logger.info("=" * 60)
    logger.info("Iniciando chequeo de resoluciones CSJN")
    logger.info(f"Termino de busqueda: {SEARCH_TERM}")
    logger.info(f"Fecha desde: {FECHA_DESDE}")
    
    try:
        # Paso 1: Obtener sesión con cookies
        session = obtener_sesion()
        
        # Paso 2: Buscar resoluciones
        resultados = buscar_resoluciones(session)
        
        if not resultados:
            logger.info("No se encontraron resoluciones para el termino de busqueda")
            return
        
        # Paso 3: Parsear resultados
        resoluciones = [parsear_resolucion(item) for item in resultados]
        
        # Paso 4: Comparar con las ya vistas
        vistas = cargar_vistas()
        nuevas = [r for r in resoluciones if r["id"] not in vistas]
        
        if not nuevas:
            logger.info(f"Sin novedades. {len(resoluciones)} resoluciones encontradas, todas ya vistas.")
            return
        
        logger.info(f"Se encontraron {len(nuevas)} resoluciones NUEVAS")
        
        # Paso 5: Notificar por Telegram
        for resolucion in nuevas:
            mensaje = formatear_mensaje(resolucion)
            enviar_telegram(mensaje)
            # Marcar como vista
            vistas[resolucion["id"]] = {
                "numero": resolucion["numero"],
                "fecha": resolucion["fecha"],
                "detalle": resolucion["detalle"],
                "url_pdf": resolucion["url_pdf"],
                "notificado": datetime.now().isoformat(),
            }
            # Pequeña pausa entre mensajes para no saturar Telegram
            time.sleep(1)
        
        # Paso 6: Guardar registro actualizado
        guardar_vistas(vistas)
        
        logger.info(f"Chequeo completado: {len(nuevas)} nuevas notificaciones enviadas")
        
    except requests.exceptions.HTTPError as e:
        logger.error(f"Error HTTP del servidor CSJN: {e}")
        # Si es acceso denegado, loguear más detalle
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"URL final: {e.response.url}")
            logger.error(f"Status: {e.response.status_code}")
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Error de conexion: {e}")
    except requests.exceptions.Timeout:
        logger.error("Timeout al conectar con el servidor CSJN")
    except Exception as e:
        logger.error(f"Error inesperado: {e}")
        import traceback
        logger.error(traceback.format_exc())


# =============================================================================
# PUNTO DE ENTRADA
# =============================================================================

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("MONITOR CSJN - DAJUDECO")
    logger.info(f"Endpoint: {URL_ENDPOINT_DATOS}")
    logger.info(f"Busqueda: {SEARCH_TERM}")
    logger.info(f"Fecha desde: {FECHA_DESDE}")
    logger.info(f"Archivo de registro: {SEEN_FILE}")
    
    if "--once" in sys.argv:
        logger.info("Modo: ejecucion unica")
        logger.info("=" * 60)
        chequear_resoluciones()
    else:
        logger.info(f"Modo: loop continuo (cada {CHECK_INTERVAL_SECONDS // 3600} horas)")
        logger.info("=" * 60)
        while True:
            chequear_resoluciones()
            logger.info(f"Proximo chequeo en {CHECK_INTERVAL_SECONDS // 3600} horas...")
            time.sleep(CHECK_INTERVAL_SECONDS)
