# coordinate_viewer.py
# 座標檢視器 - 上傳多個 CSV，合併或分頁顯示散點圖
# 圖表與表格雙向連動：點圖上的點或點表格的列，都會鎖定該點、放大置中並標示。
# 執行方式: streamlit run coordinate_viewer.py

import io
import math
import re
import time

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="座標檢視器", layout="wide")

# 點擊一個點後自動放大到的倍率（讓鎖定點清楚可辨識）
FOCUS_ZOOM = 8.0

# 啟用滑鼠滾輪縮放（以游標位置為中心，Plotly 原生）
PLOTLY_CFG = {"scrollZoom": True}

def make_palette(n):
    """產生 n 個唯一、高對比、且避開紅色的顏色。
    用黃金角分散色相（相鄰區域差異大），交替明暗增加對比。"""
    import colorsys
    n = max(int(n), 1)
    cols = []
    for i in range(n):
        h = (i * 0.6180339887) % 1.0
        h = 0.06 + h * 0.86            # 壓到 [0.06, 0.92]，避開紅色(hue≈0/1)
        s = 0.55 if (i % 3 == 2) else 0.78
        v = 0.97 if (i % 2 == 0) else 0.74   # 交替明暗
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        cols.append("#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255)))
    return cols


# 後備調色盤（已移除紅色，改白色）
COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#ffffff", "#9467bd",
    "#8c564b", "#e377c2", "#17becf", "#bcbd22", "#7f7f7f",
]


def find_xy_columns(df):
    """猜測 x / y 欄位：先找名稱，再退而用前兩個數值欄。"""
    lower = {str(c).strip().lower(): c for c in df.columns}
    x_col = y_col = None
    for key in ("x", "x座標", "x_coord", "px", "pixel_x"):
        if key in lower:
            x_col = lower[key]
            break
    for key in ("y", "y座標", "y_coord", "py", "pixel_y"):
        if key in lower:
            y_col = lower[key]
            break
    if x_col is None or y_col is None:
        numeric = df.select_dtypes(include="number").columns.tolist()
        if x_col is None and numeric:
            x_col = numeric[0]
        if y_col is None:
            for c in numeric:
                if c != x_col:
                    y_col = c
                    break
    return x_col, y_col


def read_csv(uploaded_file):
    """讀取 CSV，嘗試常見編碼。"""
    raw = uploaded_file.getvalue()
    for enc in ("utf-8-sig", "utf-8", "big5", "cp950", "gbk", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(raw), encoding=enc)
        except (UnicodeDecodeError, pd.errors.ParserError):
            continue
    return pd.read_csv(io.BytesIO(raw), encoding="latin-1", engine="python")


def square_range(frames, xcols, ycols):
    """算出對稱、含原點的方形軸範圍。"""
    vals = []
    for df, xc, yc in zip(frames, xcols, ycols):
        if xc is not None:
            vals.extend(pd.to_numeric(df[xc], errors="coerce").dropna().tolist())
        if yc is not None:
            vals.extend(pd.to_numeric(df[yc], errors="coerce").dropna().tolist())
    vals.append(0.0)
    m = max(abs(min(vals)), abs(max(vals)))
    if m == 0:
        m = 1.0
    pad = m * 0.05
    return [-(m + pad), m + pad]


def add_origin(fig, rng):
    """在 (0,0) 畫綠色十字 + 原點標記。"""
    fig.add_shape(type="line", x0=rng[0], x1=rng[1], y0=0, y1=0,
                  line=dict(color="green", width=1, dash="dot"))
    fig.add_shape(type="line", x0=0, x1=0, y0=rng[0], y1=rng[1],
                  line=dict(color="green", width=1, dash="dot"))
    fig.add_trace(go.Scatter(
        x=[0], y=[0], mode="markers",
        marker=dict(color="green", size=12, symbol="cross"),
        name="原點 (0,0)", hoverinfo="name",
    ))


def _xform(x, y, flip=False, rot_deg=0.0):
    """翻轉(背面=X 鏡像) + 繞原點逆時針旋轉。x,y 可為純量或 pandas Series。"""
    if flip:
        x = -x
    th = math.radians(rot_deg)
    c, s = math.cos(th), math.sin(th)
    return x * c - y * s, x * s + y * c


def _row_xy(loaded, name, idx):
    """回傳某區域第 idx 列的原始 (x, y)（未轉換）。"""
    for n, df, xc, yc, color in loaded:
        if n == name and xc is not None and yc is not None \
                and 0 <= idx < len(df):
            x = pd.to_numeric(df[xc], errors="coerce").iloc[idx]
            y = pd.to_numeric(df[yc], errors="coerce").iloc[idx]
            if pd.notna(x) and pd.notna(y):
                return float(x), float(y)
    return None


