"""
DuctControl - Módulo de reportes
7 reportes que se pueden ver en pantalla, descargar en PDF y descargar en Excel.

Cada reporte es una función que devuelve un diccionario con:
    title, subtitle, summary (indicadores) y tables (una o más tablas).
Los tres formatos (pantalla, PDF, Excel) se generan desde ese mismo diccionario,
así que si agregas un reporte nuevo funciona en los tres formatos automáticamente.
"""
import io
from datetime import date, datetime, timedelta
from xml.sax.saxutils import escape
from urllib.parse import urlencode

# ------------------------------------------------------------------
# Catálogo de reportes (lo que aparece en la página /reports)
# filters: "project" = pide elegir un proyecto, "dates" = pide rango de fechas
# ------------------------------------------------------------------
REPORTS = {
    "resumen": {
        "title": "Resumen de proyectos",
        "desc": "Todos los proyectos con presupuesto, costo real, ganancia proyectada y avance.",
        "filters": [],
    },
    "proyecto": {
        "title": "Estado de un proyecto",
        "desc": "Reporte completo de un proyecto: estimado contra real, líneas del estimado, compras, horas, fabricación y avance.",
        "filters": ["project"],
    },
    "riesgo": {
        "title": "Proyectos en riesgo",
        "desc": "Proyectos activos que usaron 90% o más del presupuesto, o cuyo costo va muy por delante del avance.",
        "filters": [],
    },
    "rentabilidad": {
        "title": "Rentabilidad de proyectos terminados",
        "desc": "Ganancia planeada contra ganancia real. Sirve para mejorar los próximos estimados.",
        "filters": [],
    },
    "fabricacion": {
        "title": "Piezas pendientes de taller",
        "desc": "Lista de piezas por fabricar o enviar a la obra, por proyecto.",
        "filters": [],
    },
    "horas": {
        "title": "Horas trabajadas",
        "desc": "Horas y costo de mano de obra por persona y por proyecto en un rango de fechas.",
        "filters": ["dates"],
    },
    "compras": {
        "title": "Compras de material",
        "desc": "Compras por proveedor y detalle de cada compra en un rango de fechas.",
        "filters": ["dates"],
    },
}


# ------------------------------------------------------------------
# Utilidades
# ------------------------------------------------------------------
def _num(x):
    return float(x) if x is not None else 0.0


def _sum(rows, i):
    return sum(r[i] for r in rows if r[i] is not None)


def _table(name, columns, rows, totals=None):
    return {"name": name, "columns": columns, "rows": rows, "totals": totals}


def _as_date(v):
    return v.date() if isinstance(v, datetime) else v


LEFT_KINDS = ("text", "date")


def fmt_value(kind, v):
    """Convierte un valor al texto que se muestra en pantalla y PDF."""
    if v is None or v == "":
        return ""
    if kind == "money":
        return ("-" if v < 0 else "") + "${:,.0f}".format(abs(v))
    if kind == "money2":
        return ("-" if v < 0 else "") + "${:,.2f}".format(abs(v))
    if kind == "num":
        return "{:,.0f}".format(v)
    if kind == "hours":
        return "{:,.1f}".format(v)
    if kind == "pct":
        return "{:.0f}%".format(v)
    if kind == "date":
        v = _as_date(v)
        return v.strftime("%m/%d/%Y") if hasattr(v, "strftime") else str(v)
    return str(v)


# ------------------------------------------------------------------
# Los 7 reportes
# ------------------------------------------------------------------
def r_resumen(q, p):
    data = q("SELECT * FROM project_summary ORDER BY id")
    rows = []
    for r in data:
        sale, actual = _num(r["sale_price"]), _num(r["actual_cost"])
        rows.append([
            r["name"], r["client_name"], r["status"],
            _num(r["budget_cost"]), sale, actual,
            (sale - actual) if actual > 0 else None,
            _num(r["cost_pct"]), _num(r["progress"]),
        ])
    cols = [("Proyecto", "text"), ("Cliente", "text"), ("Estado", "text"),
            ("Costo presupuestado", "money"), ("Precio de venta", "money"), ("Costo real", "money"),
            ("Ganancia proyectada", "money"), ("Costo usado", "pct"), ("Avance instalación", "pct")]
    totals = ["Total", "", "", _sum(rows, 3), _sum(rows, 4), _sum(rows, 5), _sum(rows, 6), None, None]
    return {
        "title": "Resumen de proyectos",
        "subtitle": "Presupuesto contra costo real de todos los proyectos. La ganancia proyectada solo se calcula en proyectos que ya tienen costos.",
        "summary": [("Proyectos", "num", len(rows)),
                    ("Valor total (precio de venta)", "money", _sum(rows, 4)),
                    ("Costo real acumulado", "money", _sum(rows, 5)),
                    ("Ganancia proyectada", "money", _sum(rows, 6))],
        "tables": [_table(None, cols, rows, totals)],
    }


