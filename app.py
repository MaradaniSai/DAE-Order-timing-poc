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
from dash import Dash, dcc, html, Input, Output, dash_table
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
METRICS = {
    "demand": {
        "label": "Demand",
        "column": "demand",
        "title": "Demand by Hour",
        "fmt": lambda v: f"${v:,.0f}",
    },
    "orders": {
        "label": "Orders",
        "column": "orders",
        "title": "Orders by Hour",
        "fmt": lambda v: f"{v:,.0f}",
    },
    "shipping": {
        "label": "Shipping Revenue",
        "column": "shipping",
        "title": "Shipping Revenue by Hour",
        "fmt": lambda v: f"${v:,.0f}",
    },
}

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
            dbc.Tabs(
                id="metric_tab",
                active_tab="demand",
                className="mb-2",
                children=[dbc.Tab(label=v["label"], tab_id=k) for k, v in METRICS.items()],
            ),
            dcc.Graph(id="g_hour_metric"),
            html.H5("Comp", className="mt-4"),
            dash_table.DataTable(
                id="comp_table",
                page_action="none",
                style_table={"overflowX": "auto", "maxHeight": "420px", "overflowY": "auto"},
                style_cell={"fontSize": "12px", "padding": "6px", "whiteSpace": "nowrap", "textAlign": "right"},
                style_header={"fontWeight": "600", "backgroundColor": "#f8f9fa"},
                merge_duplicate_headers=True,
                fixed_rows={"headers": True},
            ),
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


def pct_change(ty: float, ly: float):
    if not ly:
        return None
    return (ty - ly) / ly


def safe_div(num_: float, den_: float) -> float:
    return (num_ / den_) if den_ else 0.0


def hour_label(hr: int) -> str:
    h12 = 12 if hr % 12 == 0 else hr % 12
    suffix = "AM" if hr < 12 else "PM"
    return f"{h12}:00:00 {suffix}"