def make_figure(datasets, title, center=None, zoom=1.0, visible=None,
                marked=None, mark_label=None, flip=False, rot_deg=0.0):
    """datasets: list of (name, df, xcol, ycol, color)
    center/marked 已是「顯示座標」(翻轉/旋轉後)。
    flip: 翻轉到背面(X 鏡像)；rot_deg: 逆時針旋轉度數（繞原點）。"""
    fig = go.Figure()
    series = []
    allv = [0.0]
    for name, df, xc, yc, color in datasets:
        if xc is None or yc is None:
            series.append((name, None, None, color))
            continue
        x = pd.to_numeric(df[xc], errors="coerce")
        y = pd.to_numeric(df[yc], errors="coerce")
        tx, ty = _xform(x, y, flip, rot_deg)
        series.append((name, tx, ty, color))
        allv += tx.dropna().tolist()
        allv += ty.dropna().tolist()
    m = max(abs(min(allv)), abs(max(allv))) or 1.0
    rng = [-(m * 1.05), m * 1.05]
    add_origin(fig, rng)

    full_half = (rng[1] - rng[0]) / 2.0
    half = full_half / max(zoom, 1e-9)
    if center is not None and zoom > 1.0:
        cx, cy = center
    else:
        cx, cy = 0.0, 0.0
    xr = [cx - half, cx + half]
    yr = [cy - half, cy + half]

    for name, tx, ty, color in series:
        if tx is None:
            continue
        idx = list(range(len(tx)))
        vis = True
        if visible is not None and not visible.get(name, True):
            vis = "legendonly"
        fig.add_trace(go.Scatter(
            x=tx, y=ty, mode="markers", name=name,
            marker=dict(color=color, size=8),
            customdata=idx, visible=vis,
            hovertemplate=(
                f"<b>{name}</b><br>列 %{{customdata}}"
                "<br>x=%{x}<br>y=%{y}<extra></extra>"
            ),
        ))

    # 鎖定點：紅圈 + 文字標籤，放大後仍能辨識
    if marked is not None:
        fig.add_trace(go.Scatter(
            x=[marked[0]], y=[marked[1]], mode="markers",
            marker=dict(symbol="circle-open", size=24, color="red",
                        line=dict(color="red", width=3)),
            name="🔒 鎖定點", hoverinfo="skip", showlegend=True,
        ))
        if mark_label:
            fig.add_annotation(
                x=marked[0], y=marked[1], text=mark_label,
                showarrow=True, arrowhead=2, arrowcolor="red",
                ax=0, ay=-45, font=dict(color="red", size=13),
                bgcolor="rgba(255,255,255,0.75)", bordercolor="red",
            )

    fig.update_layout(
        title=title,
        legend=dict(itemclick="toggle", itemdoubleclick="toggleothers"),
        height=650,
        margin=dict(l=20, r=20, t=50, b=20),
        dragmode="pan",
    )
    fig.update_xaxes(range=xr, zeroline=False, title="X")
    fig.update_yaxes(range=yr, zeroline=False, title="Y",
                     scaleanchor="x", scaleratio=1)
    return fig


def selected_point_from_chart(state_key, datasets):
    """讀取圖表點擊事件 → 回傳 {name, df, idx, x, y}。"""
    sel = st.session_state.get(state_key)
    if not isinstance(sel, dict):
        return None
    pts = sel.get("selection", {}).get("points", [])
    if not pts:
        return None
    p = pts[-1]
    cn = p.get("curve_number")
    cd = p.get("customdata")
    if cn is None or cn < 1 or (cn - 1) >= len(datasets) or cd is None:
        return None
    x, y = p.get("x"), p.get("y")
    if x is None or y is None:
        return None
    name, df, xc, yc, color = datasets[cn - 1]
    return {"name": name, "df": df, "idx": int(cd), "x": x, "y": y}


def build_table(loaded, visible=None):
    """組成可點選的彙總表，並回傳 (DataFrame, meta)。
    meta[i] = (name, 列號)，對應表格第 i 列。"""
    parts, meta = [], []
    for name, df, xc, yc, color in loaded:
        if visible is not None and not visible.get(name, True):
            continue
        n = len(df)
        sub = pd.DataFrame({
            "檔案": name,
            "該區域加工順序": list(range(n)),
            "X": pd.to_numeric(df[xc], errors="coerce").to_numpy()
            if xc else [None] * n,
            "Y": pd.to_numeric(df[yc], errors="coerce").to_numpy()
            if yc else [None] * n,
        })
        parts.append(sub)
        meta.extend((name, i) for i in range(n))
    if parts:
        return pd.concat(parts, ignore_index=True), meta
    return pd.DataFrame(columns=["檔案", "該區域加工順序", "X", "Y"]), meta


def selected_row_from_table(state_key, meta, loaded):
    """讀取表格列點選事件 → 回傳 ({name, df, idx, x, y}, 列位置)。"""
    sel = st.session_state.get(state_key)
    if not isinstance(sel, dict):
        return None, None
    rows = sel.get("selection", {}).get("rows", [])
    if not rows or rows[0] >= len(meta):
        return None, None
    pos = rows[0]
    name, idx = meta[pos]
    for n, df, xc, yc, color in loaded:
        if n != name:
            continue
        if xc is None or yc is None:
            return None, None
        x = pd.to_numeric(df[xc], errors="coerce").iloc[idx]
        y = pd.to_numeric(df[yc], errors="coerce").iloc[idx]
        if pd.isna(x) or pd.isna(y):
            return None, None
        return {"name": name, "df": df, "idx": idx,
                "x": float(x), "y": float(y)}, pos
    return None, None


def resolve_lock(lock_key, zoom_level_key, sources):
    """sources: list of (source_id, det_or_None, signature)。
    回傳最近一次變動的來源所選的點；若都沒變則沿用上次的鎖定點。
    當鎖定點改變時，自動把放大倍率設為 FOCUS_ZOOM。"""
    sig_key = lock_key + "_sigs"
    prev = st.session_state.get(sig_key, {})
    chosen = st.session_state.get(lock_key)
    changed = False
    for sid, det, sig in sources:
        if det is not None and sig is not None and prev.get(sid) != sig:
            chosen = det
            changed = True
        prev[sid] = sig
    st.session_state[sig_key] = prev
    st.session_state[lock_key] = chosen
    if changed:
        st.session_state[zoom_level_key] = FOCUS_ZOOM
    return chosen


def zoom_controls(key, center):
    """只保留「⟲ 重置」按鈕；點圖上的點即自動以該點為中心放大。"""
    zk = f"{key}_level"
    if zk not in st.session_state:
        st.session_state[zk] = 1.0
    c_reset, c_info = st.columns([1, 7])
    if c_reset.button("⟲ 重置", key=f"{key}_reset", use_container_width=True,
                      help="回到原始大小（全部顯示）"):
        st.session_state[zk] = 1.0
    zoom = st.session_state[zk]
    if center is not None and zoom > 1.0:
        c_info.caption(f"放大 {zoom:.1f}×，鎖定點置中（紅圈）；按「⟲ 重置」回到全圖")
    elif center is not None:
        c_info.caption("已鎖定一點")
    else:
        c_info.caption("點圖上的點、或點下方表格的列即可鎖定並放大")
    return zoom