def r_proyecto(q, p):
    if not p.get("project_id"):
        raise ValueError("Selecciona un proyecto para generar este reporte.")
    pid = p["project_id"]
    pr = q("SELECT * FROM project_summary WHERE id = %s", (pid,), one=True)
    if not pr:
        raise ValueError("El proyecto no existe.")
    sale, actual, budget = _num(pr["sale_price"]), _num(pr["actual_cost"]), _num(pr["budget_cost"])
    rate = _num(pr["labor_rate"])
    est_mat, act_mat = _num(pr["est_materials"]), _num(pr["actual_materials"])
    est_lab, act_lab = _num(pr["est_hours"]) * rate, _num(pr["actual_hours"]) * rate

    def pct(a, b):
        return (a / b * 100) if b else 0.0

    tables = []
    tables.append(_table("Estimado contra real", [
        ("Concepto", "text"), ("Estimado", "money"), ("Real hasta hoy", "money"),
        ("Diferencia (real menos estimado)", "money"), ("Usado", "pct")],
        [["Materiales", est_mat, act_mat, act_mat - est_mat, pct(act_mat, est_mat)],
         ["Mano de obra", est_lab, act_lab, act_lab - est_lab, pct(act_lab, est_lab)],
         ["Costo total", budget, actual, actual - budget, pct(actual, budget)]]))

    items = q("""SELECT i.category, i.description, i.quantity, i.unit, i.unit_cost,
                        i.quantity * i.unit_cost AS line_total, i.labor_hours
                 FROM estimate_items i JOIN estimates e ON e.id = i.estimate_id
                 WHERE e.project_id = %s ORDER BY i.id""", (pid,))
    if items:
        rows = [[i["category"], i["description"], _num(i["quantity"]), i["unit"], _num(i["unit_cost"]),
                 _num(i["line_total"]), _num(i["labor_hours"])] for i in items]
        tables.append(_table("Líneas del estimado", [
            ("Categoría", "text"), ("Descripción", "text"), ("Cantidad", "num"), ("Unidad", "text"),
            ("Costo por unidad", "money2"), ("Material", "money"), ("Horas", "hours")],
            rows, ["Total", "", None, "", None, _sum(rows, 5), _sum(rows, 6)]))

    costs = q("SELECT purchase_date, description, supplier, amount FROM material_costs "
              "WHERE project_id = %s ORDER BY purchase_date, id", (pid,))
    if costs:
        rows = [[c["purchase_date"], c["description"], c["supplier"] or "", _num(c["amount"])] for c in costs]
        tables.append(_table("Compras de material", [
            ("Fecha", "date"), ("Descripción", "text"), ("Proveedor", "text"), ("Monto", "money")],
            rows, ["Total", "", "", _sum(rows, 3)]))

    hours = q("""SELECT t.work_date, u.name, t.hours, t.notes FROM time_entries t
                 JOIN users u ON u.id = t.user_id WHERE t.project_id = %s
                 ORDER BY t.work_date, t.id""", (pid,))
    if hours:
        rows = [[h["work_date"], h["name"], _num(h["hours"]), h["notes"] or ""] for h in hours]
        tables.append(_table("Horas trabajadas", [
            ("Fecha", "date"), ("Persona", "text"), ("Horas", "hours"), ("Notas", "text")],
            rows, ["Total", "", _sum(rows, 2), ""]))

    fab = q("SELECT piece, quantity, status, updated_at FROM fabrication_items "
            "WHERE project_id = %s ORDER BY id", (pid,))
    if fab:
        rows = [[f["piece"], _num(f["quantity"]), f["status"], f["updated_at"]] for f in fab]
        tables.append(_table("Fabricación", [
            ("Pieza", "text"), ("Cantidad", "num"), ("Estado", "text"), ("Actualizado", "date")], rows))

    prog = q("SELECT zone, percent FROM installation_progress WHERE project_id = %s ORDER BY id", (pid,))
    if prog:
        rows = [[z["zone"], _num(z["percent"])] for z in prog]
        tables.append(_table("Avance de instalación por zona", [("Zona", "text"), ("Avance", "pct")], rows))

    where = ", ".join(x for x in [pr["address"], pr["city"]] if x)
    return {
        "title": "Estado del proyecto: " + pr["name"],
        "subtitle": "{} · {}, VA".format(pr["client_name"], where),
        "summary": [("Estado", "text", pr["status"]),
                    ("Precio de venta", "money", sale),
                    ("Costo presupuestado", "money", budget),
                    ("Costo real", "money", actual),
                    ("Ganancia proyectada", "money", sale - actual),
                    ("Costo usado", "pct", _num(pr["cost_pct"])),
                    ("Avance instalación", "pct", _num(pr["progress"])),
                    ("Horas (real de estimadas)", "text",
                     "{:,.0f} de {:,.0f}".format(_num(pr["actual_hours"]), _num(pr["est_hours"])))],
        "tables": tables,
    }


