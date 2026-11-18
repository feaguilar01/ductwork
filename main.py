import os
import unicodedata
from datetime import date

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------
# CONFIGURACIÓN: cambia usuario/clave si los tuyos son distintos
# ---------------------------------------------------------------
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:admin@localhost:5432/ductcontrol",
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="DuctControl")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

PROJECT_STATUSES = ["Estimado", "Aprobado", "En fabricación", "En instalación", "Terminado"]
FAB_STATUSES = ["Pendiente", "Fabricado", "Enviado"]
CATEGORIES = ["Ducto rectangular", "Ducto redondo", "Flex", "Accesorios", "Aislamiento", "Otros"]


# ---------------- Filtros para las plantillas ----------------
def money(value):
    return "${:,.0f}".format(value or 0)


def number(value, decimals=0):
    return "{:,.{d}f}".format(value or 0, d=decimals)


def slug(value):
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return text.lower().replace(" ", "-")


templates.env.filters["money"] = money
templates.env.filters["number"] = number
templates.env.filters["slug"] = slug


# ---------------- Base de datos ----------------
def query(sql, params=None, one=False):
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, params=None, returning=False):
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if returning:
                    return cur.fetchone()[0]
    finally:
        conn.close()


def go(url):
    return RedirectResponse(url, status_code=303)


# ---------------- Dashboard ----------------
@app.get("/")
def dashboard(request: Request):
    projects = query("SELECT * FROM project_summary ORDER BY id")
    active = [p for p in projects if p["status"] != "Terminado"]
    at_risk = [p for p in active if float(p["cost_pct"]) >= 90]

    pending = query("""
        SELECT f.id, f.piece, f.quantity, f.status, p.id AS project_id, p.name AS project_name
        FROM fabrication_items f JOIN projects p ON p.id = f.project_id
        WHERE f.status <> 'Enviado' AND p.status <> 'Terminado'
        ORDER BY (f.status = 'Pendiente') DESC, p.id, f.id
    """)
    week_hours = query(
        "SELECT COALESCE(SUM(hours),0) AS h FROM time_entries "
        "WHERE work_date >= date_trunc('week', CURRENT_DATE)", one=True)["h"]

    chart_projects = [p for p in active if float(p["budget_cost"]) > 0]
    return templates.TemplateResponse(request, "dashboard.html", {
        "active": active,
        "at_risk": at_risk,
        "pending": pending,
        "week_hours": week_hours,
        "contract_value": sum(p["sale_price"] for p in active),
        "projected_profit": sum(p["sale_price"] - p["actual_cost"] for p in active if p["actual_cost"] > 0),
        "chart_labels": [p["name"] for p in chart_projects],
        "chart_budget": [float(p["budget_cost"]) for p in chart_projects],
        "chart_actual": [float(p["actual_cost"]) for p in chart_projects],
        "nav": "dashboard",
    })


# ---------------- Clientes ----------------
@app.get("/clients")
def clients(request: Request):
    rows = query("""
        SELECT c.*, COUNT(p.id) AS projects_count
        FROM clients c LEFT JOIN projects p ON p.client_id = c.id
        GROUP BY c.id ORDER BY c.name
    """)
    return templates.TemplateResponse(request, "clients.html", {"clients": rows, "nav": "clients"})


@app.post("/clients")
def clients_add(name: str = Form(...), contact: str = Form(""), phone: str = Form(""),
                email: str = Form(""), city: str = Form("")):
    execute("INSERT INTO clients (name, contact, phone, email, city) VALUES (%s,%s,%s,%s,%s)",
            (name, contact, phone, email, city))
    return go("/clients")


# ---------------- Proyectos ----------------
@app.get("/projects")
def projects_list(request: Request):
    rows = query("SELECT * FROM project_summary ORDER BY id DESC")
    return templates.TemplateResponse(request, "projects.html", {"projects": rows, "nav": "projects"})


@app.get("/projects/new")
def project_new(request: Request):
    clients_rows = query("SELECT id, name FROM clients ORDER BY name")
    return templates.TemplateResponse(request, "project_form.html", {
        "clients": clients_rows, "statuses": PROJECT_STATUSES, "nav": "projects"})


@app.post("/projects")
def project_create(client_id: int = Form(...), name: str = Form(...), address: str = Form(""),
                   city: str = Form(""), status: str = Form("Estimado"),
                   labor_rate: float = Form(65), margin_pct: float = Form(18), tax_pct: float = Form(5.3)):
    pid = execute(
        "INSERT INTO projects (client_id, name, address, city, status, start_date) "
        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
        (client_id, name, address, city, status, date.today()), returning=True)
    execute("INSERT INTO estimates (project_id, labor_rate, margin_pct, tax_pct) VALUES (%s,%s,%s,%s)",
            (pid, labor_rate, margin_pct, tax_pct))
    return go(f"/projects/{pid}")