def lock_info_panel(det):
    """顯示鎖定點是哪個檔、哪一列、什麼數據。"""
    if det is None:
        st.info("尚未鎖定任何點。點圖上的點，或點下方表格的任一列。")
        return
    st.success(
        f"🔒 鎖定：{det['name']} · 第 {det['idx']} 列 · "
        f"x={det['x']:.6g}, y={det['y']:.6g}"
    )
    row = det["df"].iloc[[det["idx"]]].copy()
    row.insert(0, "該區域加工順序", det["idx"])
    st.dataframe(row, use_container_width=True, hide_index=True)


def build_hide_animation(loaded, interval_ms=250):
    """用 Plotly frames 做「依序隱藏」動畫：只渲染一次，瀏覽器端逐格播放，不閃。
    每 interval_ms 毫秒隱藏一個區域，依 loaded（DrillDataSet）順序。"""
    fig = go.Figure()
    rng = square_range([d[1] for d in loaded], [d[2] for d in loaded],
                       [d[3] for d in loaded])
    add_origin(fig, rng)  # trace 0 = 原點
    for name, df, xc, yc, color in loaded:
        if xc is None or yc is None:
            x, y = [], []
        else:
            x = pd.to_numeric(df[xc], errors="coerce")
            y = pd.to_numeric(df[yc], errors="coerce")
        fig.add_trace(go.Scatter(x=x, y=y, mode="markers", name=name,
                                 marker=dict(color=color, size=8)))
    total = len(loaded)
    frames = []
    for step in range(total + 1):
        # 原點永遠顯示；前 step 個區域隱藏
        vis = [True] + [(i >= step) for i in range(total)]
        frames.append(go.Frame(
            name=str(step),
            data=[go.Scatter(visible=v) for v in vis],
            traces=list(range(total + 1)),
        ))
    fig.frames = frames

    play = dict(label="▶ 播放", method="animate", args=[None, {
        "frame": {"duration": interval_ms, "redraw": True},
        "transition": {"duration": 0}, "fromcurrent": True}])
    pause = dict(label="⏸ 暫停", method="animate", args=[[None], {
        "frame": {"duration": 0, "redraw": False}, "mode": "immediate",
        "transition": {"duration": 0}}])
    reset = dict(label="⟲ 重置", method="animate", args=[["0"], {
        "frame": {"duration": 0, "redraw": True}, "mode": "immediate"}])

    fig.update_layout(
        title="依序隱藏動畫（每 0.25 秒隱藏一個，按 ▶ 播放）",
        height=650, margin=dict(l=20, r=20, t=70, b=20), showlegend=False,
        updatemenus=[dict(type="buttons", showactive=False, direction="left",
                          x=0.0, y=1.12, xanchor="left", yanchor="top",
                          buttons=[play, pause, reset])],
    )
    fig.update_xaxes(range=rng, zeroline=False, title="X")
    fig.update_yaxes(range=rng, zeroline=False, title="Y",
                     scaleanchor="x", scaleratio=1)
    return fig


def _parse_float(s, default=0.0):
    try:
        return float(str(s).strip())
    except Exception:  # noqa: BLE001
        return default


def parse_measurement_file(uploaded):
    """解析量測原始檔（UTF-16 / Tab 分隔、群組式排列）→ 整理成矩形資料表。
    每個矩形：X中心座標、Y中心座標、間距、高度，各有 量測值/CAD值/差值。"""
    import csv as _csv
    raw = uploaded.getvalue()
    text = None
    for enc in ("utf-16", "utf-16-le", "utf-8-sig", "utf-8", "big5", "cp950"):
        try:
            text = raw.decode(enc)
            break
        except Exception:  # noqa: BLE001
            continue
    if text is None:
        text = raw.decode("latin-1")
    rows = list(_csv.reader(io.StringIO(text), delimiter="\t"))

    def _num(s):
        try:
            return float(str(s).strip())
        except Exception:  # noqa: BLE001
            return None

    kw = ("中心座標", "半徑", "間距", "高度")
    order, data = [], {}
    for row in rows:
        i, n = 0, len(row)
        while i < n:
            cell = (row[i] or "").strip()
            if any(k in cell for k in kw) and ":" in cell and i + 5 < n:
                feat, item = cell.split(":", 1)
                feat, item = feat.strip(), item.strip()
                if feat not in data:
                    data[feat] = {}
                    order.append(feat)
                data[feat][item] = (_num(row[i + 1]), _num(row[i + 2]),
                                    _num(row[i + 5]))
                i += 6
            else:
                i += 1

    header = ["矩形編號",
              "X中心座標(量測值)", "X中心座標(CAD值)", "X中心座標(量測值-CAD值)",
              "Y中心座標(量測值)", "Y中心座標(CAD值)", "Y中心座標(量測值-CAD值)",
              "間距(量測值)", "間距(CAD值)", "間距(量測值-CAD值)",
              "高度(量測值)", "高度(CAD值)", "高度(量測值-CAD值)"]
    items = ["X 中心座標", "Y 中心座標", "間距", "高度"]
    out = []
    for feat in order:
        if not feat.startswith("矩形"):
            continue
        m = re.findall(r"\d+", feat)
        r = [int(m[0]) if m else feat]
        for it in items:
            v = data[feat].get(it)
            r += list(v) if v else [None, None, None]
        out.append(r)
    return pd.DataFrame(out, columns=header)