def r_riesgo(q, p):
    data = q("SELECT * FROM project_summary WHERE status <> 'Terminado' AND budget_cost > 0 ORDER BY cost_pct DESC")
    rows = []
    for r in data:
        cost_pct, prog = _num(r["cost_pct"]), _num(r["progress"])
        reasons = []
        if cost_pct >= 90:
            reasons.append("Usó {:.0f}% del presupuesto (alerta desde 90%)".format(cost_pct))
        if r["status"] == "En instalación" and prog > 0 and cost_pct - prog >= 15:
            reasons.append("Costo {:.0f}% contra avance {:.0f}%".format(cost_pct, prog))
        if reasons:
            rows.append([r["name"], r["client_name"], r["status"], _num(r["budget_cost"]), _num(r["actual_cost"]),
                         cost_pct, prog, "; ".join(reasons)])
    cols = [("Proyecto", "text"), ("Cliente", "text"), ("Estado", "text"), ("Costo presupuestado", "money"),
            ("Costo real", "money"), ("Costo usado", "pct"), ("Avance instalación", "pct"), ("Motivo", "text")]
    return {
        "title": "Proyectos en riesgo",
        "subtitle": "Proyectos activos con 90% o más del presupuesto usado, o con costo 15 puntos o más por encima del avance (solo en instalación).",
        "summary": [("Proyectos en riesgo", "num", len(rows)),
                    ("Costo real en riesgo", "money", _sum(rows, 4))],
        "tables": [_table(None, cols, rows)],
    }


def r_rentabilidad(q, p):
    data = q("SELECT * FROM project_summary WHERE status = 'Terminado' ORDER BY id")
    rows = []
    for r in data:
        sale, budget, actual = _num(r["sale_price"]), _num(r["budget_cost"]), _num(r["actual_cost"])
        planned, real = sale - budget, sale - actual
        rows.append([r["name"], r["client_name"], sale, budget, actual, planned, real, real - planned,
                     (real / sale * 100) if sale else 0.0])
    cols = [("Proyecto", "text"), ("Cliente", "text"), ("Precio de venta", "money"),
            ("Costo presupuestado", "money"), ("Costo real", "money"), ("Ganancia planeada", "money"),
            ("Ganancia real", "money"), ("Diferencia", "money"), ("Margen real", "pct")]
    sale_total = _sum(rows, 2)
    totals = ["Total", "", sale_total, _sum(rows, 3), _sum(rows, 4), _sum(rows, 5), _sum(rows, 6), _sum(rows, 7),
              (_sum(rows, 6) / sale_total * 100) if sale_total else None]
    return {
        "title": "Rentabilidad de proyectos terminados",
        "subtitle": "Diferencia positiva: el proyecto ganó más de lo planeado. Diferencia negativa: ganó menos.",
        "summary": [("Proyectos terminados", "num", len(rows)),
                    ("Ganancia planeada", "money", _sum(rows, 5)),
                    ("Ganancia real", "money", _sum(rows, 6)),
                    ("Diferencia", "money", _sum(rows, 7))],
        "tables": [_table(None, cols, rows, totals if rows else None)],
    }