@app.callback(
    [Output(k, "children") for k in ("k_orders", "k_demand", "k_aov", "k_ship", "k_margin", "k_cancel", "asof")]
    + [Output("g_hour_metric", "figure"), Output("comp_table", "columns"), Output("comp_table", "data")],
    [
        Input("day", "date"),
        Input("banner", "value"),
        Input("ftype", "value"),
        Input("metric_tab", "active_tab"),
        Input("tick", "n_intervals"),
    ],
)
def refresh(day, banners, ftypes, metric_tab, _):
    w, p = where_clause(day, banners, ftypes)

    k = query(both_periods(KPI_SELECT, w), p).set_index("period").fillna(0)
    t = k.loc["TY"].astype(float) if "TY" in k.index else EMPTY
    l = k.loc["LY"].astype(float) if "LY" in k.index else EMPTY
    aov = lambda r: r.demand / r.orders if r.orders else 0
    mgn = lambda r: (r.demand - r.cogs) / r.demand if r.demand else 0
    canc = lambda r: r.cancelled / r.orders if r.orders else 0

    asof = query(f"SELECT CAST(MAX(CURRENTDATEEST) AS STRING) AS d FROM {CUR}")["d"].iloc[0]

    hr = query(both_periods(
        f"""
        {HOUR_EXPR} AS hr,
        CAST(SUM(DEMAND_REVENUE) AS DOUBLE) AS demand,
        COUNT(DISTINCT ORDER_ID) AS orders,
        CAST(SUM(SHIPPING_REVENUE) AS DOUBLE) AS shipping
        """,
        w,
        HOUR_EXPR,
    ), p).sort_values(["period", "hr"])

    metric_key = metric_tab if metric_tab in METRICS else "demand"
    metric_col = METRICS[metric_key]["column"]
    metric_title = METRICS[metric_key]["title"]

    hrs = pd.DataFrame({"hr": list(range(24))})
    fig_hour = go.Figure()
    for per, color in (("LY", "#EA6B35"), ("TY", "#1E88E5")):
        d = hr[hr.period == per][["hr", metric_col]].copy()
        d = hrs.merge(d, on="hr", how="left").fillna({metric_col: 0})
        fig_hour.add_bar(
            x=[hour_label(h) for h in d["hr"]],
            y=d[metric_col],
            name=f"{METRICS[metric_key]['label']} {per}",
            marker_color=color,
        )
    fig_hour.update_layout(
        title=metric_title,
        barmode="group",
        xaxis=dict(title="ORDER_HR"),
        yaxis=dict(title=METRICS[metric_key]["label"]),
        margin=dict(t=50),
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )

    comp = query(both_periods(
        f"""
        {HOUR_EXPR} AS hr,
        BANNER,
        CAST(SUM(DEMAND_REVENUE) AS DOUBLE) AS demand,
        CAST(SUM(SHIPPING_REVENUE) AS DOUBLE) AS shipping,
        COUNT(DISTINCT ORDER_ID) AS orders,
        CAST(SUM(COGS) AS DOUBLE) AS cogs
        """,
        w,
        f"{HOUR_EXPR}, BANNER",
    ), p)
    comp = comp.fillna(0)
    visible_banners = banners if banners else BANNERS

    columns = [{"name": ["", "Order Hour"], "id": "order_hour"}]
    for b in visible_banners:
        columns.extend([
            {"name": [b, "Demand TY"], "id": f"{b}__demand_ty"},
            {"name": [b, "Demand LY"], "id": f"{b}__demand_ly"},
            {"name": [b, "Demand % Change"], "id": f"{b}__demand_chg"},
            {"name": [b, "Margin TY"], "id": f"{b}__margin_ty"},
            {"name": [b, "Margin LY"], "id": f"{b}__margin_ly"},
            {"name": [b, "Margin % Change"], "id": f"{b}__margin_chg"},
            {"name": [b, "AOV TY"], "id": f"{b}__aov_ty"},
            {"name": [b, "AOV LY"], "id": f"{b}__aov_ly"},
            {"name": [b, "AOV % Change"], "id": f"{b}__aov_chg"},
        ])

    def period_row(df_, period_, hr_, banner_):
        hit = df_[(df_["period"] == period_) & (df_["hr"] == hr_) & (df_["BANNER"] == banner_)]
        if hit.empty:
            return {"demand": 0.0, "cogs": 0.0, "orders": 0.0}
        s = hit.iloc[0]
        return {"demand": float(s["demand"]), "cogs": float(s["cogs"]), "orders": float(s["orders"])}

    table_data = []
    for hr_i in range(24):
        row = {"order_hour": hour_label(hr_i)}
        has_data = False
        for b in visible_banners:
            ty = period_row(comp, "TY", hr_i, b)
            ly = period_row(comp, "LY", hr_i, b)

            margin_ty = safe_div(ty["demand"] - ty["cogs"], ty["demand"])
            margin_ly = safe_div(ly["demand"] - ly["cogs"], ly["demand"])
            aov_ty = safe_div(ty["demand"], ty["orders"])
            aov_ly = safe_div(ly["demand"], ly["orders"])

            row[f"{b}__demand_ty"] = money(ty["demand"])
            row[f"{b}__demand_ly"] = money(ly["demand"])
            row[f"{b}__demand_chg"] = pct(pct_change(ty["demand"], ly["demand"])) if ly["demand"] else "n/a"
            row[f"{b}__margin_ty"] = pct(margin_ty)
            row[f"{b}__margin_ly"] = pct(margin_ly)
            row[f"{b}__margin_chg"] = pct(pct_change(margin_ty, margin_ly)) if margin_ly else "n/a"
            row[f"{b}__aov_ty"] = money2(aov_ty)
            row[f"{b}__aov_ly"] = money2(aov_ly)
            row[f"{b}__aov_chg"] = pct(pct_change(aov_ty, aov_ly)) if aov_ly else "n/a"

            has_data = has_data or bool(ty["demand"] or ly["demand"] or ty["orders"] or ly["orders"])

        if has_data:
            table_data.append(row)

    total = {"order_hour": "Total"}
    for b in visible_banners:
        bty = comp[(comp["period"] == "TY") & (comp["BANNER"] == b)]
        bly = comp[(comp["period"] == "LY") & (comp["BANNER"] == b)]
        ty_demand = float(bty["demand"].sum())
        ly_demand = float(bly["demand"].sum())
        ty_orders = float(bty["orders"].sum())
        ly_orders = float(bly["orders"].sum())
        ty_cogs = float(bty["cogs"].sum())
        ly_cogs = float(bly["cogs"].sum())

        margin_ty = safe_div(ty_demand - ty_cogs, ty_demand)
        margin_ly = safe_div(ly_demand - ly_cogs, ly_demand)
        aov_ty = safe_div(ty_demand, ty_orders)
        aov_ly = safe_div(ly_demand, ly_orders)

        total[f"{b}__demand_ty"] = money(ty_demand)
        total[f"{b}__demand_ly"] = money(ly_demand)
        total[f"{b}__demand_chg"] = pct(pct_change(ty_demand, ly_demand)) if ly_demand else "n/a"
        total[f"{b}__margin_ty"] = pct(margin_ty)
        total[f"{b}__margin_ly"] = pct(margin_ly)
        total[f"{b}__margin_chg"] = pct(pct_change(margin_ty, margin_ly)) if margin_ly else "n/a"
        total[f"{b}__aov_ty"] = money2(aov_ty)
        total[f"{b}__aov_ly"] = money2(aov_ly)
        total[f"{b}__aov_chg"] = pct(pct_change(aov_ty, aov_ly)) if aov_ly else "n/a"
    table_data.append(total)

    return (
        card("Orders", t.orders, l.orders, num),
        card("Demand $", t.demand, l.demand, money),
        card("AOV", aov(t), aov(l), money2),
        card("Shipping $", t.shipping, l.shipping, money),
        card("Margin %", mgn(t), mgn(l), pct),
        card("Cancel rate", canc(t), canc(l), pct),
        f"Order date {day} | Current date (EST) {asof}",
        fig_hour,
        columns,
        table_data,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("DATABRICKS_APP_PORT", "8000")), debug=False)
