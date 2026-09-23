# -*- coding: utf-8 -*-
"""PDF-этикетки со штрихкодом товара (58×40 мм) для термопринтера.

Каждая страница PDF = одна этикетка 58×40 мм (Code128 из SKU + название).
Печать на термопринтере этикеток: одна страница — одна этикетка.
"""
import io
from urllib.parse import quote

from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.graphics.barcode import code128

# Размер этикетки
LABEL_W = 58 * mm
LABEL_H = 40 * mm


def _draw_label(c: pdf_canvas.Canvas, sku: str, name: str, offer_id: str = "") -> None:
    """Рисует одну этикетку 58×40 мм на текущей странице."""
    w, h = LABEL_W, LABEL_H

    # Название товара (вверху, 2 строки макс.)
    c.setFont("Helvetica", 5.5)
    title = (name or "").strip()
    if len(title) > 80:
        title = title[:77] + "…"
    # Простой перенос по ширине
    max_chars = 52
    lines = []
    while title and len(lines) < 2:
        lines.append(title[:max_chars])
        title = title[max_chars:]
    if title and lines:
        lines[1] = lines[1][:-3] + "…"
    y = h - 4 * mm
    for line in lines:
        c.drawString(2 * mm, y, line)
        y -= 2.6 * mm

    # Штрихкод Code128 из SKU (центр)
    barcode_value = str(sku or "")
    if not barcode_value:
        barcode_value = "0"
    # Ограничение длины для Code128 в reportlab — до ~30 символов ок
    barcode_value = barcode_value[:30]
    bc = code128.Code128(
        barcode_value,
        barHeight=14 * mm,
        barWidth=0.28 * mm,
        quiet=False,
    )
    bc_w = bc.width
    scale = min(1.0, (w - 4 * mm) / bc_w)  # вписываем в ширину этикетки
    c.saveState()
    c.translate((w - bc_w * scale) / 2, 8 * mm)
    c.scale(scale, 1)
    bc.drawOn(c, 0, 0)
    c.restoreState()

    # Подпись под штрихкодом: SKU (+ offer_id если есть)
    c.setFont("Helvetica-Bold", 6.5)
    label = str(sku or "")
    if offer_id and str(offer_id) != str(sku):
        label = f"{sku} / {offer_id}"
    c.drawCentredString(w / 2, 3.6 * mm, label[:40])


def labels_pdf(items: list[dict]) -> bytes:
    """Генерирует многостраничный PDF: страница = этикетка 58×40 мм.

    items: [{sku, name, offer_id?}, ...]
    """
    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=(LABEL_W, LABEL_H))
    for i, it in enumerate(items):
        if i > 0:
            c.showPage()
        _draw_label(c, it.get("sku", ""), it.get("name", ""), it.get("offer_id", ""))
    c.save()
    return buf.getvalue()


def pdf_response(items: list[dict], filename: str):
    """FastAPI-ответ с PDF."""
    from fastapi.responses import Response

    data = labels_pdf(items)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{quote(filename)}"'},
    )