def r_fabricacion(q, p):
    data = q("""SELECT p.name AS project_name, f.piece, f.quantity, f.status, f.updated_at
                FROM fabrication_items f JOIN projects p ON p.id = f.project_id
                WHERE f.status <> 'Enviado' AND p.status <> 'Terminado'
                ORDER BY p.name, (f.status = 'Pendiente') DESC, f.id""")
    rows = [[d["project_name"], d["piece"], _num(d["quantity"]), d["status"], d["updated_at"]] for d in data]
    pending = sum(r[2] for r in rows if r[3] == "Pendiente")
    made = sum(r[2] for r in rows if r[3] == "Fabricado")
    cols = [("Proyecto", "text"), ("Pieza", "text"), ("Cantidad", "num"), ("Estado", "text"),
            ("Última actualización", "date")]
    return {
        "title": "Piezas pendientes de taller",
        "subtitle": "Piezas que todavía no se han enviado a la obra, en proyectos activos. Pendiente = falta fabricar. Fabricado = lista en taller, falta enviar.",
        "summary": [("Piezas por fabricar", "num", pending), ("Fabricadas sin enviar", "num", made)],
        "tables": [_table(None, cols, rows, ["Total", "", _sum(rows, 2), "", ""] if rows else None)],
    }


def r_horas(q, p):
    d1, d2 = p["date_from"], p["date_to"]
    by_person = q("""SELECT u.name, u.role, COUNT(*) AS entries, SUM(t.hours) AS hours,
                            SUM(t.hours * COALESCE(e.labor_rate, 0)) AS cost
                     FROM time_entries t JOIN users u ON u.id = t.user_id
                     LEFT JOIN estimates e ON e.project_id = t.project_id
                     WHERE t.work_date BETWEEN %s AND %s
                     GROUP BY u.id, u.name, u.role ORDER BY hours DESC""", (d1, d2))
    by_project = q("""SELECT pr.name, SUM(t.hours) AS hours,
                             SUM(t.hours * COALESCE(e.labor_rate, 0)) AS cost
                      FROM time_entries t JOIN projects pr ON pr.id = t.project_id
                      LEFT JOIN estimates e ON e.project_id = t.project_id
                      WHERE t.work_date BETWEEN %s AND %s
                      GROUP BY pr.id, pr.name ORDER BY hours DESC""", (d1, d2))
    rows1 = [[d["name"], d["role"], _num(d["entries"]), _num(d["hours"]), _num(d["cost"])] for d in by_person]
    rows2 = [[d["name"], _num(d["hours"]), _num(d["cost"])] for d in by_project]
    return {
        "title": "Horas trabajadas",
        "subtitle": "Del {} al {}".format(d1.strftime("%m/%d/%Y"), d2.strftime("%m/%d/%Y")),
        "summary": [("Horas totales", "hours", _sum(rows1, 3)),
                    ("Costo de mano de obra", "money", _sum(rows1, 4)),
                    ("Personas", "num", len(rows1))],
        "tables": [
            _table("Por persona", [("Persona", "text"), ("Rol", "text"), ("Registros", "num"),
                                   ("Horas", "hours"), ("Costo de mano de obra", "money")],
                   rows1, ["Total", "", _sum(rows1, 2), _sum(rows1, 3), _sum(rows1, 4)] if rows1 else None),
            _table("Por proyecto", [("Proyecto", "text"), ("Horas", "hours"), ("Costo de mano de obra", "money")],
                   rows2, ["Total", _sum(rows2, 1), _sum(rows2, 2)] if rows2 else None),
        ],
    }


def r_compras(q, p):
    d1, d2 = p["date_from"], p["date_to"]
    by_supplier = q("""SELECT COALESCE(NULLIF(supplier, ''), 'Sin proveedor') AS supplier,
                              COUNT(*) AS purchases, SUM(amount) AS total
                       FROM material_costs WHERE purchase_date BETWEEN %s AND %s
                       GROUP BY 1 ORDER BY total DESC""", (d1, d2))
    detail = q("""SELECT m.purchase_date, pr.name AS project_name, m.description,
                         COALESCE(NULLIF(m.supplier, ''), 'Sin proveedor') AS supplier, m.amount
                  FROM material_costs m JOIN projects pr ON pr.id = m.project_id
                  WHERE m.purchase_date BETWEEN %s AND %s ORDER BY m.purchase_date, m.id""", (d1, d2))
    rows1 = [[d["supplier"], _num(d["purchases"]), _num(d["total"])] for d in by_supplier]
    rows2 = [[d["purchase_date"], d["project_name"], d["description"], d["supplier"], _num(d["amount"])]
             for d in detail]
    return {
        "title": "Compras de material",
        "subtitle": "Del {} al {}".format(d1.strftime("%m/%d/%Y"), d2.strftime("%m/%d/%Y")),
        "summary": [("Total comprado", "money", _sum(rows1, 2)),
                    ("Compras", "num", len(rows2)),
                    ("Proveedores", "num", len(rows1))],
        "tables": [
            _table("Por proveedor", [("Proveedor", "text"), ("Compras", "num"), ("Total", "money")],
                   rows1, ["Total", _sum(rows1, 1), _sum(rows1, 2)] if rows1 else None),
            _table("Detalle de compras", [("Fecha", "date"), ("Proyecto", "text"), ("Descripción", "text"),
                                          ("Proveedor", "text"), ("Monto", "money")],
                   rows2, ["Total", "", "", "", _sum(rows2, 4)] if rows2 else None),
        ],
    }