def render_cv_overlay(loaded, red_x, red_y, meas_x, meas_y):
    """第三張疊合圖：座標檢視器（各區域保有顏色與名稱）＋ 只標超規紅點。
    自動把量測座標系對映到座標檢視器座標系（各軸 bounding box 對齊），
    讓紅點落在對應區域；套用相同翻轉/旋轉；支援點擊放大與鎖定圈選。"""
    flip = st.session_state.get("cv_flip", False)
    rot = st.session_state.get("cv_rot", 0.0)

    # 座標檢視器整體 bounding box
    cvx, cvy = [], []
    for name, df, xc, yc, color in loaded:
        if xc is not None:
            cvx += pd.to_numeric(df[xc], errors="coerce").dropna().tolist()
        if yc is not None:
            cvy += pd.to_numeric(df[yc], errors="coerce").dropna().tolist()

    # 穩健邊界（2~98 百分位，避開突出的焊墊/雜點）
    def _rng(a):
        a = np.asarray(a, dtype=float)
        return float(np.percentile(a, 2)), float(np.percentile(a, 98))

    have = bool(cvx and cvy and meas_x and meas_y)
    if have:
        mxlo, mxhi = _rng(meas_x)
        mylo, myhi = _rng(meas_y)
        cxlo, cxhi = _rng(cvx)
        cylo, cyhi = _rng(cvy)
        asx = (cxhi - cxlo) / (mxhi - mxlo) if mxhi > mxlo else 1.0
        asy = (cyhi - cylo) / (myhi - mylo) if myhi > mylo else 1.0
    else:
        mxlo = mylo = cxlo = cxhi = cylo = cyhi = 0.0
        asx = asy = 1.0

    # 自動偵測最佳方向（是否鏡像）：把對齊後的量測點與座標檢視器點
    # 做占格 IoU，挑重疊最高者。
    def _auto_orient():
        if not have:
            return (False, False)
        cvxa = np.asarray(cvx, float)
        cvya = np.asarray(cvy, float)
        bx = (np.asarray(meas_x, float) - mxlo) * asx + cxlo
        by = (np.asarray(meas_y, float) - mylo) * asy + cylo
        x0, x1 = cvxa.min(), cvxa.max()
        y0, y1 = cvya.min(), cvya.max()
        nb = 40

        def cells(xs, ys):
            gx = np.clip(((xs - x0) / (x1 - x0 + 1e-9) * nb).astype(int), 0, nb - 1)
            gy = np.clip(((ys - y0) / (y1 - y0 + 1e-9) * nb).astype(int), 0, nb - 1)
            return set(zip(gx.tolist(), gy.tolist()))

        cvc = cells(cvxa, cvya)
        best = ((False, False), -1.0)
        for fx in (False, True):
            for fy in (False, True):
                mx = (cxlo + cxhi) - bx if fx else bx
                my = (cylo + cyhi) - by if fy else by
                mc = cells(mx, my)
                iou = len(mc & cvc) / max(len(mc | cvc), 1)
                if iou > best[1]:
                    best = ((fx, fy), iou)
        return best[0]

    auto_mir = _auto_orient()

    ac = st.columns([1.4, 1.6, 1.4, 1.4, 3])
    auto = ac[0].checkbox("自動對齊", value=True, key="cv_ov_auto")
    orient = ac[1].selectbox("方向", ["自動", "不鏡像", "X鏡像", "Y鏡像", "XY鏡像"],
                             key="cv_ov_orient")
    dx = ac[2].number_input("X 微調", value=0.0, step=0.5, format="%.2f",
                            key="cv_ov_dx")
    dy = ac[3].number_input("Y 微調", value=0.0, step=0.5, format="%.2f",
                            key="cv_ov_dy")
    omap = {"不鏡像": (False, False), "X鏡像": (True, False),
            "Y鏡像": (False, True), "XY鏡像": (True, True)}
    mirx, miry = auto_mir if orient == "自動" else omap[orient]
    det = ("X鏡像" if auto_mir[0] else "") + ("Y鏡像" if auto_mir[1] else "")
    ac[4].caption(f"自動對齊＋自動方向（偵測：{det or '不鏡像'}）；"
                  "若不準可手動選方向或用 X/Y 微調平移。")

    # 紅點：對映到座標檢視器座標系（平移+縮放，依方向鏡像），再翻轉/旋轉
    rxs, rys = [], []
    for x, y in zip(red_x, red_y):
        if auto:
            mx = (x - mxlo) * asx + cxlo
            my = (y - mylo) * asy + cylo
        else:
            mx, my = x, y
        if mirx:
            mx = (cxlo + cxhi) - mx
        if miry:
            my = (cylo + cyhi) - my
        tx, ty = _xform(mx + dx, my + dy, flip, rot)
        rxs.append(tx)
        rys.append(ty)

    # 點擊鎖定（任一點）→ 放大置中
    sel = st.session_state.get("meas_cv_overlay")
    locked = None
    if isinstance(sel, dict):
        pts = sel.get("selection", {}).get("points", [])
        if pts and pts[-1].get("x") is not None:
            locked = {"x": pts[-1]["x"], "y": pts[-1]["y"]}
    sig = (round(locked["x"], 6), round(locked["y"], 6)) if locked else None
    if locked is not None and st.session_state.get("meas_cv_sig") != sig:
        st.session_state["meas_cv_zoom_level"] = FOCUS_ZOOM
    st.session_state["meas_cv_sig"] = sig
    center = (locked["x"], locked["y"]) if locked else None
    zoom = zoom_controls("meas_cv_zoom", center)

    fig = go.Figure()
    tseries = []
    allv = [0.0]
    for name, df, xc, yc, color in loaded:
        if xc is None or yc is None:
            continue
        x = pd.to_numeric(df[xc], errors="coerce")
        y = pd.to_numeric(df[yc], errors="coerce")
        tx, ty = _xform(x, y, flip, rot)
        tseries.append((name, tx, ty, color))
        allv += tx.dropna().tolist()
        allv += ty.dropna().tolist()
    allv += rxs + rys
    m = max(abs(min(allv)), abs(max(allv))) or 1.0
    rng = [-(m * 1.05), m * 1.05]
    add_origin(fig, rng)
    full_half = (rng[1] - rng[0]) / 2.0
    half = full_half / max(zoom, 1e-9)
    if center is not None and zoom > 1.0:
        cx, cy = center
    else:
        cx, cy = 0.0, 0.0

    for name, tx, ty, color in tseries:
        fig.add_trace(go.Scatter(
            x=tx, y=ty, mode="markers", name=name,
            marker=dict(color=color, size=6),
            hovertemplate=f"<b>{name}</b><br>x=%{{x}}<br>y=%{{y}}<extra></extra>"))
    fig.add_trace(go.Scatter(
        x=rxs, y=rys, mode="markers", name="🔴 超規紅點",
        marker=dict(color="red", size=9, symbol="x"),
        hovertemplate="超規<br>x=%{x}<br>y=%{y}<extra></extra>"))
    if locked is not None:
        fig.add_trace(go.Scatter(
            x=[locked["x"]], y=[locked["y"]], mode="markers",
            marker=dict(symbol="circle-open", size=26, color="red",
                        line=dict(color="red", width=3)),
            hoverinfo="skip", showlegend=False))
    fig.update_layout(
        title="座標檢視器 ＋ 超規紅點（點圖放大、圖例可開關區域）",
        height=650, dragmode="pan",
        legend=dict(itemclick="toggle", itemdoubleclick="toggleothers"),
        margin=dict(l=20, r=20, t=50, b=20))
    fig.update_xaxes(range=[cx - half, cx + half], zeroline=False, title="X")
    fig.update_yaxes(range=[cy - half, cy + half], zeroline=False, title="Y",
                     scaleanchor="x", scaleratio=1)
    st.plotly_chart(fig, use_container_width=True, on_select="rerun",
                    selection_mode="points", key="meas_cv_overlay",
                    config=PLOTLY_CFG)

    # 各區域資料表（與合併顯示一致：可展開檢視）
    st.caption("各區域資料表（點圖例可開關區域；展開檢視各區域座標）")
    for name, df, xc, yc, color in loaded:
        with st.expander(f"📄 {name}（{len(df)} 列）", expanded=False):
            show = df.copy()
            show.insert(0, "該區域加工順序", range(len(df)))
            st.dataframe(show, use_container_width=True, height=240,
                         hide_index=True, key=f"ov_tbl_{name}")


