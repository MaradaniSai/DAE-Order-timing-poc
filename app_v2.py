"""Ecom NOW dashboard with Demand Review + Order Timing tabs (Dash app for Databricks Apps)."""
import os
import time
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import dash_bootstrap_components as dbc
from dash import Dash, Input, Output, dcc, dash_table, html
from databricks import sql
from databricks.sdk.core import Config

CFG = Config()  # uses the app service principal inside Databricks Apps
WAREHOUSE_ID = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "300"))
DEMAND_CATALOG_SCHEMA = os.getenv("DEMAND_CATALOG_SCHEMA", "SF_GOLD_DEV_DB.TRAFFIC_GOLD_DEV")

# Order Timing objects
TY = "sf_PLATINUM_DEV_DB.CUSTOMER_PLATINUM_DEV.ECOMNOW_ORDERTIMING_V_TY"
LY = "sf_PLATINUM_DEV_DB.CUSTOMER_PLATINUM_DEV.ECOMNOW_ORDERTIMING_V_LY"
CUR = "sf_PLATINUM_DEV_DB.CUSTOMER_PLATINUM_DEV.ECOMNOW_CURRENTDATE"

# Demand Review objects (as provided)
DEMAND_FACT_T = f"{DEMAND_CATALOG_SCHEMA}.rt_demand_fact_t"
DEMAND_ORDER_V = f"{DEMAND_CATALOG_SCHEMA}.rt_demand_order_v"
DEMAND_FILTERS_V = f"{DEMAND_CATALOG_SCHEMA}.rt_demand_filters_v"
DEMAND_KPI_V = f"{DEMAND_CATALOG_SCHEMA}.rt_demand_kpi_v"
DEMAND_BANNER_CHANNEL_V = f"{DEMAND_CATALOG_SCHEMA}.rt_demand_banner_channel_v"
DEMAND_HOURLY_V = f"{DEMAND_CATALOG_SCHEMA}.rt_demand_hourly_v"
DEMAND_SKU_V = f"{DEMAND_CATALOG_SCHEMA}.rt_demand_sku_v"
DEMAND_ORDER_DETAIL_V = f"{DEMAND_CATALOG_SCHEMA}.rt_demand_order_detail_v"

_cache: dict = {}
_view_columns_cache: dict[str, list[str]] = {}


def query(stmt: str, params: dict | None = None) -> pd.DataFrame:
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


def query_safe(stmt: str, params: dict | None = None) -> pd.DataFrame:
    try:
        return query(stmt, params)
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


def get_view_columns(view_name: str) -> list[str]:
    if view_name in _view_columns_cache:
        return _view_columns_cache[view_name]
    df = query_safe(f"SELECT * FROM {view_name} LIMIT 1")
    cols = list(df.columns)
    _view_columns_cache[view_name] = cols
    return cols


def find_col(columns: list[str], candidates: list[str]) -> str | None:
    if not columns:
        return None
    upper_map = {c.upper(): c for c in columns}
    for cand in candidates:
        if cand.upper() in upper_map:
            return upper_map[cand.upper()]
    for c in columns:
        uc = c.upper()
        if any(cand.upper() in uc for cand in candidates):
            return c
    return None


def debug_columns(df: pd.DataFrame, table_name: str):
    """Debug helper: Print all columns in a dataframe"""
    if df.empty:
        print(f"{table_name}: DataFrame is empty")
        return
    cols = list(df.columns)
    print(f"{table_name} columns: {cols}")
    print(f"{table_name} first row: {df.iloc[0].to_dict() if len(df) > 0 else 'No data'}")


def sql_in_filter(col: str | None, vals: list[str] | None, prefix: str, parts: list[str], params: dict):
    if not col or not vals:
        return
    keys = [f"{prefix}{i}" for i in range(len(vals))]
    parts.append(f"{col} IN ({', '.join(':' + k for k in keys)})")
    params.update(dict(zip(keys, vals)))


def demand_where(view_name: str, start_date: str | None, end_date: str | None, banners: list[str] | None, cancel_codes: list[str] | None, source_codes: list[str] | None):
    cols = get_view_columns(view_name)
    date_col = find_col(cols, ["ORDER_DATE", "ORDER_DATE_AGG", "BUSINESS_DATE", "DATE", "DT"])
    banner_col = find_col(cols, ["BANNER"])
    cancel_col = find_col(cols, ["CANCEL_CODE", "CANCEL"])
    source_col = find_col(cols, ["SOURCE_CODE", "SOURCE"])

    parts = []
    params = {}
    if date_col and start_date and end_date:
        parts.append(f"{date_col} BETWEEN CAST(:start_date AS DATE) AND CAST(:end_date AS DATE)")
        params["start_date"] = start_date
        params["end_date"] = end_date

    sql_in_filter(banner_col, banners, "b", parts, params)
    sql_in_filter(cancel_col, cancel_codes, "c", parts, params)
    sql_in_filter(source_col, source_codes, "s", parts, params)
    return (" AND ".join(parts) if parts else "1=1"), params