BUILDERS = {
    "resumen": r_resumen, "proyecto": r_proyecto, "riesgo": r_riesgo, "rentabilidad": r_rentabilidad,
    "fabricacion": r_fabricacion, "horas": r_horas, "compras": r_compras,
}


# ------------------------------------------------------------------
# Preparación de texto para pantalla (HTML)
# ------------------------------------------------------------------
def prepare(report):
    report["summary_view"] = [{"label": l, "kind": k, "text": fmt_value(k, v)} for l, k, v in report["summary"]]
    for t in report["tables"]:
        t["head"] = [{"label": l, "align": "start" if k in LEFT_KINDS else "end"} for l, k in t["columns"]]
        t["view_rows"] = [[fmt_value(k, v) for (l, k), v in zip(t["columns"], row)] for row in t["rows"]]
        t["view_totals"] = ([fmt_value(k, v) for (l, k), v in zip(t["columns"], t["totals"])]
                            if t["totals"] else None)
    return report


# ------------------------------------------------------------------
# PDF (reportlab)
# ------------------------------------------------------------------
def render_pdf(report):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether

    INK, DUCT = colors.HexColor("#17232D"), colors.HexColor("#2B6E8F")
    STEEL, LIGHT = colors.HexColor("#C5CED4"), colors.HexColor("#EDF0F2")
    SOFT = colors.HexColor("#5A6873")

    h1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=17, leading=21, textColor=INK, spaceAfter=2)
    sub = ParagraphStyle("sub", fontName="Helvetica", fontSize=9.5, leading=13, textColor=SOFT, spaceAfter=10)
    h2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=12, leading=15, textColor=INK, spaceBefore=12, spaceAfter=5, keepWithNext=1)
    cell = ParagraphStyle("cell", fontName="Helvetica", fontSize=8.5, leading=10.5, textColor=INK)
    cell_r = ParagraphStyle("cell_r", parent=cell, alignment=TA_RIGHT)
    cell_b = ParagraphStyle("cell_b", parent=cell, fontName="Helvetica-Bold")
    cell_br = ParagraphStyle("cell_br", parent=cell_b, alignment=TA_RIGHT)
    head = ParagraphStyle("head", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)
    head_r = ParagraphStyle("head_r", parent=head, alignment=TA_RIGHT)
    kl = ParagraphStyle("kl", fontName="Helvetica", fontSize=8, leading=10, textColor=SOFT)
    kv = ParagraphStyle("kv", fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=INK)

    page_w = landscape(letter)[0]
    avail = page_w - 1.0 * inch
    generated = date.today().strftime("%m/%d/%Y")

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(SOFT)
        canvas.drawString(0.5 * inch, 0.35 * inch, "DuctControl  |  " + report["title"][:80] + "  |  Generado " + generated)
        canvas.drawRightString(page_w - 0.5 * inch, 0.35 * inch, "Página %d" % doc.page)
        canvas.setStrokeColor(DUCT)
        canvas.setLineWidth(2)
        canvas.line(0.5 * inch, landscape(letter)[1] - 0.38 * inch, page_w - 0.5 * inch, landscape(letter)[1] - 0.38 * inch)
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(letter), leftMargin=0.5 * inch, rightMargin=0.5 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch, title=report["title"], author="DuctControl")
    story = [Paragraph(escape(report["title"]), h1), Paragraph(escape(report["subtitle"]), sub)]

    # Indicadores en bloques de 4
    sv = report["summary_view"]
    for i in range(0, len(sv), 4):
        chunk = sv[i:i + 4]
        labels = [Paragraph(escape(s["label"]), kl) for s in chunk]
        values = [Paragraph(escape(s["text"]), kv) for s in chunk]
        t = Table([labels, values], colWidths=[avail / 4.0] * len(chunk), hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), LIGHT), ("BOX", (0, 0), (-1, -1), 0.5, STEEL),
            ("LINEAFTER", (0, 0), (-2, -1), 0.5, STEEL), ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, 0), 6), ("BOTTOMPADDING", (0, 1), (-1, 1), 7)]))
        story += [t, Spacer(1, 4)]

    for tb in report["tables"]:
        if tb["name"]:
            story.append(Paragraph(escape(tb["name"]), h2))
        else:
            story.append(Spacer(1, 8))
        cols = tb["columns"]
        if not tb["rows"]:
            story.append(Paragraph("Sin registros para este reporte.", cell))
            continue
        header = [Paragraph(escape(l), head if k in LEFT_KINDS else head_r) for l, k in cols]
        data = [header]
        for row in tb["view_rows"]:
            data.append([Paragraph(escape(v), cell if cols[j][1] in LEFT_KINDS else cell_r) for j, v in enumerate(row)])
        if tb["view_totals"]:
            data.append([Paragraph(escape(v), cell_b if cols[j][1] in LEFT_KINDS else cell_br)
                         for j, v in enumerate(tb["view_totals"])])
        # Ancho de columnas proporcional al contenido
        weights = []
        for j, (l, k) in enumerate(cols):
            cells = [len(r[j]) for r in tb["view_rows"]] + [len(tb["view_totals"][j])] if tb["view_totals"] else [len(r[j]) for r in tb["view_rows"]]
            longest = max([len(l) * 0.75] + cells)
            # mínimo: la palabra más larga del título y el valor más largo (los números no se parten)
            min_w = max(max(len(w) for w in l.split()), max(cells + [0]) if k not in LEFT_KINDS else 0) + 2
            weights.append(max(min_w, min(longest, 42)))
        widths = [avail * w / sum(weights) for w in weights]
        t = Table(data, colWidths=widths, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), INK), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, STEEL),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        if tb["view_totals"]:
            last = len(data) - 1
            style += [("LINEABOVE", (0, last), (-1, last), 1.2, INK), ("BACKGROUND", (0, last), (-1, last), STEEL)]
        t.setStyle(TableStyle(style))
        story.append(t)

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


