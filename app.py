"""Ecom NOW - Order Timing (Dash app for Databricks Apps).

TY vs LY KPIs from ECOMNOW_ORDERTIMING_V_TY / _V_LY.
LY rows are already date-aligned to the TY calendar (ORDER_DATE_AGG), so both
views are filtered on the same selected date.
"""
import os
import time
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import dash_bootstrap_components as dbc
from dash import Dash, dcc, html, Input, Output
from databricks import sql
from databricks.sdk.core import Config

CFG = Config()  # uses the app service principal inside Databricks Apps
WAREHOUSE_ID = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
CS = os.getenv("OT_CATALOG_SCHEMA", "sf_PLATINUM_DEV_DB.CUSTOMER_PLATINUM_DEV")
TY = f"{CS}.ECOMNOW_ORDERTIMING_V_TY"
LY = f"{CS}.ECOMNOW_ORDERTIMING_V_LY"
CUR = f"{CS}.ECOMNOW_CURRENTDATE"
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "300"))

_cache: dict = {}


def query(stmt: str, params: dict | None = None) -> pd.DataFrame:
    """Run SQL on the warehouse with a small TTL cache (source is federated and slow)."""
    key = (stmt, tuple(sorted((params or {}).items())))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    with sql.connect(
        server_hostname=CFG.host,
        http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
        credentials_provider=lambda: CFG.authenticate,
    ) as conn, conn.cursor() as cur:
        cur.execute(stmt, params or {})
        df = cur.fetchall_arrow().to_pandas()
    _cache[key] = (time.time(), df)
    return df


def where_clause(day, banners, ftypes):
    parts, params = ["ORDER_DATE_AGG = CAST(:day AS DATE)"], {"day": str(day)}
    for col, vals, p in (("BANNER", banners, "b"), ("FULLFILLMENT_TYPE", ftypes, "f")):
        if vals:
            keys = [f"{p}{i}" for i in range(len(vals))]
            parts.append(f"{col} IN ({', '.join(':' + k for k in keys)})")
            params.update(dict(zip(keys, vals)))
    return " AND ".join(parts), params


def both_periods(select_body: str, where: str, group: str = "") -> str:
    g = f" GROUP BY {group}" if group else ""
    return (f"SELECT 'TY' AS period, {select_body} FROM {TY} WHERE {where}{g} "
            f"UNION ALL "
            f"SELECT 'LY' AS period, {select_body} FROM {LY} WHERE {where}{g}")


HOUR_EXPR = "CAST(SUBSTR(ORDER_HR, 1, 2) AS INT)"  # ORDER_HR is 'HH:MM:SS' string

KPI_SELECT = """
  COUNT(DISTINCT ORDER_ID)                                                   AS orders,
  CAST(SUM(DEMAND_REVENUE)   AS DOUBLE)                                      AS demand,
  CAST(SUM(SHIPPING_REVENUE) AS DOUBLE)                                      AS shipping,
  CAST(SUM(COGS)             AS DOUBLE)                                      AS cogs,
  COUNT(DISTINCT CASE WHEN COALESCE(CANCEL_CODE,'') <> '' THEN ORDER_ID END) AS cancelled
"""

BANNERS = ["Champs", "Champs CA", "FL Canada", "Footlocker", "Kids Footlocker"]
FTYPES = ["ELECTRONIC", "PICK", "SHIP"]

app = Dash(__name__, external_stylesheets=[dbc.themes.FLATLY], title="Order Timing")
server = app.server


def default_day() -> date:
    try:
        d = query(f"SELECT MAX(ORDER_DATE_AGG) AS d FROM {TY}")["d"].iloc[0]
        return pd.to_datetime(d).date()
    except Exception:  # noqa: BLE001
        return date.today()


def kpi_card(id_):
    return dbc.Col(dbc.Card(dbc.CardBody(id=id_), className="shadow-sm text-center"), md=2)


def serve_layout():
    return dbc.Container(fluid=True, children=[
        html.H2("Ecom NOW - Order Timing", className="mt-3"),
        html.Div(id="asof", className="text-muted mb-2"),
        dbc.Row([
            dbc.Col(dcc.DatePickerSingle(id="day", date=default_day(), display_format="YYYY-MM-DD"), md=2),
            dbc.Col(dcc.Dropdown(BANNERS, multi=True, id="banner", placeholder="Banner (all)"), md=4),
            dbc.Col(dcc.Dropdown(FTYPES, multi=True, id="ftype", placeholder="Fulfillment type (all)"), md=4),
        ], className="mb-3"),
        dcc.Loading(children=[
            dbc.Row([kpi_card(k) for k in ("k_orders", "k_demand", "k_aov", "k_ship", "k_margin", "k_cancel")],
                    className="g-2 mb-3"),
            dbc.Row([
                dbc.Col(dcc.Graph(id="g_hour_cum"), md=6),
                dbc.Col(dcc.Graph(id="g_hour_orders"), md=6),
            ]),
            dbc.Row([
                dbc.Col(dcc.Graph(id="g_banner"), md=6),
                dbc.Col(dcc.Graph(id="g_ftype"), md=6),
            ]),
        ]),
        dcc.Interval(id="tick", interval=5 * 60 * 1000),  # refresh every 5 min
    ])


