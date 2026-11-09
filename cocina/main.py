import base64
import binascii
import logging
import os
import random
import time
from collections import Counter
from datetime import datetime, timezone
from functools import cache

from flask import Flask, request
from google.cloud import firestore

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cocina")

BASE_DATOS = os.environ.get("BASE_DATOS", "(default)")
TIEMPO_PREPARACION = float(os.environ.get("TIEMPO_PREPARACION", "6"))
PROBABILIDAD_FALLO = float(os.environ.get("PROBABILIDAD_FALLO", "0"))
PLATO_MISTERIOSO = "plato-misterioso"

app = Flask(__name__)


@cache
def db():
    return firestore.Client(database=BASE_DATOS)


def ahora():
    return datetime.now(timezone.utc)


@firestore.transactional
def procesar_pedido_transaccional(transaccion, pedido_ref, cantidades):
    """Ejecuta de forma atómica e idempotente la validación y descuento en Firestore.

    Lee el pedido y el inventario, verifica si ya fue procesado o si alcanzan
    las existencias, y aplica las actualizaciones en un solo commit transaccional.
    """
    pedido_doc = pedido_ref.get(transaction=transaccion)
    if not pedido_doc.exists:
        return "inexistente"

    estado_actual = pedido_doc.get("estado")
    # Idempotencia: si ya está en estado terminal (listo o rechazado), no repetir descuento
    if estado_actual in ("listo", "rechazado"):
        return estado_actual

    # Lectura de todos los productos necesarios en la transacción
    inventario_col = db().collection("inventario")
    docs_inventario = {}
    faltan = []

    for producto, cantidad in cantidades.items():
        doc_ref = inventario_col.document(producto)
        doc = doc_ref.get(transaction=transaccion)
        existencias = (doc.get("existencias") or 0) if doc.exists else 0
        docs_inventario[producto] = (doc_ref, existencias)
        if existencias < cantidad:
            faltan.append(producto)

    # Escritura atómica
    momento = ahora()
    if faltan:
        transaccion.update(
            pedido_ref,
            {
                "estado": "rechazado",
                "motivo": "Se acabó: " + ", ".join(faltan),
                "actualizado": momento,
            },
        )
        return "rechazado"
    else:
        for producto, cantidad in cantidades.items():
            doc_ref, existencias_actuales = docs_inventario[producto]
            transaccion.update(doc_ref, {"existencias": existencias_actuales - cantidad})
        transaccion.update(pedido_ref, {"estado": "listo", "actualizado": momento})
        return "listo"


@app.post("/")
def recibir():
    sobre = request.get_json(silent=True)
    try:
        pedido_id = base64.b64decode(sobre["message"]["data"], validate=True).decode()
    except (TypeError, KeyError, binascii.Error, UnicodeDecodeError):
        log.error("Llegó un sobre que no es de Pub/Sub o no trae data: %r", sobre)
        return "sobre inválido", 400

    intento = sobre.get("deliveryAttempt")
    log.info("Pedido %s, intento de entrega %s", pedido_id, intento or "desconocido")

    pedido_ref = db().collection("pedidos").document(pedido_id)
    pedido = pedido_ref.get()
    if not pedido.exists:
        log.warning("El pedido %s no existe; se descarta el mensaje", pedido_id)
        return "", 204

    estado_actual = pedido.get("estado")
    # Si ya fue terminado previamente por otra entrega o reintento, confirmamos de inmediato
    if estado_actual in ("listo", "rechazado"):
        log.info(
            "Pedido %s ya se encuentra en estado %s; se confirma sin duplicar trabajo",
            pedido_id,
            estado_actual,
        )
        return "", 204

    # Marcamos en preparación si aún está recibido
    if estado_actual == "recibido":
        pedido_ref.update({"estado": "preparando", "actualizado": ahora()})

    cantidades = Counter()
    for item in pedido.get("items"):
        cantidades[item["producto"]] += item["cantidad"]

    # Falla determinística para mensajes muertos
    if PLATO_MISTERIOSO in cantidades:
        raise RuntimeError(
            f"La cocina no tiene receta para el plato misterioso (pedido {pedido_id})"
        )

    # Tiempo de preparación simulado
    time.sleep(TIEMPO_PREPARACION)

    # Ejecución atómica mediante transacción de Firestore
    transaccion = db().transaction()
    resultado = procesar_pedido_transaccional(transaccion, pedido_ref, cantidades)
    log.info("Pedido %s resuelto en transacción como: %s", pedido_id, resultado)

    # Simulación de falla (at-least-once) si está configurada
    if random.random() < PROBABILIDAD_FALLO:
        log.warning(
            "Falla simulada en el pedido %s: el trabajo quedó hecho pero no se confirma",
            pedido_id,
        )
        return "falla simulada", 500

    return "", 204