@app.get("/projects/{pid}")
def project_detail(request: Request, pid: int):
    project = query("SELECT * FROM project_summary WHERE id = %s", (pid,), one=True)
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    items = query("""
        SELECT i.*, i.quantity * i.unit_cost AS line_total
        FROM estimate_items i JOIN estimates e ON e.id = i.estimate_id
        WHERE e.project_id = %s ORDER BY i.id""", (pid,))
    fabrication = query("SELECT * FROM fabrication_items WHERE project_id = %s ORDER BY id", (pid,))
    progress = query("SELECT * FROM installation_progress WHERE project_id = %s ORDER BY id", (pid,))
    time_rows = query("""
        SELECT t.*, u.name AS user_name FROM time_entries t JOIN users u ON u.id = t.user_id
        WHERE t.project_id = %s ORDER BY t.work_date DESC, t.id DESC LIMIT 10""", (pid,))
    costs = query("SELECT * FROM material_costs WHERE project_id = %s ORDER BY purchase_date DESC, id DESC", (pid,))
    users = query("SELECT id, name, role FROM users WHERE active ORDER BY name")
    return templates.TemplateResponse(request, "project_detail.html", {
        "p": project, "items": items, "fabrication": fabrication, "progress": progress,
        "time_rows": time_rows, "costs": costs, "users": users,
        "categories": CATEGORIES, "fab_statuses": FAB_STATUSES, "statuses": PROJECT_STATUSES,
        "today": date.today().isoformat(), "nav": "projects"})


@app.post("/projects/{pid}/status")
def project_status(pid: int, status: str = Form(...)):
    execute("UPDATE projects SET status = %s WHERE id = %s", (status, pid))
    return go(f"/projects/{pid}")


# ---- Estimado ----
@app.post("/projects/{pid}/items")
def item_add(pid: int, category: str = Form(...), description: str = Form(...), quantity: float = Form(...),
             unit: str = Form(...), unit_cost: float = Form(...), labor_hours: float = Form(0)):
    execute("""
        INSERT INTO estimate_items (estimate_id, category, description, quantity, unit, unit_cost, labor_hours)
        VALUES ((SELECT id FROM estimates WHERE project_id = %s), %s,%s,%s,%s,%s,%s)""",
            (pid, category, description, quantity, unit, unit_cost, labor_hours))
    return go(f"/projects/{pid}#estimado")


@app.post("/projects/{pid}/items/{iid}/delete")
def item_delete(pid: int, iid: int):
    execute("DELETE FROM estimate_items WHERE id = %s", (iid,))
    return go(f"/projects/{pid}#estimado")


# ---- Fabricación ----
@app.post("/projects/{pid}/fabrication")
def fab_add(pid: int, piece: str = Form(...), quantity: int = Form(1)):
    execute("INSERT INTO fabrication_items (project_id, piece, quantity) VALUES (%s,%s,%s)",
            (pid, piece, quantity))
    return go(f"/projects/{pid}#fabricacion")


@app.post("/projects/{pid}/fabrication/{fid}")
def fab_status(pid: int, fid: int, status: str = Form(...)):
    execute("UPDATE fabrication_items SET status = %s, updated_at = NOW() WHERE id = %s", (status, fid))
    return go(f"/projects/{pid}#fabricacion")


# ---- Instalación ----
@app.post("/projects/{pid}/progress")
def progress_save(pid: int, zone: str = Form(...), percent: int = Form(...)):
    percent = max(0, min(100, percent))
    execute("""
        INSERT INTO installation_progress (project_id, zone, percent) VALUES (%s,%s,%s)
        ON CONFLICT (project_id, zone) DO UPDATE SET percent = EXCLUDED.percent""",
            (pid, zone, percent))
    return go(f"/projects/{pid}#instalacion")


# ---- Horas y costos reales ----
@app.post("/projects/{pid}/hours")
def hours_add(pid: int, user_id: int = Form(...), work_date: str = Form(...),
              hours: float = Form(...), notes: str = Form("")):
    execute("INSERT INTO time_entries (project_id, user_id, work_date, hours, notes) VALUES (%s,%s,%s,%s,%s)",
            (pid, user_id, work_date, hours, notes))
    return go(f"/projects/{pid}#costos")


@app.post("/projects/{pid}/costs")
def cost_add(pid: int, description: str = Form(...), supplier: str = Form(""),
             amount: float = Form(...), purchase_date: str = Form(...)):
    execute("INSERT INTO material_costs (project_id, description, supplier, amount, purchase_date) "
            "VALUES (%s,%s,%s,%s,%s)", (pid, description, supplier, amount, purchase_date))
    return go(f"/projects/{pid}#costos")


# ---------------- Reportes (pantalla, PDF y Excel) ----------------
try:
    from .reports import build_router      # cuando se corre como app.main:app
except ImportError:
    from reports import build_router       # cuando se corre como main:app

app.include_router(build_router(query, templates))