app.layout = serve_layout


def card(title, ty, ly, fmt):
    delta = (ty - ly) / ly if ly else None
    color = "success" if (delta or 0) >= 0 else "danger"
    return [
        html.Div(title, className="text-muted small"),
        html.H4(fmt(ty)),
        html.Div(f"LY {fmt(ly)}", className="small"),
        dbc.Badge(f"{delta:+.1%}" if delta is not None else "n/a", color=color),
    ]


money = lambda v: f"${v:,.0f}"
money2 = lambda v: f"${v:,.2f}"
num = lambda v: f"{v:,.0f}"
pct = lambda v: f"{v:.1%}"
EMPTY = pd.Series({"orders": 0, "demand": 0.0, "shipping": 0.0, "cogs": 0.0, "cancelled": 0})


@app.callback(
    [Output(k, "children") for k in ("k_orders", "k_demand", "k_aov", "k_ship", "k_margin", "k_cancel", "asof")]
    + [Output(g, "figure") for g in ("g_hour_cum", "g_hour_orders", "g_banner", "g_ftype")],
    [Input("day", "date"), Input("banner", "value"), Input("ftype", "value"), Input("tick", "n_intervals")],
)
def refresh(day, banners, ftypes, _):
    w, p = where_clause(day, banners, ftypes)

    k = query(both_periods(KPI_SELECT, w), p).set_index("period").fillna(0)
    t = k.loc["TY"].astype(float) if "TY" in k.index else EMPTY
    l = k.loc["LY"].astype(float) if "LY" in k.index else EMPTY
    aov = lambda r: r.demand / r.orders if r.orders else 0
    mgn = lambda r: (r.demand - r.cogs) / r.demand if r.demand else 0
    canc = lambda r: r.cancelled / r.orders if r.orders else 0

    asof = query(f"SELECT CAST(MAX(CURRENTDATEEST) AS STRING) AS d FROM {CUR}")["d"].iloc[0]

    hr = query(both_periods(
        f"{HOUR_EXPR} AS hr, CAST(SUM(DEMAND_REVENUE) AS DOUBLE) AS demand, COUNT(DISTINCT ORDER_ID) AS orders",
        w, HOUR_EXPR), p).sort_values(["period", "hr"])
    hr["cum_demand"] = hr.groupby("period")["demand"].cumsum()

    def lines(y, title):
        fig = go.Figure()
        for per, dash_ in (("TY", "solid"), ("LY", "dash")):
            d = hr[hr.period == per]
            fig.add_scatter(x=d.hr, y=d[y], name=per, mode="lines+markers", line=dict(dash=dash_))
        fig.update_layout(title=title, xaxis=dict(title="Hour of day (EST)", dtick=1, range=[-0.5, 23.5]),
                          margin=dict(t=50), template="plotly_white")
        return fig

    def bars(col, title):
        d = query(both_periods(f"{col} AS dim, CAST(SUM(DEMAND_REVENUE) AS DOUBLE) AS demand", w, col), p)
        fig = go.Figure()
        for per in ("TY", "LY"):
            s = d[d.period == per].sort_values("dim")
            fig.add_bar(x=s.dim, y=s.demand, name=per)
        fig.update_layout(title=title, barmode="group", margin=dict(t=50), template="plotly_white")
        return fig

    return (
        card("Orders", t.orders, l.orders, num),
        card("Demand $", t.demand, l.demand, money),
        card("AOV", aov(t), aov(l), money2),
        card("Shipping $", t.shipping, l.shipping, money),
        card("Margin %", mgn(t), mgn(l), pct),
        card("Cancel rate", canc(t), canc(l), pct),
        f"Order date {day} | Current date (EST) {asof}",
        lines("cum_demand", "Cumulative Demand $ by Hour - TY vs LY"),
        lines("orders", "Orders by Hour - TY vs LY"),
        bars("BANNER", "Demand $ by Banner"),
        bars("FULLFILLMENT_TYPE", "Demand $ by Fulfillment Type"),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("DATABRICKS_APP_PORT", "8000")), debug=False)