def render_measurement_analysis(uploaded, loaded=None):
    """列出欄位 + 資料表，並用 X、Y 量測座標畫獨立分布圖。
    支援：點擊鎖定/放大、依矩形編號標註、超規標紅、紅點疊到座標檢視器圖。"""
    try:
        df = parse_measurement_file(uploaded)
    except Exception as e:  # noqa: BLE001
        st.error(f"解析失敗：{e}")
        return
    if df.empty:
        st.warning("找不到矩形資料，請確認是量測原始檔。")
        return

    ids = df["矩形編號"].tolist()
    id_to_pos = {rid: i for i, rid in enumerate(ids)}

    # 解析點選來源（圖表點擊 / 表格選列），決定鎖定的列（取最近變動者）
    chart_id = None
    sel = st.session_state.get("meas_scatter")
    if isinstance(sel, dict):
        for p in sel.get("selection", {}).get("points", [])[::-1]:
            if p.get("curve_number") == 0 and p.get("customdata") is not None:
                chart_id = p["customdata"]
                break
    table_pos = None
    tsel = st.session_state.get("meas_table")
    if isinstance(tsel, dict):
        rws = tsel.get("selection", {}).get("rows", [])
        if rws:
            table_pos = rws[0]
    chart_pos = id_to_pos.get(chart_id) if chart_id is not None else None
    chart_sig = ("c", chart_id) if chart_id is not None else None
    table_sig = ("t", table_pos) if table_pos is not None else None
    prev = st.session_state.get("meas_sigs", {})
    lock_pos = st.session_state.get("meas_lock_pos")
    changed = False
    for sid, sig, pos in (("chart", chart_sig, chart_pos),
                          ("table", table_sig, table_pos)):
        if sig is not None and prev.get(sid) != sig and pos is not None \
                and pos < len(ids):
            lock_pos = pos
            changed = True
        prev[sid] = sig
    st.session_state["meas_sigs"] = prev
    st.session_state["meas_lock_pos"] = lock_pos
    if changed:
        st.session_state["meas_zoom_level"] = FOCUS_ZOOM

    st.success(f"已解析 {len(df)} 個矩形")
    st.markdown("**資料欄位**：" + "、".join(f"`{c}`" for c in df.columns))

    # 鎖定的列：釘在大表格上方。點圖表的點時，這一列會同步更新（解決
    # Streamlit 無法從程式端自動選取/捲動大表格的限制）。
    if lock_pos is not None and lock_pos < len(ids):
        st.markdown(f"**🔒 已選取：矩形 {ids[lock_pos]}**　"
                    "（點圖上的點會同步更新此列）")
        st.dataframe(df.iloc[[lock_pos]], use_container_width=True,
                     hide_index=True)
    st.caption("👇 點表格任一列 → 圖上同步鎖定、放大；點圖上的點 → 上方"
               "「已選取」列同步更新。")
    st.dataframe(df, use_container_width=True, height=240, hide_index=True,
                 on_select="rerun", selection_mode="single-row",
                 key="meas_table")

    xcol, ycol = "X中心座標(量測值)", "Y中心座標(量測值)"
    xv = pd.to_numeric(df[xcol], errors="coerce")
    yv = pd.to_numeric(df[ycol], errors="coerce")

    c1, c2, c3 = st.columns([2, 2, 2])
    color_opts = ["（不上色）", "間距(量測值-CAD值)", "高度(量測值-CAD值)",
                  "X中心座標(量測值-CAD值)", "Y中心座標(量測值-CAD值)"]
    color_by = c1.selectbox("依差值上色", color_opts, key="meas_color")
    show_labels = c2.checkbox("顯示所有編號（點多時較慢）", key="meas_labels")
    rot_deg = c3.number_input("逆時針旋轉角度（度，繞原點 0,0）",
                              value=0.0, step=1.0, format="%.2f",
                              key="meas_rot")

    # 以原點為中心，逆時針旋轉 rot_deg 度
    th = math.radians(rot_deg)
    cos_t, sin_t = math.cos(th), math.sin(th)
    xr = xv * cos_t - yv * sin_t
    yr = xv * sin_t + yv * cos_t

    locked = None
    if lock_pos is not None and lock_pos < len(ids):
        locked = {"id": ids[lock_pos],
                  "x": float(xr.iloc[lock_pos]),
                  "y": float(yr.iloc[lock_pos])}
    center = (locked["x"], locked["y"]) if locked else None
    zoom = zoom_controls("meas_zoom", center)

    # 視窗範圍（1:1，以鎖定點為中心放大）。用旋轉後座標。
    xmin, xmax = float(xr.min()), float(xr.max())
    ymin, ymax = float(yr.min()), float(yr.max())
    base_half = max(xmax - xmin, ymax - ymin) / 2.0 * 1.05 or 1.0
    if center is not None and zoom > 1.0:
        cx, cy = center
        half = base_half / zoom
    else:
        cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
        half = base_half

    # 超出規格標紅設定：上限(+) / 下限(−) 各別輸入。
    #  X中心、Y中心：基準=CAD值，偏差=量測值−CAD值
    #  間距、高度：基準=該項量測值的平均，偏差=量測值−平均
    sp_meas = pd.to_numeric(df["間距(量測值)"], errors="coerce")
    ht_meas = pd.to_numeric(df["高度(量測值)"], errors="coerce")
    sp_mean = float(sp_meas.mean())
    ht_mean = float(ht_meas.mean())
    specs = [
        ("X中心", "x", pd.to_numeric(df["X中心座標(量測值-CAD值)"], errors="coerce"),
         "量測−CAD值"),
        ("Y中心", "y", pd.to_numeric(df["Y中心座標(量測值-CAD值)"], errors="coerce"),
         "量測−CAD值"),
        ("間距", "p", sp_meas - sp_mean, f"量測−平均({sp_mean:.4f})"),
        ("高度", "h", ht_meas - ht_mean, f"量測−平均({ht_mean:.4f})"),
    ]
    over = pd.Series(False, index=df.index)
    spec_active = False
    with st.expander("🔴 超出規格標紅設定，填 0=不檢查（單位 mm）。", expanded=True):
        cols = st.columns(4)
        for (lab, key, dev, basis), c in zip(specs, cols):
            c.caption(f"**{lab}**（{basis}）")
            up = _parse_float(c.text_input("上限 (+)", value="0",
                                           key=f"up_{key}"))
            lo = _parse_float(c.text_input("下限 (−)", value="0",
                                           key=f"lo_{key}"))
            if up > 0:
                spec_active = True
                over = over | (dev > up)
            if lo < 0:
                spec_active = True
                over = over | (dev < lo)
    n_over = int(over.sum())
    if spec_active:
        st.caption(f"🔴 超出規格 {n_over} / {len(df)} 個（紅點）　"
                   f"｜間距平均={sp_mean:.4f}、高度平均={ht_mean:.4f}")

    fig = go.Figure()
    if spec_active:
        marker = dict(size=6,
                      color=["red" if o else "#1f77b4" for o in over.tolist()])
    elif color_by != "（不上色）" and color_by in df.columns:
        marker = dict(size=6, color=pd.to_numeric(df[color_by], errors="coerce"),
                      colorscale="RdBu", cmid=0, showscale=True,
                      colorbar=dict(title=color_by))
    else:
        marker = dict(size=6, color="#1f77b4")
    fig.add_trace(go.Scatter(
        x=xr, y=yr,
        mode="markers+text" if show_labels else "markers",
        marker=marker,
        text=[str(i) for i in ids] if show_labels else None,
        textposition="top center", textfont=dict(size=8),
        customdata=ids,
        hovertemplate="矩形 %{customdata}<br>X=%{x}<br>Y=%{y}<extra></extra>",
    ))
    # 鎖定點：紅圈 + 編號標註
    if locked is not None:
        fig.add_trace(go.Scatter(
            x=[locked["x"]], y=[locked["y"]], mode="markers",
            marker=dict(symbol="circle-open", size=22, color="red",
                        line=dict(color="red", width=3)),
            hoverinfo="skip", showlegend=False,
        ))
        fig.add_annotation(x=locked["x"], y=locked["y"],
                           text=f"矩形 {locked['id']}",
                           showarrow=True, arrowhead=2, arrowcolor="red",
                           ax=0, ay=-40, font=dict(color="red", size=13),
                           bgcolor="rgba(255,255,255,0.8)", bordercolor="red")
    fig.update_layout(title="量測中心座標分布圖（量測值）", height=650,
                      dragmode="pan", showlegend=False,
                      margin=dict(l=20, r=20, t=50, b=20))
    fig.update_xaxes(range=[cx - half, cx + half], title="X 中心座標(量測值)",
                     zeroline=False)
    fig.update_yaxes(range=[cy - half, cy + half], title="Y 中心座標(量測值)",
                     zeroline=False, scaleanchor="x", scaleratio=1)
    st.plotly_chart(fig, use_container_width=True, on_select="rerun",
                    selection_mode="points", key="meas_scatter",
                    config=PLOTLY_CFG)

    # 第三張圖：把超規紅點疊到座標檢視器圖（需有座標檢視器資料且有標紅）
    if spec_active and loaded:
        if st.button("🟥 將超規紅點標示到座標檢視器圖（產生第三張圖）",
                     key="meas_cv_btn"):
            st.session_state["meas_show_cv"] = \
                not st.session_state.get("meas_show_cv", False)
        if st.session_state.get("meas_show_cv"):
            rx = xv[over].tolist()
            ry = yv[over].tolist()
            st.caption(f"在彩色座標檢視器圖上標出 {len(rx)} 個超規紅點"
                       "（量測座標系已自動對映到座標檢視器；圖例可開關、"
                       "點圖可放大、點任一點可鎖定圈選）。")
            render_cv_overlay(loaded, rx, ry, xv.tolist(), yv.tolist())

    if locked is not None:
        row = df[df["矩形編號"] == locked["id"]]
        if not row.empty:
            r = row.iloc[0]

            def fmt(v):
                try:
                    return f"{float(v):.4f}"
                except Exception:  # noqa: BLE001
                    return "—"

            def sfmt(v):
                try:
                    return f"{float(v):+.4f}"
                except Exception:  # noqa: BLE001
                    return "—"

            st.info(
                f"🔒 矩形 {locked['id']}\n\n"
                f"X 中心={fmt(r['X中心座標(量測值)'])}（差 {sfmt(r['X中心座標(量測值-CAD值)'])}）　"
                f"Y 中心={fmt(r['Y中心座標(量測值)'])}（差 {sfmt(r['Y中心座標(量測值-CAD值)'])}）\n\n"
                f"間距={fmt(r['間距(量測值)'])}（差 {sfmt(r['間距(量測值-CAD值)'])}）　"
                f"高度={fmt(r['高度(量測值)'])}（差 {sfmt(r['高度(量測值-CAD值)'])}）"
            )
    else:
        st.caption("👆 點圖上任一點、或點表格任一列即可鎖定，"
                   "並以 ➕ / ➖ 以該點為中心放大。")

    tsv = df.to_csv(index=False, sep="\t").encode("utf-8-sig")
    st.download_button("⬇️ 下載整理後 CSV（Tab 分隔）", tsv,
                       file_name="量測整理_矩形.csv", mime="text/csv",
                       key="meas_dl")