def build_filtered_view(view_name: str, start_date: str | None, end_date: str | None, banners: list[str] | None, cancel_codes: list[str] | None, source_codes: list[str] | None, limit_rows: int | None = None) -> pd.DataFrame:
    where_sql, params = demand_where(view_name, start_date, end_date, banners, cancel_codes, source_codes)
    sql_stmt = f"SELECT * FROM {view_name} WHERE {where_sql}"
    if limit_rows:
        sql_stmt += f" LIMIT {limit_rows}"
    d = query_safe(sql_stmt, params)
    if not d.empty:
        return d
    fallback = f"SELECT * FROM {view_name}"
    if limit_rows:
        fallback += f" LIMIT {limit_rows}"
    return query_safe(fallback)


def default_day() -> date:
    try:
        d = query(f"SELECT MAX(ORDER_DATE_AGG) AS d FROM {TY}")["d"].iloc[0]
        return pd.to_datetime(d).date()
    except Exception:  # noqa: BLE001
        return date.today()


def default_demand_range() -> tuple[date, date]:
    cols = get_view_columns(DEMAND_ORDER_V)
    date_col = find_col(cols, ["ORDER_DATE", "ORDER_DATE_AGG", "BUSINESS_DATE", "DATE", "DT"])
    if date_col:
        d = query_safe(f"SELECT MAX({date_col}) AS mx FROM {DEMAND_ORDER_V}")
        if not d.empty and "mx" in d.columns and pd.notna(d["mx"].iloc[0]):
            end = pd.to_datetime(d["mx"].iloc[0]).date()
            return end - timedelta(days=7), end
    end = date.today()
    return end - timedelta(days=7), end


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
    return (
        f"SELECT 'TY' AS period, {select_body} FROM {TY} WHERE {where}{g} "
        f"UNION ALL "
        f"SELECT 'LY' AS period, {select_body} FROM {LY} WHERE {where}{g}"
    )


HOUR_EXPR = "CAST(SUBSTR(ORDER_HR, 1, 2) AS INT)"
KPI_SELECT = """
  COUNT(DISTINCT ORDER_ID) AS orders,
  CAST(SUM(DEMAND_REVENUE) AS DOUBLE) AS demand,
  CAST(SUM(SHIPPING_REVENUE) AS DOUBLE) AS shipping,
  CAST(SUM(COGS) AS DOUBLE) AS cogs,
  COUNT(DISTINCT CASE WHEN COALESCE(CANCEL_CODE,'') <> '' THEN ORDER_ID END) AS cancelled
"""

BANNERS = ["Champs", "Champs CA", "FL Canada", "Footlocker", "Kids Footlocker"]
FTYPES = ["ELECTRONIC", "PICK", "SHIP"]
METRICS = {
    "demand": {"label": "Demand", "column": "demand", "title": "Demand by Hour", "fmt": lambda v: f"${v:,.0f}"},
    "orders": {"label": "Orders", "column": "orders", "title": "Orders by Hour", "fmt": lambda v: f"{v:,.0f}"},
    "shipping": {"label": "Shipping Revenue", "column": "shipping", "title": "Shipping Revenue by Hour", "fmt": lambda v: f"${v:,.0f}"},
}

app = Dash(__name__, external_stylesheets=[dbc.themes.FLATLY], title="Order Timing", suppress_callback_exceptions=True)
server = app.server

APP_CSS = """
        .app-shell {font-family: 'Segoe UI', Arial, sans-serif; background:#F7F9FC; color:#122039; min-height:100vh;}
        .top-title {font-size:34px; font-weight:700; margin: 10px 0 2px 0;}
        .subtle {color:#6B778C; font-size:12px;}
        .filters-label {font-size:12px; font-weight:600; color:#1D2B44; margin-bottom:4px;}
        .tile {background:#fff; border:1px solid #E6ECF5; border-radius:8px; box-shadow:0 1px 2px rgba(20,36,64,.05);}
        .kpi-title {font-size:12px; text-transform:uppercase; color:#6A7892; letter-spacing:.04em; font-weight:600;}
        .kpi-value {font-size:32px; font-weight:800; line-height:1.2; color:#0F1B2D; margin:8px 0;}
        .kpi-sub {font-size:12px; font-weight:600; margin-top:4px;}
        .kpi-delta-positive {color:#16A57A;}
        .kpi-delta-negative {color:#E33E3E;}
        .metric-tab .nav-link {border:1px solid #CFD8E3 !important; border-radius:4px !important; margin-right:6px; color:#2B3D5A; font-weight:600; padding:3px 12px; font-size:13px;}
        .metric-tab .nav-link.active {background:#D4A51B !important; color:#fff !important; border-color:#D4A51B !important;}
        .main-tab .nav-link {border:1px solid #D5B15A !important; color:#876100 !important; background:#fff; font-weight:700; padding:6px 18px; border-radius:4px !important; margin-right:8px;}
        .main-tab .nav-link.active {background:#D4A51B !important; color:#fff !important;}
        .chip {display:inline-block; background:#D4A51B; color:#fff; font-size:16px; font-weight:700; padding:6px 16px; border-radius:6px; margin:6px 0;}
        .excluded-fraud-label {color:#6B778C; font-size:12px; font-weight:600; text-align:right; padding-top:4px;}
        .dr-card-body {padding:16px 12px; display:flex; flex-direction:column; align-items:center; justify-content:center;}
"""
app.index_string = app.index_string.replace("{%css%}", "{%css%}<style>" + APP_CSS + "</style>")


