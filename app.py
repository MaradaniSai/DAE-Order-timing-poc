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
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "300"))

# Hardcoded source objects (as requested)
ORDER_HEADER = "sf_PLATINUM_DEV_DB.CUSTOMER_PLATINUM_DEV.ECOMNOW_ORDER_HEADER_V"
ORDER_DETAIL = "sf_PLATINUM_DEV_DB.CUSTOMER_PLATINUM_DEV.ECOMNOW_ORDER_DETAIL_V"
CONSIGNMENTS = "sf_PLATINUM_DEV_DB.CUSTOMER_PLATINUM_DEV.ECOMNOW_CONSIGNMENTS_V"
RETURNS = "sf_PLATINUM_DEV_DB.CUSTOMER_PLATINUM_DEV.ECOMNOW_RETURNS_V"
TY = "sf_PLATINUM_DEV_DB.CUSTOMER_PLATINUM_DEV.ECOMNOW_ORDERTIMING_V_TY"
LY = "sf_PLATINUM_DEV_DB.CUSTOMER_PLATINUM_DEV.ECOMNOW_ORDERTIMING_V_LY"
CUR = "sf_PLATINUM_DEV_DB.CUSTOMER_PLATINUM_DEV.ECOMNOW_CURRENTDATE"
STR_DETAILS = "sf_PLATINUM_DEV_DB.CUSTOMER_PLATINUM_DEV.STR_DETAILS_OBF_V"

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
        "agg": "CAST(SUM(DEMAND_REVENUE) AS DOUBLE)",
    },
    "orders": {
        "label": "Orders",
        "column": "orders",
        "title": "Orders by Hour",
        "fmt": lambda v: f"{v:,.0f}",
        "agg": "COUNT(DISTINCT ORDER_ID)",
    },
    "shipping": {
        "label": "Shipping Revenue",
        "column": "shipping",
        "title": "Shipping Revenue by Hour",
        "fmt": lambda v: f"${v:,.0f}",
        "agg": "CAST(SUM(SHIPPING_REVENUE) AS DOUBLE)",
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


def load_filter_options(column_name: str, fallback: list[str]) -> list[str]:
    try:
        d = query(
            both_periods(f"{column_name} AS v", "1=1", column_name)
        )
        if "v" not in d.columns:
            return fallback
        vals = [str(v) for v in sorted(d["v"].dropna().unique()) if str(v).strip()]
        return vals or fallback
    except Exception:  # noqa: BLE001
        return fallback


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
                style_data_conditional=[
                    {"if": {"column_id": "order_hour"}, "textAlign": "left"},
                    {"if": {"filter_query": '{order_hour} = "Total"'}, "fontWeight": "700"},
                ],
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
    if not day:
        day = str(default_day())

    banner_options = load_filter_options("BANNER", BANNERS)
    ftype_options = load_filter_options("FULLFILLMENT_TYPE", FTYPES)
    banners = banners or banner_options
    ftypes = ftypes or ftype_options

    w, p = where_clause(day, banners, ftypes)

    kdf = query(both_periods(KPI_SELECT, w), p)
    k = kdf.set_index("period").fillna(0) if "period" in kdf.columns else pd.DataFrame()
    t = k.loc["TY"].astype(float) if "TY" in k.index else EMPTY
    l = k.loc["LY"].astype(float) if "LY" in k.index else EMPTY
    aov = lambda r: r.demand / r.orders if r.orders else 0
    mgn = lambda r: (r.demand - r.cogs) / r.demand if r.demand else 0
    canc = lambda r: r.cancelled / r.orders if r.orders else 0

    asof_df = query(f"SELECT CAST(MAX(CURRENTDATEEST) AS STRING) AS d FROM {CUR}")
    asof = asof_df["d"].iloc[0] if "d" in asof_df.columns and not asof_df.empty else "n/a"

    hr = query(both_periods(
        f"""
        {HOUR_EXPR} AS hr,
        CAST(SUM(DEMAND_REVENUE) AS DOUBLE) AS demand,
        COUNT(DISTINCT ORDER_ID) AS orders,
        CAST(SUM(SHIPPING_REVENUE) AS DOUBLE) AS shipping
        """,
        w,
        HOUR_EXPR,
    ), p)
    for c, default in (("period", ""), ("hr", 0), ("demand", 0.0), ("orders", 0.0), ("shipping", 0.0)):
        if c not in hr.columns:
            hr[c] = default
    hr = hr.sort_values(["period", "hr"])

    metric_key = metric_tab if metric_tab in METRICS else "demand"
    metric_col = METRICS[metric_key]["column"]
    metric_title = METRICS[metric_key]["title"]

    hrs = pd.DataFrame({"hr": list(range(24))})
    hour_categories = [hour_label(h) for h in range(24)]
    fig_hour = go.Figure()
    y_max = 0.0
    y_min = 0.0
    for per, color in (("LY", "#1E88E5"), ("TY", "#EA6B35")):
        d = hr[hr.period == per][["hr", metric_col]].copy()
        d = hrs.merge(d, on="hr", how="left").fillna({metric_col: 0})
        y_max = max(y_max, float(d[metric_col].max()))
        y_min = min(y_min, float(d[metric_col].min()))
        fig_hour.add_bar(
            x=hour_categories,
            y=d[metric_col],
            name=f"{METRICS[metric_key]['label']} {per}",
            marker_color=color,
        )
    y_upper = (y_max * 1.1) if y_max > 0 else 1.0
    y_lower = (y_min * 1.1) if y_min < 0 else 0.0
    y_tickprefix = "$" if metric_key in ("demand", "shipping") else ""
    fig_hour.update_layout(
        title=metric_title,
        barmode="group",
        bargap=0.22,
        bargroupgap=0.08,
        xaxis=dict(
            title="ORDER_HR",
            type="category",
            categoryorder="array",
            categoryarray=hour_categories,
        ),
        yaxis=dict(
            title=METRICS[metric_key]["label"],
            range=[y_lower, y_upper],
            tickprefix=y_tickprefix,
            tickformat=",.0f",
        ),
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
    for c, default in (("period", ""), ("hr", 0), ("BANNER", ""), ("demand", 0.0), ("orders", 0.0), ("shipping", 0.0), ("cogs", 0.0)):
        if c not in comp.columns:
            comp[c] = default
    comp = comp.fillna(0)
    visible_banners = banners if banners else banner_options

    columns = [{"name": ["", "Order Hour"], "id": "order_hour"}]
    metric_label = METRICS[metric_key]["label"]
    metric_fmt = METRICS[metric_key]["fmt"]
    for b in visible_banners:
        columns.extend([
            {"name": [b, f"{metric_label} TY"], "id": f"{b}__ty"},
            {"name": [b, f"{metric_label} LY"], "id": f"{b}__ly"},
            {"name": [b, "% Change"], "id": f"{b}__chg"},
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

            ty_val = float(ty[metric_col])
            ly_val = float(ly[metric_col])
            row[f"{b}__ty"] = metric_fmt(ty_val)
            row[f"{b}__ly"] = metric_fmt(ly_val)
            row[f"{b}__chg"] = pct(pct_change(ty_val, ly_val)) if ly_val else "n/a"

            has_data = has_data or bool(ty_val or ly_val)

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

        ty_metrics = {
            "demand": ty_demand,
            "orders": ty_orders,
            "shipping": float(bty["shipping"].sum()),
        }
        ly_metrics = {
            "demand": ly_demand,
            "orders": ly_orders,
            "shipping": float(bly["shipping"].sum()),
        }
        ty_val = ty_metrics[metric_col]
        ly_val = ly_metrics[metric_col]

        total[f"{b}__ty"] = metric_fmt(ty_val)
        total[f"{b}__ly"] = metric_fmt(ly_val)
        total[f"{b}__chg"] = pct(pct_change(ty_val, ly_val)) if ly_val else "n/a"
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