# ----------------------------- UI -----------------------------
st.title("📍 座標檢視器")
st.caption("上傳多個 CSV，圖表與表格雙向連動：點圖上的點或表格的列，"
           "都會鎖定該點、放大置中並以紅圈標示。")

with st.sidebar:
    st.header("上傳檔案")
    files = st.file_uploader(
        "1️⃣ 拖曳上傳多個座標 CSV 檔案",
        type=["csv"],
        accept_multiple_files=True,
    )
    st.divider()
    drill_file = st.file_uploader(
        "2️⃣ （選用）上傳 DrillDataSet 檔，依它的順序排列各區域",
        type=["csv"],
        accept_multiple_files=False,
        help="會讀取 ArrayName / FilePath 欄，依其列順序排列圖層與分頁。",
    )
    st.divider()
    meas_file = st.file_uploader(
        "3️⃣ （選用）上傳量測原始檔，列出欄位並產生分布圖",
        type=["csv"],
        accept_multiple_files=False,
        help="解析量測檔（X/Y 中心座標、間距、高度的量測值/CAD值/差值），"
             "用 X、Y 量測座標畫一張獨立的分布圖。",
    )

def _basename(s):
    """去掉路徑與副檔名，轉小寫，方便比對。"""
    s = str(s).replace("\\", "/").split("/")[-1].strip().lower()
    if s.endswith(".csv"):
        s = s[:-4]
    return s