# ------------------------------------------------------------------
# Excel (openpyxl)
# ------------------------------------------------------------------
XLSX_FORMATS = {"money": '"$"#,##0', "money2": '"$"#,##0.00', "num": "#,##0", "hours": "#,##0.0",
                "pct": '0"%"', "date": "mm/dd/yyyy"}


def render_xlsx(report):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    ink, steel, light = "17232D", "C5CED4", "EDF0F2"
    head_fill = PatternFill("solid", fgColor=ink)
    total_fill = PatternFill("solid", fgColor=steel)
    thin = Side(style="thin", color=steel)

    wb = Workbook()
    wb.remove(wb.active)
    used = set()

    def sheet_name(name, n):
        base = (name or "Reporte")
        for ch in '[]:*?/\\':
            base = base.replace(ch, "")
        base = base[:28] or "Hoja"
        cand = base
        while cand in used:
            cand = "{} {}".format(base[:25], n)
            n += 1
        used.add(cand)
        return cand

    for idx, tb in enumerate(report["tables"]):
        ws = wb.create_sheet(sheet_name(tb["name"], idx + 1))
        r = 1
        ws.cell(r, 1, report["title"]).font = Font(bold=True, size=14, color=ink)
        r += 1
        ws.cell(r, 1, report["subtitle"]).font = Font(italic=True, color="5A6873")
        r += 1
        ws.cell(r, 1, "Generado: " + date.today().strftime("%m/%d/%Y")).font = Font(color="5A6873")
        r += 2
        if idx == 0 and report["summary"]:
            for label, kind, value in report["summary"]:
                ws.cell(r, 1, label).font = Font(bold=True)
                c = ws.cell(r, 2, value)
                c.alignment = Alignment(horizontal="left")
                if kind in XLSX_FORMATS:
                    c.number_format = XLSX_FORMATS[kind]
                r += 1
            r += 1
        if tb["name"]:
            ws.cell(r, 1, tb["name"]).font = Font(bold=True, size=12, color=ink)
            r += 1
        header_row = r
        for j, (label, kind) in enumerate(tb["columns"], start=1):
            c = ws.cell(r, j, label)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = head_fill
            c.alignment = Alignment(horizontal="left" if kind in LEFT_KINDS else "right", vertical="center", wrap_text=True)
        r += 1
        widths = [len(l) + 2 for l, k in tb["columns"]]

        def put(row_values, row_idx, bold=False, fill=None):
            for j, ((label, kind), v) in enumerate(zip(tb["columns"], row_values), start=1):
                v = _as_date(v) if kind == "date" else v
                c = ws.cell(row_idx, j, v if v != "" else None)
                if kind in XLSX_FORMATS:
                    c.number_format = XLSX_FORMATS[kind]
                c.alignment = Alignment(horizontal="left" if kind in LEFT_KINDS else "right", vertical="top")
                c.border = Border(bottom=thin)
                if bold:
                    c.font = Font(bold=True)
                if fill:
                    c.fill = fill
                widths[j - 1] = max(widths[j - 1], len(fmt_value(kind, v)) + 2)

        for row in tb["rows"]:
            put(row, r)
            r += 1
        if tb["totals"]:
            put(tb["totals"], r, bold=True, fill=total_fill)
            r += 1
        if not tb["rows"]:
            ws.cell(r, 1, "Sin registros para este reporte.").font = Font(italic=True)
        for j, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(j)].width = min(max(w, 11), 60)
        if tb["rows"]:
            ws.freeze_panes = ws.cell(header_row + 1, 1)
            ws.auto_filter.ref = "A{}:{}{}".format(header_row, get_column_letter(len(tb["columns"])),
                                                   header_row + len(tb["rows"]))
        ws.sheet_view.showGridLines = False
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------
# Rutas de FastAPI
# ------------------------------------------------------------------
def build_router(query, templates):
    from fastapi import APIRouter, Request, HTTPException
    from fastapi.responses import Response

    router = APIRouter()

    def parse_date(text, default):
        if not text:
            return default
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise HTTPException(status_code=400, detail="Fecha inválida: " + text)

    @router.get("/reports")
    def reports_index(request: Request):
        today = date.today()
        return templates.TemplateResponse(request, "reports.html", {
            "reports": REPORTS,
            "projects": query("SELECT id, name FROM projects ORDER BY name"),
            "date_from": (today - timedelta(days=30)).isoformat(),
            "date_to": today.isoformat(),
            "nav": "reports",
        })

    @router.get("/reports/{key}")
    def report_run(request: Request, key: str, format: str = "html", project_id: str = "",
                   date_from: str = "", date_to: str = ""):
        if key not in BUILDERS:
            raise HTTPException(status_code=404, detail="Reporte no encontrado")
        today = date.today()
        d1 = parse_date(date_from, today - timedelta(days=30))
        d2 = parse_date(date_to, today)
        if d1 > d2:
            d1, d2 = d2, d1
        try:
            pid = int(project_id) if project_id else None
        except ValueError:
            raise HTTPException(status_code=400, detail="Proyecto inválido")
        try:
            report = prepare(BUILDERS[key](query, {"project_id": pid, "date_from": d1, "date_to": d2}))
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err))

        stamp = today.strftime("%Y%m%d")
        if format == "pdf":
            return Response(render_pdf(report), media_type="application/pdf", headers={
                "Content-Disposition": 'attachment; filename="ductcontrol_{}_{}.pdf"'.format(key, stamp)})
        if format == "xlsx":
            return Response(render_xlsx(report),
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": 'attachment; filename="ductcontrol_{}_{}.xlsx"'.format(key, stamp)})

        params = {}
        if pid:
            params["project_id"] = pid
        if "dates" in REPORTS[key]["filters"]:
            params.update(date_from=d1.isoformat(), date_to=d2.isoformat())
        base = "/reports/{}?{}".format(key, urlencode(params))
        sep = "&" if params else ""
        links = {"pdf": base + sep + "format=pdf", "xlsx": base + sep + "format=xlsx"}
        return templates.TemplateResponse(request, "report_view.html", {
            "report": report, "key": key, "links": links, "nav": "reports",
            "generated": today.strftime("%m/%d/%Y")})

    return router