money = lambda v: f"${v:,.0f}"
money2 = lambda v: f"${v:,.2f}"
num = lambda v: f"{v:,.0f}"
pct = lambda v: f"{v:.1%}"
EMPTY = pd.Series({"orders": 0, "demand": 0.0, "shipping": 0.0, "cogs": 0.0, "cancelled": 0})


def pct_change(ty: float, ly: float):
    if not ly:
        return None
    return (ty - ly) / ly


def hour_label(hr: int) -> str:
    h12 = 12 if hr % 12 == 0 else hr % 12
    suffix = "AM" if hr < 12 else "PM"
    return f"{h12}:00:00 {suffix}"


def card(title, ty, ly, fmt):
    delta = (ty - ly) / ly if ly else None
    color = "success" if (delta or 0) >= 0 else "danger"
    return [html.Div(title, className="text-muted small"), html.H4(fmt(ty)), html.Div(f"LY {fmt(ly)}", className="small"), dbc.Badge(f"{delta:+.1%}" if delta is not None else "n/a", color=color)]


def dr_card(title: str, val: str, delta_text: str, positive: bool = True):
    delta_class = "kpi-delta-positive" if positive else "kpi-delta-negative"
    return [
        html.Div(title, className="kpi-title"),
        html.Div(val, className="kpi-value"),
        html.Div(delta_text, className=f"kpi-sub {delta_class}")
    ]




def demand_filter_values() -> tuple[list[str], list[str], list[str]]:
    d = query_safe(f"SELECT * FROM {DEMAND_FILTERS_V}")
    if d.empty:
        return BANNERS, [], []
    cols = list(d.columns)
    banner_col = find_col(cols, ["BANNER"])
    cancel_col = find_col(cols, ["CANCEL_CODE", "CANCEL"])
    source_col = find_col(cols, ["SOURCE_CODE", "SOURCE"])
    banners = sorted([str(v) for v in d[banner_col].dropna().unique()]) if banner_col else BANNERS
    cancels = sorted([str(v) for v in d[cancel_col].dropna().unique()]) if cancel_col else []
    sources = sorted([str(v) for v in d[source_col].dropna().unique()]) if source_col else []
    return banners or BANNERS, cancels, sources