def parse_drill_order(uploaded):
    """從 DrillDataSet 讀出區域排列順序（回傳 base 名稱清單）。"""
    raw = uploaded.getvalue()
    order = []
    for enc in ("utf-8-sig", "utf-8", "big5", "cp950", "latin-1"):
        for sep in (None, ",", "\t"):
            try:
                d = pd.read_csv(io.BytesIO(raw), encoding=enc, sep=sep,
                                engine="python")
            except Exception:  # noqa: BLE001
                continue
            col = None
            for c in d.columns:
                cl = str(c).lower()
                if "arrayname" in cl or "filepath" in cl:
                    col = c
                    if "arrayname" in cl:
                        break
            if col is not None and len(d) > 0:
                order = [_basename(v) for v in d[col].tolist()]
                return order
    return order


# 讀取座標 CSV（可能沒有）。用高對比、無紅色的調色盤，每區域唯一色。
palette = make_palette(len(files or []))
loaded = []  # (name, df, xcol, ycol, color)
for i, f in enumerate(files or []):
    try:
        df = read_csv(f)
    except Exception as e:  # noqa: BLE001
        st.error(f"無法讀取 {f.name}：{e}")
        continue
    xc, yc = find_xy_columns(df)
    loaded.append((f.name, df, xc, yc, palette[i % len(palette)]))

# 依 DrillDataSet 重新排列各區域
drill_msg = None
if loaded and drill_file is not None:
    order = parse_drill_order(drill_file)
    if order:
        rank = {nm: i for i, nm in enumerate(order)}
        loaded.sort(key=lambda d: rank.get(_basename(d[0]), len(rank) + 1))
        loaded = [(n, df, xc, yc, palette[i % len(palette)])
                  for i, (n, df, xc, yc, _c) in enumerate(loaded)]
        matched = sum(1 for d in loaded if _basename(d[0]) in rank)
        drill_msg = f"✅ 已依 DrillDataSet 排序（{matched}/{len(loaded)} 個區域比對成功）"
    else:
        drill_msg = "⚠️ 無法從 DrillDataSet 讀出 ArrayName/FilePath 欄，維持原順序。"

# === 量測檔分析（顯示在座標檢視器上方）===
if meas_file is not None:
    st.header("📐 量測檔分析")
    render_measurement_analysis(meas_file, loaded)
    st.divider()

# === 座標檢視器 ===
if not files:
    if meas_file is None:
        st.markdown(
            "#### 使用說明\n"
            "**📁 上傳座標 CSV（可多選）**　\n"
            "上傳 LCH 加工 case 的微孔座標檔案，位於 ProgramObjects 資料夾內"
            "（ex：69x71_250um-aliceblue.csv）\n\n"
            "**📑 上傳 DrillDataSet（排序）**　\n"
            "DrillDataSet.csv 是 LCH 微孔加工區域的順序，可自行排定加工順序，"
            "調換欄位即可。\n\n"
            "**📐 上傳量測原始檔**　\n"
            "台超量測產出的原始檔案直接載入即可。")
    st.stop()
if not loaded:
    st.stop()
st.header("📍 座標檢視器")
if drill_msg:
    st.caption(drill_msg)

# 各 CSV 顯示狀態
names = [d[0] for d in loaded]
if st.session_state.get("_loaded_names") != names:
    st.session_state["_loaded_names"] = names
    st.session_state["visible"] = {n: True for n in names}
visible = st.session_state["visible"]
for n in names:
    visible.setdefault(n, True)

tab_merged, tab_split = st.tabs(["🔗 合併顯示", "🗂 分頁顯示"])

