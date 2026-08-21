# -*- coding: utf-8 -*-
"""Экспорт данных в CSV для загрузки в Excel/Google Sheets.

CSV формируется в кодировке UTF-8 с BOM (чтобы Excel корректно открывал
кириллицу) и разделителем «;» (локаль ru-RU).
"""
import csv
import io
from datetime import date, datetime
from typing import Any, Iterable


def to_csv_bytes(rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> bytes:
    """Превращает список словарей в CSV (UTF-8 + BOM, разделитель ';').

    rows       — итерируемый набор словарей с данными
    fieldnames — порядок и состав колонок
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, delimiter=";", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _format_value(v) for k, v in row.items()})
    # UTF-8 с BOM — Excel понимает кириллицу
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _format_value(value: Any) -> str:
    """Приводит значение к строке для CSV (даты, числа — с запятой)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")[:19]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        # Excel ru-RU ждёт запятую как разделитель дробной части
        return f"{value:.2f}".replace(".", ",")
    return str(value)


def csv_response(rows: Iterable[dict[str, Any]], fieldnames: list[str], filename: str):
    """FastAPI-ответ с CSV-файлом (Content-Disposition: attachment)."""
    from fastapi.responses import Response

    data = to_csv_bytes(rows, fieldnames)
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