def format_banner_channel_table(df: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """Format RT_DEMAND_BANNER_CHANNEL_V data with Channel vs Banner comparison."""
    if df.empty:
        return [], []
    
    # Get column names
    cols = list(df.columns)
    banner_col = find_col(cols, ["BANNER"])
    channel_col = find_col(cols, ["CHANNEL"])
    order_col = find_col(cols, ["ORDERS"])
    demand_col = find_col(cols, ["DEMAND"])
    cogs_col = find_col(cols, ["COGS"])
    
    if not (banner_col and channel_col and order_col and demand_col):
        return [], []
    
    # Group by banner and channel
    pivot_data = {}
    for _, row in df.iterrows():
        banner = str(row[banner_col])
        channel = str(row[channel_col])
        
        if banner not in pivot_data:
            pivot_data[banner] = {}
        
        orders = float(row[order_col]) if pd.notna(row[order_col]) else 0.0
        demand = float(row[demand_col]) if pd.notna(row[demand_col]) else 0.0
        cogs = float(row[cogs_col]) if pd.notna(row[cogs_col]) else 0.0
        
        pivot_data[banner][channel] = {
            "orders": orders,
            "demand": demand,
            "cogs": cogs,
            "aov": demand / orders if orders > 0 else 0.0,
            "gm": (cogs / demand * 100) if demand > 0 else 0.0,
        }
    
    # Build table rows
    rows = []
    all_channels = set()
    for banner_data in pivot_data.values():
        all_channels.update(banner_data.keys())
    all_channels = sorted(list(all_channels))
    
    for banner in sorted(pivot_data.keys()):
        row = {"Banner": banner}
        total_orders = 0
        total_demand = 0.0
        
        for channel in all_channels:
            if channel in pivot_data[banner]:
                data = pivot_data[banner][channel]
                row[f"{channel}_Count"] = f"{int(data['orders']):,}"
                row[f"{channel}_AOV"] = f"${data['aov']:,.2f}"
                row[f"{channel}_GM"] = f"{data['gm']:.2f}%"
                row[f"{channel}_Amount"] = f"${data['demand']:,.2f}"
                total_orders += data['orders']
                total_demand += data['demand']
            else:
                row[f"{channel}_Count"] = "0"
                row[f"{channel}_AOV"] = "$0.00"
                row[f"{channel}_GM"] = "0.00%"
                row[f"{channel}_Amount"] = "$0.00"
        
        row["Total_Count"] = f"{int(total_orders):,}"
        row["Total_Amount"] = f"${total_demand:,.2f}"
        rows.append(row)
    
    # Create columns
    columns = [{"name": "Banner", "id": "Banner"}]
    for channel in all_channels:
        columns.extend([
            {"name": [channel, "Count"], "id": f"{channel}_Count"},
            {"name": [channel, "AOV"], "id": f"{channel}_AOV"},
            {"name": [channel, "GM"], "id": f"{channel}_GM"},
            {"name": [channel, "Amount"], "id": f"{channel}_Amount"},
        ])
    columns.extend([
        {"name": ["Total", "Count"], "id": "Total_Count"},
        {"name": ["Total", "Amount"], "id": "Total_Amount"},
    ])
    
    return columns, rows


def format_sku_table(df: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """Format RT_DEMAND_SKU_V data with SKU details."""
    if df.empty:
        return [], []
    
    cols = list(df.columns)
    sku_col = find_col(cols, ["SKU"])
    desc_col = find_col(cols, ["DESCRIPTION"])
    units_col = find_col(cols, ["UNITS"])
    # Try different column names for revenue
    revenue_col = find_col(cols, ["REVENUE", "DEMAND", "DEMAND_WITH_COST"])
    cogs_col = find_col(cols, ["COGS"])
    list_price_col = find_col(cols, ["LIST_AMOUNT"])
    
    rows = []
    for _, row in df.iterrows():
        sku = str(row[sku_col]) if sku_col and pd.notna(row[sku_col]) else ""
        desc = str(row[desc_col]) if desc_col and pd.notna(row[desc_col]) else ""
        units = int(row[units_col]) if units_col and pd.notna(row[units_col]) else 0
        revenue = float(row[revenue_col]) if revenue_col and pd.notna(row[revenue_col]) else 0.0
        cogs = float(row[cogs_col]) if cogs_col and pd.notna(row[cogs_col]) else 0.0
        list_price = float(row[list_price_col]) if list_price_col and pd.notna(row[list_price_col]) else 0.0
        
        # GM% = COGS / Revenue (displayed as percentage)
        gm_pct = (cogs / revenue * 100) if revenue > 0 else 0.0
        # % Off = (List Price - Revenue) / List Price (only if list_price exists)
        pct_off = ((list_price - revenue) / list_price * 100) if list_price > 0 else 0.0
        
        rows.append({
            "SKU": sku,
            "Description": desc,
            "Units": f"{units:,}",
            "Rev": f"${revenue:,.2f}",
            "GM": f"{gm_pct:.2f}%",
            "% Off": f"{pct_off:.2f}%",
        })
    
    columns = [
        {"name": "SKU", "id": "SKU"},
        {"name": "Description", "id": "Description"},
        {"name": "Units", "id": "Units"},
        {"name": "Rev", "id": "Rev"},
        {"name": "GM", "id": "GM"},
        {"name": "% Off", "id": "% Off"},
    ]
    
    return columns, rows


def format_order_detail_table(df: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """Format RT_DEMAND_ORDER_DETAIL_V data with order line details."""
    if df.empty:
        return [], []
    
    cols = list(df.columns)
    company_col = find_col(cols, ["COMPANY_NUMBER", "COMPANY"])
    order_col = find_col(cols, ["ORDER_ID", "ORDER_NUMBER"])
    source_col = find_col(cols, ["SOURCE_CODE", "SOURCE"])
    sku_col = find_col(cols, ["SKU"])
    size_col = find_col(cols, ["SIZE"])
    qty_col = find_col(cols, ["QUANTITY_ORDERED", "QUANTITY", "QTY_ORDERED", "QTY"])
    # Try multiple price column names
    list_price_col = find_col(cols, ["UNIT_LIST_PRICE", "LIST_PRICE", "UNIT_PRICE", "PRICE"])
    net_price_col = find_col(cols, ["UNIT_NET_PRICE", "NET_PRICE", "UNIT_NET_AMOUNT"])
    
    rows = []
    for _, row in df.iterrows():
        company = str(row[company_col]) if company_col and pd.notna(row[company_col]) else ""
        order_id = str(row[order_col]) if order_col and pd.notna(row[order_col]) else ""
        source = str(row[source_col]) if source_col and pd.notna(row[source_col]) else ""
        sku = str(row[sku_col]) if sku_col and pd.notna(row[sku_col]) else ""
        size = str(row[size_col]) if size_col and pd.notna(row[size_col]) else ""
        qty = int(row[qty_col]) if qty_col and pd.notna(row[qty_col]) else 0
        list_price = float(row[list_price_col]) if list_price_col and pd.notna(row[list_price_col]) else 0.0
        net_price = float(row[net_price_col]) if net_price_col and pd.notna(row[net_price_col]) else 0.0
        
        # Handle negative prices by taking absolute value
        list_price = abs(list_price)
        net_price = abs(net_price)
        
        rows.append({
            "Company": company,
            "Order Number": order_id,
            "Source Code": source,
            "SKU": sku,
            "Size": size,
            "Qty Ordered": f"{qty:,}" if qty > 0 else "0",
            "Unit List Price": f"${list_price:,.2f}",
            "Unit Net Price": f"${net_price:,.2f}",
        })
    
    columns = [
        {"name": "Company", "id": "Company"},
        {"name": "Order Number", "id": "Order Number"},
        {"name": "Source Code", "id": "Source Code"},
        {"name": "SKU", "id": "SKU"},
        {"name": "Size", "id": "Size"},
        {"name": "Qty Ordered", "id": "Qty Ordered"},
        {"name": "Unit List Price", "id": "Unit List Price"},
        {"name": "Unit Net Price", "id": "Unit Net Price"},
    ]
    
    return columns, rows


def demand_kpis(start_date, end_date, banners, cancel_codes, source_codes):
    kpi_source = DEMAND_KPI_V
    cols = get_view_columns(kpi_source)
    if not cols:
        kpi_source = DEMAND_ORDER_V
        cols = get_view_columns(kpi_source)

    date_col = find_col(cols, ["ORDER_DATE", "ORDER_DATE_AGG", "BUSINESS_DATE", "DATE", "DT"])
    banner_col = find_col(cols, ["BANNER"])
    order_col = find_col(cols, ["ORDER_ID", "ORDER_NUMBER", "ORDER_NBR", "ORDERNUM"])
    demand_col = find_col(cols, ["DEMAND_REVENUE", "DEMAND", "REV", "AMOUNT", "NET_SALES", "SALES"])
    cogs_col = find_col(cols, ["COGS", "COST"])

    if (not order_col or not demand_col) and kpi_source != DEMAND_ORDER_V:
        kpi_source = DEMAND_ORDER_V
        cols = get_view_columns(kpi_source)
        date_col = find_col(cols, ["ORDER_DATE", "ORDER_DATE_AGG", "BUSINESS_DATE", "DATE", "DT"])
        banner_col = find_col(cols, ["BANNER"])
        order_col = find_col(cols, ["ORDER_ID", "ORDER_NUMBER", "ORDER_NBR", "ORDERNUM"])
        demand_col = find_col(cols, ["DEMAND_REVENUE", "DEMAND", "REV", "AMOUNT", "NET_SALES", "SALES"])
        cogs_col = find_col(cols, ["COGS", "COST"])

    if not order_col or not demand_col:
        return 0.0, 0, 0.0, 0.0, 0, 0.0, 0, 0.0, 0.0

    where_sql, params = demand_where(kpi_source, start_date, end_date, banners, cancel_codes, source_codes)
    sql_now = f"""
        SELECT
            SUM(CAST({demand_col} AS DOUBLE)) AS demand,
            COUNT(DISTINCT {order_col}) AS orders,
            {f"SUM(CAST({cogs_col} AS DOUBLE))" if cogs_col else "NULL"} AS cogs,
            {f"COUNT(DISTINCT {banner_col})" if banner_col else "0"} AS banners
        FROM {kpi_source}
        WHERE {where_sql}
    """
    now_df = query_safe(sql_now, params)
    demand_now = float(now_df["demand"].iloc[0]) if not now_df.empty and pd.notna(now_df["demand"].iloc[0]) else 0.0
    orders_now = int(now_df["orders"].iloc[0]) if not now_df.empty and pd.notna(now_df["orders"].iloc[0]) else 0
    cogs_now = float(now_df["cogs"].iloc[0]) if (not now_df.empty and "cogs" in now_df.columns and pd.notna(now_df["cogs"].iloc[0])) else 0.0
    banners_now = int(now_df["banners"].iloc[0]) if (not now_df.empty and "banners" in now_df.columns and pd.notna(now_df["banners"].iloc[0])) else 0
    aov_now = (demand_now / orders_now) if orders_now else 0.0
    gm_now = ((demand_now - cogs_now) / demand_now * 100.0) if demand_now else 0.0

    if not date_col or not start_date or not end_date:
        return demand_now, orders_now, aov_now, gm_now, banners_now, 0.0, 0, 0.0, 0.0

    start_ly = (pd.to_datetime(start_date) - timedelta(days=364)).date().isoformat()
    end_ly = (pd.to_datetime(end_date) - timedelta(days=364)).date().isoformat()
    where_ly, params_ly = demand_where(DEMAND_ORDER_V, start_ly, end_ly, banners, cancel_codes, source_codes)
    ly_df = query_safe(sql_now.replace(where_sql, where_ly), params_ly)
    demand_ly = float(ly_df["demand"].iloc[0]) if not ly_df.empty and pd.notna(ly_df["demand"].iloc[0]) else 0.0
    orders_ly = int(ly_df["orders"].iloc[0]) if not ly_df.empty and pd.notna(ly_df["orders"].iloc[0]) else 0
    cogs_ly = float(ly_df["cogs"].iloc[0]) if (not ly_df.empty and "cogs" in ly_df.columns and pd.notna(ly_df["cogs"].iloc[0])) else 0.0
    aov_ly = (demand_ly / orders_ly) if orders_ly else 0.0
    gm_ly = ((demand_ly - cogs_ly) / demand_ly * 100.0) if demand_ly else 0.0
    return demand_now, orders_now, aov_now, gm_now, banners_now, demand_ly, orders_ly, aov_ly, gm_ly


def order_timing_layout():
    return html.Div([
        dbc.Row([
            dbc.Col([html.Div("Banner", className="filters-label"), dcc.Dropdown(BANNERS, multi=True, id="ot-banner", placeholder="Banner (all)")], md=4),
            dbc.Col([html.Div("Fulfillment Type", className="filters-label"), dcc.Dropdown(FTYPES, multi=True, id="ot-ftype", placeholder="Fulfillment type (all)")], md=4),
            dbc.Col([html.Div("Date", className="filters-label"), dcc.DatePickerSingle(id="ot-day", date=default_day(), display_format="YYYY-MM-DD")], md=2),
        ], className="g-2 mb-3"),
        html.Div(id="ot-asof", className="subtle mb-2"),
        dbc.Row([
            dbc.Col(dbc.Card(dbc.CardBody(id="ot-k_orders"), className="shadow-sm text-center"), md=2),
            dbc.Col(dbc.Card(dbc.CardBody(id="ot-k_demand"), className="shadow-sm text-center"), md=2),
            dbc.Col(dbc.Card(dbc.CardBody(id="ot-k_aov"), className="shadow-sm text-center"), md=2),
            dbc.Col(dbc.Card(dbc.CardBody(id="ot-k_ship"), className="shadow-sm text-center"), md=2),
            dbc.Col(dbc.Card(dbc.CardBody(id="ot-k_margin"), className="shadow-sm text-center"), md=2),
            dbc.Col(dbc.Card(dbc.CardBody(id="ot-k_cancel"), className="shadow-sm text-center"), md=2),
        ], className="g-2 mb-3"),
        dbc.Tabs(id="ot-metric-tab", active_tab="demand", className="metric-tab mb-2", children=[dbc.Tab(label=v["label"], tab_id=k) for k, v in METRICS.items()]),
        html.Div(id="ot-chip-title", className="chip"),
        dbc.Card(dbc.CardBody(dcc.Graph(id="ot-g-hour-metric", config={"displayModeBar": False})), className="tile mb-3"),
        html.Div("Comp", className="chip"),
        dbc.Card(dbc.CardBody(dash_table.DataTable(id="ot-comp-table", page_action="none", style_table={"overflowX": "auto", "maxHeight": "420px", "overflowY": "auto"}, style_cell={"fontFamily": "Segoe UI, Arial", "fontSize": "12px", "padding": "6px", "whiteSpace": "nowrap", "textAlign": "right"}, style_header={"fontWeight": "700", "backgroundColor": "#F3F8FF", "color": "#223A5E", "textAlign": "center"}, merge_duplicate_headers=True, fixed_rows={"headers": True})), className="tile"),
        dcc.Interval(id="ot-tick", interval=5 * 60 * 1000),
    ])


def demand_review_layout():
    start_default, end_default = default_demand_range()
    return html.Div([
        dbc.Row([
            dbc.Col(html.Div("Demand Review", style={"fontSize": "24px", "fontWeight": "700", "color": "#0F1B2D"}), md=8),
            dbc.Col(html.Div("# Excluded Fraud", className="excluded-fraud-label"), md=4),
        ], className="mb-2"),
        html.Div(id="dr-last-refresh", className="subtle mb-3"),
        dbc.Row([
            dbc.Col([html.Div("Banner", className="filters-label"), dcc.Dropdown(id="dr-banner", multi=True, placeholder="All")], md=4),
            dbc.Col([html.Div("Cancel Code", className="filters-label"), dcc.Dropdown(id="dr-cancel", multi=True, placeholder="All")], md=2),
            dbc.Col([html.Div("Source Code", className="filters-label"), dcc.Dropdown(id="dr-source", multi=True, placeholder="All")], md=2),
            dbc.Col([html.Div("Date", className="filters-label"), dcc.DatePickerRange(id="dr-date-range", start_date=start_default, end_date=end_default, display_format="YYYY-MM-DD")], md=4),
        ], className="g-2 mb-4"),
        dbc.Row([
            dbc.Col(dbc.Card(dbc.CardBody(id="dr-k-total-demand", className="dr-card-body"), className="tile"), md=2),
            dbc.Col(dbc.Card(dbc.CardBody(id="dr-k-orders", className="dr-card-body"), className="tile"), md=2),
            dbc.Col(dbc.Card(dbc.CardBody(id="dr-k-aov", className="dr-card-body"), className="tile"), md=2),
            dbc.Col(dbc.Card(dbc.CardBody(id="dr-k-gm", className="dr-card-body"), className="tile"), md=2),
            dbc.Col(dbc.Card(dbc.CardBody(id="dr-k-banners", className="dr-card-body"), className="tile"), md=2),
        ], className="g-2 mb-4"),
        dbc.Card(dbc.CardBody([html.H4("Demand by Channel", className="mb-3", style={"fontSize": "16px", "fontWeight": "700"}), dash_table.DataTable(id="dr-banner-channel-table", page_action="none", style_table={"overflowX": "auto", "maxHeight": "360px", "overflowY": "auto"}, style_cell={"fontFamily": "Segoe UI, Arial", "fontSize": "12px", "padding": "8px", "whiteSpace": "nowrap", "textAlign": "right"}, style_cell_conditional=[{"if": {"column_id": "Banner"}, "textAlign": "left"}], style_header={"fontWeight": "700", "backgroundColor": "#F3F8FF", "color": "#223A5E", "textAlign": "center", "borderRight": "1px solid #E6ECF5"}, style_data_conditional=[{"if": {"row_index": "odd"}, "backgroundColor": "#fafbfc"}])]), className="tile mb-4"),
        dbc.Card(dbc.CardBody([html.H4("SKU Detail", className="mb-3", style={"fontSize": "16px", "fontWeight": "700"}), dash_table.DataTable(id="dr-sku-table", page_action="none", style_table={"overflowX": "auto", "maxHeight": "360px", "overflowY": "auto"}, style_cell={"fontFamily": "Segoe UI, Arial", "fontSize": "12px", "padding": "8px", "whiteSpace": "nowrap", "textAlign": "right"}, style_cell_conditional=[{"if": {"column_id": c}, "textAlign": "left"} for c in ["SKU", "Description"]], style_header={"fontWeight": "700", "backgroundColor": "#F3F8FF", "color": "#223A5E", "textAlign": "center"}, style_data_conditional=[{"if": {"row_index": "odd"}, "backgroundColor": "#fafbfc"}])]), className="tile mb-4"),
        dbc.Card(dbc.CardBody([html.H4("Order Detail", className="mb-3", style={"fontSize": "16px", "fontWeight": "700"}), dash_table.DataTable(id="dr-order-table", page_size=10, style_table={"overflowX": "auto"}, style_cell={"fontFamily": "Segoe UI, Arial", "fontSize": "12px", "padding": "8px", "whiteSpace": "nowrap", "textAlign": "right"}, style_cell_conditional=[{"if": {"column_id": c}, "textAlign": "left"} for c in ["Order Number", "Source Code", "SKU", "Description"]], style_header={"fontWeight": "700", "backgroundColor": "#F3F8FF", "color": "#223A5E", "textAlign": "center"})]), className="tile mb-3"),
        dcc.Interval(id="dr-tick", interval=5 * 60 * 1000),
    ])


app.layout = html.Div(className="app-shell", children=[dbc.Container(fluid=True, children=[
    html.Div("Ecom NOW", className="top-title"),
    dbc.Tabs(id="main-tab", active_tab="demand_review", className="main-tab mb-3", children=[dbc.Tab(label="Demand Review", tab_id="demand_review"), dbc.Tab(label="Order Timing", tab_id="order_timing")]),
    html.Div(id="main-tab-content"),
])])


@app.callback(Output("main-tab-content", "children"), Input("main-tab", "active_tab"))
def render_main_tab(active_tab):
    return order_timing_layout() if active_tab == "order_timing" else demand_review_layout()


@app.callback([Output("dr-banner", "options"), Output("dr-banner", "value"), Output("dr-cancel", "options"), Output("dr-source", "options")], Input("main-tab", "active_tab"))
def init_demand_filters(active_tab):
    if active_tab != "demand_review":
        return [], [], [], []
    banners, cancels, sources = demand_filter_values()
    return banners, banners, cancels, sources


@app.callback([
    Output("dr-last-refresh", "children"), Output("dr-k-total-demand", "children"), Output("dr-k-orders", "children"), Output("dr-k-aov", "children"), Output("dr-k-gm", "children"), Output("dr-k-banners", "children"), Output("dr-banner-channel-table", "columns"), Output("dr-banner-channel-table", "data"), Output("dr-sku-table", "columns"), Output("dr-sku-table", "data"), Output("dr-order-table", "columns"), Output("dr-order-table", "data")
], [Input("dr-date-range", "start_date"), Input("dr-date-range", "end_date"), Input("dr-banner", "value"), Input("dr-cancel", "value"), Input("dr-source", "value"), Input("dr-tick", "n_intervals")])
def refresh_demand_review(start_date, end_date, banners, cancel_codes, source_codes, _):
    demand_now, orders_now, aov_now, gm_now, banners_now, demand_ly, orders_ly, aov_ly, gm_ly = demand_kpis(start_date, end_date, banners or None, cancel_codes or None, source_codes or None)
    demand_delta = pct_change(demand_now, demand_ly)
    orders_delta = pct_change(orders_now, orders_ly)
    aov_delta = pct_change(aov_now, aov_ly)
    gm_delta_pp = gm_now - gm_ly
    
    # Fetch and format tables
    channel_df = build_filtered_view(DEMAND_BANNER_CHANNEL_V, start_date, end_date, banners, cancel_codes, source_codes, limit_rows=500)
    sku_df = build_filtered_view(DEMAND_SKU_V, start_date, end_date, banners, cancel_codes, source_codes, limit_rows=300)
    order_df = build_filtered_view(DEMAND_ORDER_DETAIL_V, start_date, end_date, banners, cancel_codes, source_codes, limit_rows=500)
    
    channel_cols, channel_data = format_banner_channel_table(channel_df)
    sku_cols, sku_data = format_sku_table(sku_df)
    order_cols, order_data = format_order_detail_table(order_df)
    
    return (
        f"Last refresh: {pd.Timestamp.utcnow().strftime('%m/%d/%Y %H:%M UTC')}",
        dr_card("TOTAL DEMAND", money(demand_now), f"▲ {demand_delta:+.1%} vs LY" if demand_delta is not None else "n/a", (demand_delta or 0) >= 0),
        dr_card("ORDERS", num(orders_now), f"▲ {orders_delta:+.1%} vs LY" if orders_delta is not None else "n/a", (orders_delta or 0) >= 0),
        dr_card("AOV", money2(aov_now), f"▲ {aov_delta:+.1%} vs LY" if aov_delta is not None else "n/a", (aov_delta or 0) >= 0),
        dr_card("GM%", f"{gm_now:.1f}%", f"{'▲' if gm_delta_pp >= 0 else '▼'} {gm_delta_pp:+.1f}pp vs LY", gm_delta_pp >= 0),
        dr_card("BANNERS", num(banners_now), "▲ Active" if banners_now else "n/a", banners_now > 0),
        channel_cols, channel_data, sku_cols, sku_data, order_cols, order_data
    )


@app.callback([
    Output("ot-k_orders", "children"), Output("ot-k_demand", "children"), Output("ot-k_aov", "children"), Output("ot-k_ship", "children"), Output("ot-k_margin", "children"), Output("ot-k_cancel", "children"), Output("ot-asof", "children"), Output("ot-chip-title", "children"), Output("ot-g-hour-metric", "figure"), Output("ot-comp-table", "columns"), Output("ot-comp-table", "data")
], [Input("ot-day", "date"), Input("ot-banner", "value"), Input("ot-ftype", "value"), Input("ot-metric-tab", "active_tab"), Input("ot-tick", "n_intervals")])
def refresh_order_timing(day, banners, ftypes, metric_tab, _):
    day = day or str(default_day())
    banners = banners or BANNERS
    ftypes = ftypes or FTYPES
    w, p = where_clause(day, banners, ftypes)
    kdf = query_safe(both_periods(KPI_SELECT, w), p)
    k = kdf.set_index("period").fillna(0) if "period" in kdf.columns else pd.DataFrame()
    t = k.loc["TY"].astype(float) if "TY" in k.index else EMPTY
    l = k.loc["LY"].astype(float) if "LY" in k.index else EMPTY
    aov = lambda r: r.demand / r.orders if r.orders else 0
    mgn = lambda r: (r.demand - r.cogs) / r.demand if r.demand else 0
    canc = lambda r: r.cancelled / r.orders if r.orders else 0
    asof_df = query_safe(f"SELECT CAST(MAX(CURRENTDATEEST) AS STRING) AS d FROM {CUR}")
    asof = asof_df["d"].iloc[0] if "d" in asof_df.columns and not asof_df.empty else "n/a"

    hr = query_safe(both_periods(f"{HOUR_EXPR} AS hr, CAST(SUM(DEMAND_REVENUE) AS DOUBLE) AS demand, COUNT(DISTINCT ORDER_ID) AS orders, CAST(SUM(SHIPPING_REVENUE) AS DOUBLE) AS shipping", w, HOUR_EXPR), p)
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
    for per, color in (("LY", "#1E88E5"), ("TY", "#EA6B35")):
        d = hr[hr.period == per][["hr", metric_col]].copy()
        d = hrs.merge(d, on="hr", how="left").fillna({metric_col: 0})
        fig_hour.add_bar(x=hour_categories, y=d[metric_col], name=f"{METRICS[metric_key]['label']} {per}", marker_color=color)
    fig_hour.update_layout(title=metric_title, barmode="group", template="plotly_white")

    comp = query_safe(both_periods(f"{HOUR_EXPR} AS hr, BANNER, CAST(SUM(DEMAND_REVENUE) AS DOUBLE) AS demand, CAST(SUM(SHIPPING_REVENUE) AS DOUBLE) AS shipping, COUNT(DISTINCT ORDER_ID) AS orders, CAST(SUM(COGS) AS DOUBLE) AS cogs", w, f"{HOUR_EXPR}, BANNER"), p).fillna(0)
    for c, default in (("period", ""), ("hr", 0), ("BANNER", ""), ("demand", 0.0), ("orders", 0.0), ("shipping", 0.0), ("cogs", 0.0)):
        if c not in comp.columns:
            comp[c] = default

    columns = [{"name": ["", "Order Hour"], "id": "order_hour"}]
    metric_fmt = METRICS[metric_key]["fmt"]
    metric_label = METRICS[metric_key]["label"]
    for b in banners:
        columns.extend([{"name": [b, f"{metric_label} TY"], "id": f"{b}__ty"}, {"name": [b, f"{metric_label} LY"], "id": f"{b}__ly"}, {"name": [b, "% Change"], "id": f"{b}__chg"}])

    table_data = []
    for hr_i in range(24):
        row = {"order_hour": hour_label(hr_i)}
        for b in banners:
            ty = comp[(comp["period"] == "TY") & (comp["hr"] == hr_i) & (comp["BANNER"] == b)]
            ly = comp[(comp["period"] == "LY") & (comp["hr"] == hr_i) & (comp["BANNER"] == b)]
            ty_val = float(ty[metric_col].sum())
            ly_val = float(ly[metric_col].sum())
            row[f"{b}__ty"] = metric_fmt(ty_val)
            row[f"{b}__ly"] = metric_fmt(ly_val)
            row[f"{b}__chg"] = pct(pct_change(ty_val, ly_val)) if ly_val else "n/a"
        table_data.append(row)

    return (
        card("Orders", t.orders, l.orders, num), card("Demand $", t.demand, l.demand, money), card("AOV", aov(t), aov(l), money2),
        card("Shipping $", t.shipping, l.shipping, money), card("Margin %", mgn(t), mgn(l), pct), card("Cancel rate", canc(t), canc(l), pct),
        f"Order date {day} | Current date (EST) {asof}", metric_title, fig_hour, columns, table_data
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("DATABRICKS_APP_PORT", "8000")), debug=False)