# ---- 合併顯示 ----
with tab_merged:
    b1, b2, b3, binfo = st.columns([1, 1, 1.4, 3])
    if b1.button("✅ 全部顯示", key="show_all", use_container_width=True):
        for n in names:
            visible[n] = True
    if b2.button("⬜ 全部隱藏", key="hide_all", use_container_width=True):
        for n in names:
            visible[n] = False
    if b3.button("▶️ 依序隱藏動畫", key="anim_hide", use_container_width=True,
                 help="依目前排列順序（DrillDataSet 順序）每 0.25 秒隱藏一個區域"):
        st.session_state["show_anim"] = not st.session_state.get("show_anim", False)
    shown = sum(1 for n in names if visible.get(n, True))
    binfo.caption(f"顯示中 {shown} / {len(names)} 個 CSV；也可點圖例個別開關")

    if st.session_state.get("show_anim"):
        st.plotly_chart(build_hide_animation(loaded, 250),
                        use_container_width=True, key="hide_anim",
                        config=PLOTLY_CFG)
        st.caption("▶ 播放後每 0.25 秒隱藏一個區域；⟲ 重置；再按上方按鈕可收合動畫。")

    visible_files = [d for d in loaded if visible.get(d[0], True)]

    # 收集所有來源（圖表 + 每個檔案的表格）以決定鎖定點
    chart_det = selected_point_from_chart("merged_chart", loaded)
    chart_sig = (chart_det["name"], chart_det["idx"]) if chart_det else None
    sources = [("chart", chart_det, chart_sig)]
    for name, df, xc, yc, color in visible_files:
        ds = [(name, df, xc, yc, color)]
        _, meta_n = build_table(ds)
        d_n, pos_n = selected_row_from_table(f"merged_tbl_{name}", meta_n, ds)
        sources.append((f"merged_tbl_{name}", d_n, pos_n))
    det = resolve_lock("merged_lock", "merged_zoom_level", sources)

    col_chart, col_table = st.columns([3, 2])
    with col_chart:
        # 翻轉/旋轉控制（放在「⟲ 重置」上方，垂直對齊）
        cv_flip = st.toggle("🔄 翻轉到背面（背面為 X 鏡像）", key="cv_flip",
                            help="入射面=正面；勾選顯示出射面(背面)。"
                                 "旋轉同時套用到分頁與第三張疊合圖。")
        cv_rot = st.number_input("逆時針旋轉（度，繞原點）", value=0.0, step=1.0,
                                 format="%.2f", key="cv_rot")
        # 鎖定點座標依目前翻轉/旋轉換算（讓紅圈跟著變換）
        center = None
        label = None
        if det:
            rxy = _row_xy(loaded, det["name"], det["idx"])
            if rxy:
                center = _xform(rxy[0], rxy[1], cv_flip, cv_rot)
            label = f"{det['name']} #{det['idx']}"
        zoom = zoom_controls("merged_zoom", center)
        use_mark = center is not None and zoom > 1.0
        fig = make_figure(
            loaded, "全部 CSV（合併）", center=center, zoom=zoom,
            visible=visible,
            marked=center if use_mark else None,
            mark_label=label if use_mark else None,
            flip=cv_flip, rot_deg=cv_rot,
        )
        st.plotly_chart(
            fig, use_container_width=True,
            on_select="rerun", selection_mode="points",
            key="merged_chart", config=PLOTLY_CFG,
        )
    with col_table:
        st.subheader("鎖定點資訊")
        lock_info_panel(det)
        st.caption("👇 展開檔案後點某一列 → 圖上同步標示並放大置中")
        for name, df, xc, yc, color in visible_files:
            locked_here = det is not None and det["name"] == name
            with st.expander(f"📄 {name}（{len(df)} 列）", expanded=locked_here):
                show = df.copy()
                show.insert(0, "該區域加工順序", range(len(df)))
                st.dataframe(
                    show, use_container_width=True, height=240,
                    hide_index=True, on_select="rerun",
                    selection_mode="single-row", key=f"merged_tbl_{name}",
                )

# ---- 分頁顯示 ----
with tab_split:
    s_flip = st.session_state.get("cv_flip", False)
    s_rot = st.session_state.get("cv_rot", 0.0)
    sub_tabs = st.tabs([name for name, *_ in loaded])
    for t, (name, df, xc, yc, color) in zip(sub_tabs, loaded):
        with t:
            ds = [(name, df, xc, yc, color)]
            tbl_df, meta_s = build_table(ds)
            ckey, tkey = f"chart_{name}", f"tbl_{name}"
            chart_det = selected_point_from_chart(ckey, ds)
            table_det, table_pos = selected_row_from_table(tkey, meta_s, ds)
            chart_sig = (chart_det["name"], chart_det["idx"]) if chart_det else None
            det = resolve_lock(
                f"lock_{name}", f"zoom_{name}_level",
                [("chart", chart_det, chart_sig),
                 ("table", table_det, table_pos)],
            )
            center = None
            label = None
            if det:
                rxy = _row_xy(ds, det["name"], det["idx"])
                if rxy:
                    center = _xform(rxy[0], rxy[1], s_flip, s_rot)
                label = f"#{det['idx']}"

            c1, c2 = st.columns([3, 2])
            with c1:
                zoom = zoom_controls(f"zoom_{name}", center)
                use_mark = center is not None and zoom > 1.0
                fig1 = make_figure(
                    ds, name, center=center, zoom=zoom,
                    marked=center if use_mark else None,
                    mark_label=label if use_mark else None,
                    flip=s_flip, rot_deg=s_rot,
                )
                st.plotly_chart(
                    fig1, use_container_width=True,
                    on_select="rerun", selection_mode="points",
                    key=ckey, config=PLOTLY_CFG,
                )
            with c2:
                st.subheader("鎖定點資訊")
                lock_info_panel(det)
                st.caption("👇 點任一列 → 圖上同步標示並放大置中")
                st.dataframe(
                    tbl_df, use_container_width=True, height=360,
                    hide_index=True, on_select="rerun",
                    selection_mode="single-row", key=tkey,
                )
            if xc is None or yc is None:
                st.warning(f"{name}：找不到可用的 X / Y 數值欄位。")
