from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import httpx

from app.settings import settings


class BCUClient:
    def __init__(self) -> None:
        self.url = settings.bcu_wsdl_url

    async def get_cotizaciones(
        self,
        monedas: list[int] | None = None,
        fecha_desde: str | None = None,
        fecha_hasta: str | None = None,
        grupo: int = 1,
    ) -> dict[str, Any]:
        moneda_list = monedas or [1]
        payload = self._build_soap_request(
            monedas=moneda_list,
            fecha_desde=fecha_desde,
            fecha_hasta=fecha_hasta,
            grupo=grupo,
        )

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.url,
                content=payload,
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": "Cotizaaction/AWSBCUCOTIZACIONES.Execute",
                },
            )
            response.raise_for_status()

        return _parse_bcu_response(response.text)

    def _build_soap_request(
        self,
        monedas: list[int],
        fecha_desde: str | None,
        fecha_hasta: str | None,
        grupo: int,
    ) -> str:
        items_xml = "".join(f"<tns:item>{int(moneda)}</tns:item>" for moneda in monedas)
        fecha_desde_xml = f"<tns:FechaDesde>{fecha_desde}</tns:FechaDesde>" if fecha_desde else ""
        fecha_hasta_xml = f"<tns:FechaHasta>{fecha_hasta}</tns:FechaHasta>" if fecha_hasta else ""

        return f'''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tns="Cotiza">
  <soap:Body>
    <tns:wsbcucotizaciones.Execute>
      <tns:Entrada>
        <tns:Moneda>
          {items_xml}
        </tns:Moneda>
        {fecha_desde_xml}
        {fecha_hasta_xml}
        <tns:Grupo>{int(grupo)}</tns:Grupo>
      </tns:Entrada>
    </tns:wsbcucotizaciones.Execute>
  </soap:Body>
</soap:Envelope>'''


def _parse_bcu_response(xml_text: str) -> dict[str, Any]:
    root = ET.fromstring(xml_text)
    ns = {
        "soap": "http://schemas.xmlsoap.org/soap/envelope/",
        "c": "Cotiza",
    }

    body = root.find("soap:Body", ns)
    if body is None:
        raise ValueError("Respuesta SOAP del BCU sin cuerpo")

    response_element = body[0]
    salida = response_element.find("c:Salida", ns)
    if salida is None:
        raise ValueError("Respuesta SOAP del BCU sin bloque Salida")

    status_node = salida.find("c:respuestastatus/c:status", ns)
    code_node = salida.find("c:respuestastatus/c:codigoerror", ns)
    message_node = salida.find("c:respuestastatus/c:mensaje", ns)

    items: list[dict[str, Any]] = []
    for dato in salida.findall(".//c:datoscotizaciones.dato", ns):
        items.append(
            {
                "Fecha": _read_text(dato.find("c:Fecha", ns)),
                "Moneda": _as_int(_read_text(dato.find("c:Moneda", ns))),
                "Nombre": _read_text(dato.find("c:Nombre", ns)),
                "CodigoISO": _read_text(dato.find("c:CodigoISO", ns)),
                "Emisor": _read_text(dato.find("c:Emisor", ns)),
                "TCC": _as_float(_read_text(dato.find("c:TCC", ns))),
                "TCV": _as_float(_read_text(dato.find("c:TCV", ns))),
                "ArbAct": _as_float(_read_text(dato.find("c:ArbAct", ns))),
                "FormaArbitrar": _as_int(_read_text(dato.find("c:FormaArbitrar", ns))),
            }
        )

    return {
        "status": _as_int(_read_text(status_node)),
        "codigoerror": _as_int(_read_text(code_node)),
        "mensaje": _read_text(message_node),
        "items": items,
    }


def _read_text(node: ET.Element | None) -> str | None:
    if node is None:
        return None
    text = node.text or ""
    return text.strip() if text else None


def _as_int(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
