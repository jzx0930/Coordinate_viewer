# coordinate_viewer_desktop.py
# 座標檢視器（桌面版）— PyQt5 + pyqtgraph，為大量點即時互動而生
# 執行: python coordinate_viewer_desktop.py
#
# 功能對齊網頁版：
#   座標檢視器：多 CSV 合併 + 各檔分頁；高對比無紅色配色；翻轉到背面(X鏡像)、
#     逆時針旋轉；點圖/點表格列鎖定並放大置中(紅圈+編號)；滾輪以游標為中心縮放、
#     拖曳平移；全部顯示/隱藏；依序隱藏動畫；DrillDataSet 排序。
#   量測檔分析：解析量測原始檔→資料表；X/Y 量測中心分布圖（旋轉、鎖定、依編號標註）；
#     超規標紅（X/Y中心=量測−CAD；間距/高度=量測−平均；各設上下限）。
#   第三張疊合圖：彩色座標檢視器 + 超規紅點（自動對齊兩座標系，含鏡像偵測）。

import csv
import io
import math
import os
import re
import sys

import numpy as np
import pandas as pd
from PyQt5 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

pg.setConfigOptions(antialias=False, background="#1f2125", foreground="#c8ccd0")

FOCUS_ZOOM = 8.0
HIDE_INTERVAL_MS = 250


# ===================== 純資料函式 =====================
def make_palette(n):
    import colorsys
    n = max(int(n), 1)
    cols = []
    for i in range(n):
        h = (i * 0.6180339887) % 1.0
        h = 0.06 + h * 0.86
        s = 0.55 if i % 3 == 2 else 0.78
        v = 0.97 if i % 2 == 0 else 0.74
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        cols.append("#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255)))
    return cols


def find_xy_columns(df):
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


def read_csv(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "utf-8", "big5", "cp950", "gbk", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(raw), encoding=enc)
        except (UnicodeDecodeError, pd.errors.ParserError):
            continue
    return pd.read_csv(io.BytesIO(raw), encoding="latin-1", engine="python")


def _xform(x, y, flip=False, rot_deg=0.0):
    if flip:
        x = -x
    th = math.radians(rot_deg)
    c, s = math.cos(th), math.sin(th)
    return x * c - y * s, x * s + y * c


def _basename(s):
    s = str(s).replace("\\", "/").split("/")[-1].strip().lower()
    if s.endswith(".csv"):
        s = s[:-4]
    return s


def parse_drill_order(path):
    with open(path, "rb") as fh:
        raw = fh.read()
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
                return [_basename(v) for v in d[col].tolist()]
    return []


def _read_excel_rows(path):
    """讀 .xlsx/.xls 成一格一格的字串二維陣列（與 CSV 的 rows 結構一致）。"""
    try:
        xl = pd.read_excel(path, header=None, dtype=object, sheet_name=0)
    except Exception:  # noqa: BLE001
        return []
    return [["" if pd.isna(v) else str(v) for v in r]
            for r in xl.values.tolist()]


def parse_measurement_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm", ".xls"):
        rows = _read_excel_rows(path)
    else:
        raw = open(path, "rb").read()
        text = None
        for enc in ("utf-16", "utf-16-le", "utf-8-sig",
                    "utf-8", "big5", "cp950"):
            try:
                text = raw.decode(enc)
                break
            except Exception:  # noqa: BLE001
                continue
        if text is None:
            text = raw.decode("latin-1")
        # 自動判斷分隔符號：原始台超檔用 Tab；被 Excel 另存過的用逗號
        delim = "\t" if "\t" in text else ","
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))

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
    df = pd.DataFrame(out, columns=header)
    # 依矩形編號由小到大排序（不管原始檔怎麼排都一致）
    key = pd.to_numeric(df["矩形編號"], errors="coerce")
    df = (df.assign(_k=key)
            .sort_values("_k", kind="stable", na_position="last")
            .drop(columns="_k").reset_index(drop=True))
    return df


def diagnose_meas(path):
    """量測檔解析不出資料時，回傳一段人看得懂的可能原因。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm", ".xls"):
        rows = _read_excel_rows(path)
        if not rows:
            return ("無法讀取這個 Excel 檔（請確認是有效的 .xlsx，"
                    "且已安裝 openpyxl 套件）。")
        text = "\n".join("\t".join(r) for r in rows)
    else:
        try:
            raw = open(path, "rb").read()
        except Exception as e:  # noqa: BLE001
            return f"無法讀取檔案：{e}"
        text = None
        for enc in ("utf-16", "utf-16-le", "utf-8-sig",
                    "utf-8", "big5", "cp950"):
            try:
                text = raw.decode(enc)
                break
            except Exception:  # noqa: BLE001
                continue
        if text is None:
            return "檔案編碼無法辨識（不是 UTF-16／UTF-8／Big5）。"
    kw = ("中心座標", "半徑", "間距", "高度")
    if "矩形" not in text:
        if any(k in text for k in kw):
            return ("檔案裡沒有『矩形』這個特徵名稱（可能只有圓或其他命名）；"
                    "量測分析目前只處理「矩形 N: …」格式的資料。")
        return ("這看起來不是台超量測原始檔——找不到「矩形」與"
                "「中心座標／間距／高度」等欄位。\n"
                "請確認上傳的是『量測原始檔』，而不是座標 CSV、"
                "DrillDataSet 或已整理過的表格。")
    if not any(k in text for k in kw):
        return "有「矩形」但找不到「中心座標／間距／高度」數值欄位。"
    return ("找到「矩形」也找到量測欄位，但欄位切不出來——"
            "可能分隔符號不是 Tab 也不是逗號，或檔案結構與台超原始檔不同。")


def _cluster_blocks(x, y, nb=64, min_frac=0.02):
    """用格點 8-連通把點雲分成數個區塊（面板），濾掉很小的離群塊。
    回傳 list，每塊含 px/py（點座標）、cx/cy 質心、x/ylo、x/yhi 外框。"""
    from collections import deque
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    ok = ~(np.isnan(x) | np.isnan(y))
    x2, y2 = x[ok], y[ok]
    if len(x2) == 0:
        return []
    x0, x1 = x2.min(), x2.max()
    y0, y1 = y2.min(), y2.max()
    gx = np.clip(((x2 - x0) / (x1 - x0 + 1e-9) * nb).astype(int), 0, nb - 1)
    gy = np.clip(((y2 - y0) / (y1 - y0 + 1e-9) * nb).astype(int), 0, nb - 1)
    occ = np.zeros((nb, nb), bool)
    occ[gx, gy] = True
    label = -np.ones((nb, nb), int)
    cid = 0
    for i in range(nb):
        for j in range(nb):
            if occ[i, j] and label[i, j] < 0:
                q = deque([(i, j)])
                label[i, j] = cid
                while q:
                    a, b = q.popleft()
                    for da in (-1, 0, 1):
                        for db in (-1, 0, 1):
                            na, nbj = a + da, b + db
                            if 0 <= na < nb and 0 <= nbj < nb \
                                    and occ[na, nbj] and label[na, nbj] < 0:
                                label[na, nbj] = cid
                                q.append((na, nbj))
                cid += 1
    if cid == 0:
        return []
    comp = label[gx, gy]                      # 每個點的元件編號
    tot = len(x2)
    blocks = []
    for kk in range(cid):
        m = comp == kk
        if m.sum() < max(5, min_frac * tot):  # 濾掉離群小塊
            continue
        bx, by = x2[m], y2[m]
        blocks.append(dict(px=bx, py=by,
                           cx=float(bx.mean()), cy=float(by.mean()),
                           xlo=float(bx.min()), xhi=float(bx.max()),
                           ylo=float(by.min()), yhi=float(by.max())))
    return blocks


def _block_map(mblk, cblk, nb=24):
    """建立「量測區塊 → 座標檢視器區塊」的外框線性映射 + 區塊內鏡像判斷。"""
    mxlo, mxhi = mblk["xlo"], mblk["xhi"]
    mylo, myhi = mblk["ylo"], mblk["yhi"]
    cxlo, cxhi = cblk["xlo"], cblk["xhi"]
    cylo, cyhi = cblk["ylo"], cblk["yhi"]
    asx = (cxhi - cxlo) / (mxhi - mxlo) if mxhi > mxlo else 1.0
    asy = (cyhi - cylo) / (myhi - mylo) if myhi > mylo else 1.0

    def base(x, y):
        return (x - mxlo) * asx + cxlo, (y - mylo) * asy + cylo

    def cells(xs, ys):
        gx = np.clip(((xs - cxlo) / (cxhi - cxlo + 1e-9) * nb).astype(int),
                     0, nb - 1)
        gy = np.clip(((ys - cylo) / (cyhi - cylo + 1e-9) * nb).astype(int),
                     0, nb - 1)
        return set(zip(np.atleast_1d(gx).tolist(), np.atleast_1d(gy).tolist()))

    cvc = cells(cblk["px"], cblk["py"])
    bx, by = base(mblk["px"], mblk["py"])
    best = ((False, False), -1.0)
    for fx in (False, True):
        for fy in (False, True):
            mx = (cxlo + cxhi) - bx if fx else bx
            my = (cylo + cyhi) - by if fy else by
            iou = len(cells(mx, my) & cvc) / max(len(cells(mx, my) | cvc), 1)
            if iou > best[1]:
                best = ((fx, fy), iou)
    mirx, miry = best[0]

    def f(x, y):
        mx, my = base(x, y)
        if mirx:
            mx = (cxlo + cxhi) - mx
        if miry:
            my = (cylo + cyhi) - my
        return mx, my
    return f


def _match_panels(mb, cb):
    """把量測區塊與 CV 區塊用正規化質心最近鄰配對，回傳(各塊 map, 各量測塊質心)。"""
    def norm(blocks):
        cx = np.array([b["cx"] for b in blocks])
        cy = np.array([b["cy"] for b in blocks])

        def nz(a):
            lo, hi = a.min(), a.max()
            return (a - lo) / (hi - lo) if hi > lo else a * 0.0
        return np.c_[nz(cx), nz(cy)]

    nm, nc = norm(mb), norm(cb)
    used, maps, centers = set(), [], []
    for i in range(len(mb)):
        best, bd = -1, 1e18
        for j in range(len(cb)):
            if j in used:
                continue
            d = float(((nm[i] - nc[j]) ** 2).sum())
            if d < bd:
                bd, best = d, j
        used.add(best)
        maps.append(_block_map(mb[i], cb[best]))
        centers.append((mb[i]["cx"], mb[i]["cy"]))
    return maps, centers


def _align_global(meas_x, meas_y, cvx, cvy):
    """整體外框對齊（單一 bounding box + 一次鏡像判斷）。分面板不成立時的後備。"""
    def rng(a):
        return float(np.percentile(a, 2)), float(np.percentile(a, 98))

    mxlo, mxhi = rng(meas_x)
    mylo, myhi = rng(meas_y)
    cxlo, cxhi = rng(cvx)
    cylo, cyhi = rng(cvy)
    asx = (cxhi - cxlo) / (mxhi - mxlo) if mxhi > mxlo else 1.0
    asy = (cyhi - cylo) / (myhi - mylo) if myhi > mylo else 1.0
    bx = (meas_x - mxlo) * asx + cxlo
    by = (meas_y - mylo) * asy + cylo
    x0, x1 = cvx.min(), cvx.max()
    y0, y1 = cvy.min(), cvy.max()
    nb = 40

    def cells(xs, ys):
        gx = np.clip(((xs - x0) / (x1 - x0 + 1e-9) * nb).astype(int), 0, nb - 1)
        gy = np.clip(((ys - y0) / (y1 - y0 + 1e-9) * nb).astype(int), 0, nb - 1)
        return set(zip(gx.tolist(), gy.tolist()))

    cvc = cells(cvx, cvy)
    best = ((False, False), -1.0)
    for fx in (False, True):
        for fy in (False, True):
            mx = (cxlo + cxhi) - bx if fx else bx
            my = (cylo + cyhi) - by if fy else by
            iou = len(cells(mx, my) & cvc) / max(len(cells(mx, my) | cvc), 1)
            if iou > best[1]:
                best = ((fx, fy), iou)
    mirx, miry = best[0]

    def f(x, y):
        mx = (x - mxlo) * asx + cxlo
        my = (y - mylo) * asy + cylo
        if mirx:
            mx = (cxlo + cxhi) - mx
        if miry:
            my = (cylo + cyhi) - my
        return mx, my
    return f


def align_measurement_to_cv(meas_x, meas_y, cvx, cvy):
    """量測座標 → 座標檢視器座標。兩邊都是多塊面板且塊數相同時分面板各自對齊，
    否則退回整體外框對齊。"""
    meas_x = np.asarray(meas_x, float)
    meas_y = np.asarray(meas_y, float)
    cvx = np.asarray(cvx, float)
    cvy = np.asarray(cvy, float)
    if not (len(meas_x) and len(cvx)):
        return lambda x, y: (x, y)
    mb = _cluster_blocks(meas_x, meas_y)
    cb = _cluster_blocks(cvx, cvy)
    if len(mb) >= 2 and len(mb) == len(cb):
        maps, centers = _match_panels(mb, cb)
        mc = np.array(centers, float)

        def f(x, y):
            k = int(np.argmin((mc[:, 0] - x) ** 2 + (mc[:, 1] - y) ** 2))
            return maps[k](x, y)
        return f
    return _align_global(meas_x, meas_y, cvx, cvy)


def register_meas_to_cv(mx, my, hx, hy, cell=0.065, sample=8000, astep=3,
                        iters=10):
    """自動配準：量測點與座標檢視器孔為同尺度，找剛體轉換（旋轉+水平翻轉+平移）
    讓最多量測點落在孔上。流程：粗搜角度/翻轉 → 細格點 → ICP 最小平方收尾。
    回傳 dict：T(x,y)->(cx,cy) 轉換、frac 擬合率、deg 角度、flip 是否水平翻轉。"""
    mx = np.asarray(mx, float)
    my = np.asarray(my, float)
    ok = ~(np.isnan(mx) | np.isnan(my))
    mx, my = mx[ok], my[ok]
    hx = np.asarray(hx, float)
    hy = np.asarray(hy, float)
    hok = ~(np.isnan(hx) | np.isnan(hy))
    hx, hy = hx[hok], hy[hok]
    if len(mx) < 3 or len(hx) < 3:
        return None
    x0, y0 = hx.min() - cell, hy.min() - cell
    nx = int((hx.max() - x0) / cell) + 3
    ny = int((hy.max() - y0) / cell) + 3
    gx = ((hx - x0) / cell).astype(int)
    gy = ((hy - y0) / cell).astype(int)
    occ = np.zeros((nx, ny), bool)
    occ[gx, gy] = True
    d = occ.copy()
    d[1:, :] |= occ[:-1, :]
    d[:-1, :] |= occ[1:, :]
    o = d.copy()
    o[:, 1:] |= d[:, :-1]
    o[:, :-1] |= d[:, 1:]
    occd = o                       # 膨脹後的佔用格（粗搜計分用）
    cidx = -np.ones((nx, ny), int)
    cidx[gx, gy] = np.arange(len(hx))   # 每格對應的孔索引（ICP 找最近孔用）

    def count(rx, ry):
        jx = ((rx - x0) / cell).astype(int)
        jy = ((ry - y0) / cell).astype(int)
        m = (jx >= 0) & (jx < nx) & (jy >= 0) & (jy < ny)
        return int(occd[jx[m], jy[m]].sum())

    def nearest(cx, cy):
        jx = ((cx - x0) / cell).astype(int)
        jy = ((cy - y0) / cell).astype(int)
        bd = np.full(len(cx), np.inf)
        bi = np.full(len(cx), -1, int)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                ax = np.clip(jx + dx, 0, nx - 1)
                ay = np.clip(jy + dy, 0, ny - 1)
                hi = cidx[ax, ay]
                has = hi >= 0
                hh = np.where(has, hi, 0)
                dd = np.where(has, (hx[hh] - cx) ** 2 + (hy[hh] - cy) ** 2,
                              np.inf)
                u = dd < bd
                bd[u] = dd[u]
                bi[u] = hi[u]
        return bi, bd

    if len(mx) > sample:
        sub = np.linspace(0, len(mx) - 1, sample).astype(int)
        smx, smy = mx[sub], my[sub]
    else:
        smx, smy = mx, my
    mcx, mcy = mx.mean(), my.mean()
    hcx, hcy = hx.mean(), hy.mean()
    # 粗搜：水平翻轉 × 旋轉角（每 astep 度）
    best = None
    for flip in (False, True):
        s_ = -1.0 if flip else 1.0
        X = s_ * (smx - mcx)
        Y = smy - mcy
        for deg in range(0, 360, astep):
            th = math.radians(deg)
            c, s = math.cos(th), math.sin(th)
            cc = count(X * c - Y * s + hcx, X * s + Y * c + hcy)
            if best is None or cc > best[0]:
                best = (cc, deg, flip)
    _, deg0, flip = best
    sx = -1.0 if flip else 1.0
    X = sx * (smx - mcx)
    Y = smy - mcy
    # 細格點：角度 ±2°（0.05°）+ 微平移 ±0.6mm，取得足夠精準的起點給 ICP
    ref = (-1, float(deg0), 0.0, 0.0)
    for ddeg in np.arange(deg0 - 2, deg0 + 2.01, 0.05):
        th = math.radians(ddeg)
        c, s = math.cos(th), math.sin(th)
        bx, by = X * c - Y * s, X * s + Y * c
        for ex in np.arange(-0.6, 0.61, 0.3):
            for ey in np.arange(-0.6, 0.61, 0.3):
                cc = count(bx + hcx + ex, by + hcy + ey)
                if cc > ref[0]:
                    ref = (cc, ddeg, ex, ey)
    _, ddeg, ex, ey = ref
    th = math.radians(ddeg)
    R = np.array([[math.cos(th), -math.sin(th)],
                  [math.sin(th),  math.cos(th)]])
    S = np.stack([sx * (mx - mcx), my - mcy])   # 2xN 來源點（含翻轉、置中）
    t = np.array([hcx + ex, hcy + ey])
    tol2 = cell ** 2                            # 容差＝半孔距（只配到正確孔）
    for _ in range(iters):
        P = R @ S + t[:, None]
        bi, bd = nearest(P[0], P[1])
        good = (bi >= 0) & (bd < tol2)
        if int(good.sum()) < 10:
            break
        Sg = S[:, good]
        Qg = np.stack([hx[bi[good]], hy[bi[good]]])
        sc = Sg.mean(1, keepdims=True)
        qc = Qg.mean(1, keepdims=True)
        H = (Sg - sc) @ (Qg - qc).T
        U, _, Vt = np.linalg.svd(H)
        Rn = Vt.T @ U.T
        if np.linalg.det(Rn) < 0:
            Vt[-1] *= -1
            Rn = Vt.T @ U.T
        R = Rn
        t = qc[:, 0] - R @ sc[:, 0]
    P = R @ S + t[:, None]
    bi, bd = nearest(P[0], P[1])
    frac = float(((bi >= 0) & (bd < cell ** 2)).mean())
    fdeg = math.degrees(math.atan2(R[1, 0], R[0, 0]))

    def T(x, y):
        s2 = np.stack([sx * (np.asarray(x, float) - mcx),
                       np.asarray(y, float) - mcy])
        p = R @ s2 + t[:, None] if s2.ndim > 1 else R @ s2 + t
        return (p[0], p[1])

    return {"T": T, "frac": frac, "deg": float(fdeg), "flip": bool(flip)}


def register_meas_panels(mx, my, hx, hy, anchors, cell=0.065, iters=15):
    """分面板錨點配準：整體配準取旋轉/翻轉，再把量測分成數個面板，
    每個面板用它自己的錨點（量測點↔孔）鎖定初始位置後 ICP 收斂。
    anchors: [(ax, ay, cvx, cvy), ...] 各為一個面板的錨點（量測原始座標 ↔ 孔座標）。
    回傳 T(x,y)：依點所屬面板套用對應剛體轉換。無錨點時退回整體配準。"""
    mx = np.asarray(mx, float)
    my = np.asarray(my, float)
    hx = np.asarray(hx, float)
    hy = np.asarray(hy, float)
    g = register_meas_to_cv(mx, my, hx, hy)
    if not anchors or g is None:
        return g["T"] if g else (lambda x, y: (np.asarray(x), np.asarray(y)))
    deg, flip = g["deg"], g["flip"]
    sx = -1.0 if flip else 1.0
    th = math.radians(deg)
    R0 = np.array([[math.cos(th), -math.sin(th)],
                   [math.sin(th),  math.cos(th)]])
    x0, y0 = hx.min() - cell, hy.min() - cell
    nx = int((hx.max() - x0) / cell) + 3
    ny = int((hy.max() - y0) / cell) + 3
    cidx = -np.ones((nx, ny), int)
    cidx[((hx - x0) / cell).astype(int),
         ((hy - y0) / cell).astype(int)] = np.arange(len(hx))

    def nearest(cx, cy):
        jx = ((cx - x0) / cell).astype(int)
        jy = ((cy - y0) / cell).astype(int)
        bd = np.full(len(cx), np.inf)
        bi = np.full(len(cx), -1, int)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                ax_ = np.clip(jx + dx, 0, nx - 1)
                ay_ = np.clip(jy + dy, 0, ny - 1)
                hi = cidx[ax_, ay_]
                has = hi >= 0
                hh = np.where(has, hi, 0)
                dd = np.where(has, (hx[hh] - cx) ** 2 + (hy[hh] - cy) ** 2,
                              np.inf)
                u = dd < bd
                bd[u] = dd[u]
                bi[u] = hi[u]
        return bi, bd

    def block_map(b, anc):
        mcx, mcy = b["cx"], b["cy"]
        R = R0.copy()
        sa = np.array([sx * (anc[0] - mcx), anc[1] - mcy])
        t = np.array([anc[2], anc[3]]) - R @ sa
        S = np.stack([sx * (b["px"] - mcx), b["py"] - mcy])
        for _ in range(iters):
            P = R @ S + t[:, None]
            bi, bd = nearest(P[0], P[1])
            good = (bi >= 0) & (bd < cell ** 2)
            if int(good.sum()) < 6:
                break
            Sg = S[:, good]
            Qg = np.stack([hx[bi[good]], hy[bi[good]]])
            scn = Sg.mean(1, keepdims=True)
            qc = Qg.mean(1, keepdims=True)
            H = (Sg - scn) @ (Qg - qc).T
            U, _, Vt = np.linalg.svd(H)
            Rn = Vt.T @ U.T
            if np.linalg.det(Rn) < 0:
                Vt[-1] *= -1
                Rn = Vt.T @ U.T
            R = Rn
            t = qc[:, 0] - R @ scn[:, 0]

        def f(x, y):
            s2 = np.stack([sx * (np.asarray(x, float) - mcx),
                           np.asarray(y, float) - mcy])
            p = R @ s2 + t[:, None] if s2.ndim > 1 else R @ s2 + t
            return (p[0], p[1])
        return f

    blocks = _cluster_blocks(mx, my)
    maps, cents = [], []
    for b in blocks:
        anc = None
        for a in anchors:
            if (b["xlo"] - 0.3 <= a[0] <= b["xhi"] + 0.3
                    and b["ylo"] - 0.3 <= a[1] <= b["yhi"] + 0.3):
                anc = a
                break
        maps.append(block_map(b, anc) if anc is not None else g["T"])
        cents.append((b["cx"], b["cy"]))
    cents = np.array(cents, float)

    def T(x, y):
        xa = np.asarray(x, float)
        ya = np.asarray(y, float)
        if xa.ndim == 0:
            k = int(np.argmin((cents[:, 0] - float(xa)) ** 2
                              + (cents[:, 1] - float(ya)) ** 2))
            return maps[k](float(xa), float(ya))
        ox = np.empty_like(xa)
        oy = np.empty_like(ya)
        d = ((xa[:, None] - cents[None, :, 0]) ** 2
             + (ya[:, None] - cents[None, :, 1]) ** 2)
        kk = np.argmin(d, axis=1)
        for j in range(len(maps)):
            m = kk == j
            if m.any():
                cxx, cyy = maps[j](xa[m], ya[m])
                ox[m] = cxx
                oy[m] = cyy
        return (ox, oy)
    return T


# ===================== 表格模型（虛擬化，支援上萬列） =====================
class DataFrameModel(QtCore.QAbstractTableModel):
    def __init__(self, df):
        super().__init__()
        self._df = df.reset_index(drop=True)
        self._hl = -1

    def rowCount(self, parent=None):
        return len(self._df)

    def columnCount(self, parent=None):
        return self._df.shape[1]

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid():
            return None
        if role == QtCore.Qt.DisplayRole:
            v = self._df.iat[index.row(), index.column()]
            return "" if pd.isna(v) else str(v)
        if role == QtCore.Qt.TextAlignmentRole:
            return int(QtCore.Qt.AlignCenter)
        return None

    def headerData(self, sec, orient, role=QtCore.Qt.DisplayRole):
        if role == QtCore.Qt.DisplayRole and orient == QtCore.Qt.Horizontal:
            # 括號部分換到下一行顯示
            return str(self._df.columns[sec]).replace("(", "\n(")
        return None

    def set_highlight(self, row):
        old = self._hl
        self._hl = row
        for r in (old, row):
            if 0 <= r < self.rowCount():
                self.dataChanged.emit(self.index(r, 0),
                                      self.index(r, self.columnCount() - 1))


def with_index(df):
    """在最前面加一欄「該區域加工順序」(從 0 起)。"""
    d = df.copy()
    d.insert(0, "該區域加工順序", range(len(df)))
    return d


def make_table(df, on_row):
    """QTableView（虛擬化）+ 單列選取回呼 on_row(idx)。"""
    view = QtWidgets.QTableView()
    view.setModel(DataFrameModel(df))
    view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
    view.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
    view.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    view.verticalHeader().setVisible(False)
    hh = view.horizontalHeader()
    hh.setStretchLastSection(True)
    hh.setDefaultAlignment(QtCore.Qt.AlignCenter)
    hh.setMinimumHeight(46)   # 容納兩行表頭（括號換行）
    view.setStyleSheet(
        "QTableView{selection-background-color:#bdbdbd;selection-color:black;}")
    view._suppress = False

    def _sel():
        if getattr(view, "_suppress", False):
            return
        idxs = view.selectionModel().selectedRows()
        if idxs:
            on_row(idxs[0].row())
    view.selectionModel().selectionChanged.connect(lambda *a: _sel())
    return view


def highlight_table_row(view, idx):
    m = view.model()
    if 0 <= idx < m.rowCount():
        view._suppress = True
        view.selectRow(idx)
        view.scrollTo(m.index(idx, 0),
                      QtWidgets.QAbstractItemView.PositionAtCenter)
        view._suppress = False


# ===================== 繪圖面板（pyqtgraph） =====================
class PlotPanel(QtWidgets.QWidget):
    lockChanged = QtCore.pyqtSignal(str, int)  # (series name, idx)

    def __init__(self, title="", label_prefix="#", show_reset=True, hint=None):
        super().__init__()
        self.label_prefix = label_prefix
        self.series = []
        self.red = None
        self.flip = False
        self.rot = 0.0
        self.lock = None         # (kind, name, idx)
        self.items = {}
        self.red_item = None
        self.base_half = 1.0

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QtWidgets.QHBoxLayout()
        if show_reset:
            b = QtWidgets.QPushButton("⟲ 重置")
            b.clicked.connect(self.reset)
            bar.addWidget(b)
        self.info = QtWidgets.QLabel(
            hint or "點圖上的點即鎖定並放大；滾輪以游標為中心縮放、可拖曳平移")
        bar.addWidget(self.info)
        bar.addStretch(1)
        lay.addLayout(bar)

        self.plot = pg.PlotWidget()
        self.plot.setAspectLocked(True)
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setTitle(title)
        self.plot.setLabel("bottom", "X")
        self.plot.setLabel("left", "Y")
        lay.addWidget(self.plot)
        self.vb = self.plot.getViewBox()
        green = pg.mkPen((0, 160, 0), style=QtCore.Qt.DotLine)
        self.plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=green))
        self.plot.addItem(pg.InfiniteLine(pos=0, angle=90, pen=green))

        self.lock_ring = pg.ScatterPlotItem(
            symbol="o", size=26, brush=None, pen=pg.mkPen("r", width=2))
        self.lock_ring.setZValue(50)
        self.lock_text = pg.TextItem("", color="r", anchor=(0, 1))
        self.lock_text.setZValue(51)
        self.plot.addItem(self.lock_ring)
        self.plot.addItem(self.lock_text)

        self._gx = self._gy = None
        self._gmeta = []
        self._region = {}
        self._gvisible = None
        self._red_disp = None
        self.plot.scene().sigMouseClicked.connect(self._on_scene_click)

    # ---- 資料 ----
    def set_series(self, series):
        self.series = series
        self.lock = None

    def set_red(self, rx, ry, ids=None):
        if rx is not None and len(rx):
            self.red = {"rx": np.asarray(rx, float), "ry": np.asarray(ry, float),
                        "ids": ids}
        else:
            self.red = None

    def set_flip_rot(self, flip, rot):
        self.flip = bool(flip)
        self.rot = float(rot)
        self.draw()

    def draw(self):
        for it in self.items.values():
            self.plot.removeItem(it)
        self.items = {}
        if self.red_item is not None:
            self.plot.removeItem(self.red_item)
            self.red_item = None
        allv = [0.0]
        gx, gy, meta = [], [], []
        self._region = {}
        cum = 0
        for s in self.series:
            tx, ty = _xform(s["rx"], s["ry"], self.flip, self.rot)
            tx = np.asarray(tx, float)
            ty = np.asarray(ty, float)
            it = pg.ScatterPlotItem(x=tx, y=ty, pen=None, size=6,
                                    brush=pg.mkBrush(s["color"]))
            it.setVisible(s.get("visible", True))
            self.plot.addItem(it)
            self.items[s["name"]] = it
            n = len(tx)
            gx.append(tx)
            gy.append(ty)
            meta.extend([(s["name"], k) for k in range(n)])
            self._region[s["name"]] = (cum, cum + n)
            cum += n
            msk = ~(np.isnan(tx) | np.isnan(ty))
            if s.get("visible", True) and msk.any():
                allv += [float(np.max(np.abs(tx[msk]))),
                         float(np.max(np.abs(ty[msk])))]
        if gx:
            self._gx = np.concatenate(gx)
            self._gy = np.concatenate(gy)
            self._gmeta = meta
            self._gvisible = np.ones(len(self._gx), bool)
            for s in self.series:
                if not s.get("visible", True):
                    a, b = self._region[s["name"]]
                    self._gvisible[a:b] = False
        else:
            self._gx = self._gy = None
            self._gmeta = []
            self._gvisible = None
        self._red_disp = None
        if self.red is not None:
            rtx, rty = _xform(self.red["rx"], self.red["ry"], self.flip, self.rot)
            self._red_disp = (np.asarray(rtx, float), np.asarray(rty, float))
            self.red_item = pg.ScatterPlotItem(
                x=rtx, y=rty, symbol="x", size=12, brush=pg.mkBrush("r"),
                pen=pg.mkPen("r"))
            self.red_item.setZValue(10)
            self.plot.addItem(self.red_item)
            allv += [float(np.nanmax(np.abs(rtx))), float(np.nanmax(np.abs(rty)))]
        m = max(abs(min(allv)), abs(max(allv))) or 1.0
        self.base_half = m * 1.05
        self._update_lock()
        self.reset_view()

    def reset_view(self):
        h = self.base_half
        self.vb.setRange(xRange=(-h, h), yRange=(-h, h), padding=0)

    def set_series_visible(self, name, vis):
        if name in self.items:
            self.items[name].setVisible(vis)
        if self._gvisible is not None and name in self._region:
            a, b = self._region[name]
            self._gvisible[a:b] = vis

    # ---- 鎖定：場景點擊 + numpy 找最近可見點（不靠 sigClicked，較穩） ----
    def _on_scene_click(self, ev):
        try:
            if ev.button() != QtCore.Qt.LeftButton:
                return
        except Exception:  # noqa: BLE001
            return
        pt = self.vb.mapSceneToView(ev.scenePos())
        x, y = pt.x(), pt.y()
        best = None
        if self._gx is not None and len(self._gx):
            d2 = (self._gx - x) ** 2 + (self._gy - y) ** 2
            # 排除不可見與座標為 NaN 的點（NaN 會讓 argmin 誤判成最小值）
            d2 = np.where(self._gvisible & np.isfinite(d2), d2, np.inf)
            gi = int(np.argmin(d2))
            if np.isfinite(d2[gi]):
                best = (float(d2[gi]), "series", self._gmeta[gi])
        if self._red_disp is not None and len(self._red_disp[0]):
            rx, ry = self._red_disp
            d2 = (rx - x) ** 2 + (ry - y) ** 2
            d2 = np.where(np.isfinite(d2), d2, np.inf)
            ri = int(np.argmin(d2))
            if np.isfinite(d2[ri]) and (best is None or d2[ri] < best[0]):
                best = (float(d2[ri]), "red", ri)
        if best is None:
            return
        (vx0, vx1), _vy = self.vb.viewRange()
        tol = (vx1 - vx0) * 0.04
        if best[0] > tol * tol:
            return
        if best[1] == "series":
            name, local = best[2]
            self.lock = ("series", name, local)
            self._update_lock()
            self._zoom_lock()
            self.lockChanged.emit(name, local)
        else:
            self.lock = ("red", None, best[2])
            self._update_lock()
            self._zoom_lock()

    def lock_series_point(self, name, idx):
        self.lock = ("series", name, idx)
        self._update_lock()
        self._zoom_lock()

    def _locked_raw(self):
        if self.lock is None:
            return None
        kind, name, idx = self.lock
        if kind == "series":
            s = next((s for s in self.series if s["name"] == name), None)
            if s is not None and 0 <= idx < len(s["rx"]):
                return s["rx"][idx], s["ry"][idx], s
        elif kind == "red" and self.red is not None and 0 <= idx < len(self.red["rx"]):
            return self.red["rx"][idx], self.red["ry"][idx], None
        return None

    def _lock_label(self):
        if self.lock is None:
            return ""
        kind, name, idx = self.lock
        if kind == "series":
            s = next((s for s in self.series if s["name"] == name), None)
            if s is not None and s.get("ids") is not None:
                return f"{self.label_prefix}{s['ids'][idx]}"
            return f"{self.label_prefix}{idx}"
        if kind == "red":
            ids = self.red.get("ids") if self.red else None
            return f"矩形 {ids[idx]}" if ids is not None else "超規點"
        return ""

    def _update_lock(self):
        lr = self._locked_raw()
        if lr is None:
            self.lock_ring.setData([], [])
            self.lock_text.setText("")
            return
        lx, ly = _xform(lr[0], lr[1], self.flip, self.rot)
        self.lock_ring.setData([lx], [ly])
        self.lock_text.setText(self._lock_label())
        self.lock_text.setPos(lx, ly)

    def _zoom_lock(self):
        lr = self._locked_raw()
        if lr is None:
            return
        lx, ly = _xform(lr[0], lr[1], self.flip, self.rot)
        if not (np.isfinite(lx) and np.isfinite(ly)):
            return          # 座標為 NaN（無中心座標的矩形）不放大，避免視圖壞掉
        h = self.base_half / FOCUS_ZOOM
        self.vb.setRange(xRange=(lx - h, lx + h), yRange=(ly - h, ly + h),
                         padding=0)

    def reset(self):
        self.lock = None
        self._update_lock()
        self.reset_view()


# ===================== 可折疊檔案列（合併頁右側） =====================
class FileRow(QtWidgets.QWidget):
    def __init__(self, name, df, color, on_row, on_toggle):
        super().__init__()
        self.name, self.df, self.on_row = name, df, on_row
        self.view = None
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        head = QtWidgets.QHBoxLayout()
        sw = QtWidgets.QLabel()
        sw.setFixedSize(14, 14)
        sw.setStyleSheet(f"background:{color};border:1px solid #888;")
        head.addWidget(sw)
        self.chk = QtWidgets.QCheckBox()
        self.chk.setChecked(True)
        self.chk.toggled.connect(lambda v: on_toggle(name, v))
        head.addWidget(self.chk)
        self.btn = QtWidgets.QToolButton()
        self.btn.setText(f"▶ {name}（{len(df)} 列）")
        self.btn.setStyleSheet("text-align:left;")
        self.btn.setCheckable(True)
        self.btn.clicked.connect(self._toggle)
        head.addWidget(self.btn, 1)
        lay.addLayout(head)
        self.holder = QtWidgets.QVBoxLayout()
        lay.addLayout(self.holder)

    def _toggle(self):
        if self.btn.isChecked():
            if self.view is None:
                self.view = make_table(with_index(self.df), self.on_row)
                self.view.setMinimumHeight(320)
                # 三欄等寬、填滿、且不讓使用者調整欄寬
                self.view.horizontalHeader().setSectionResizeMode(
                    QtWidgets.QHeaderView.Stretch)
                self.holder.addWidget(self.view)
            self.view.setVisible(True)
            self.btn.setText(f"▼ {self.name}（{len(self.df)} 列）")
        else:
            if self.view is not None:
                self.view.setVisible(False)
            self.btn.setText(f"▶ {self.name}（{len(self.df)} 列）")

    def expand(self):
        if not self.btn.isChecked():
            self.btn.setChecked(True)
            self._toggle()

    def collapse(self):
        if self.btn.isChecked():
            self.btn.setChecked(False)
            self._toggle()

    def highlight(self, idx):
        self.expand()
        highlight_table_row(self.view, idx)

    def set_check(self, v):
        self.chk.setChecked(v)


# ===================== 座標檢視器分頁 =====================
class CoordTab(QtWidgets.QWidget):
    def __init__(self, datasets, combined, app, red=None, title=None):
        super().__init__()
        self.datasets = datasets
        self.combined = combined
        self.app = app
        self.red = red
        self._title = title
        self.visible = {d[0]: True for d in datasets}
        self.files = {}
        self.scroll = None
        self._anim = None
        self._anim_active = False

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(split)

        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        ctrl = QtWidgets.QHBoxLayout()
        if combined:
            ba = QtWidgets.QPushButton("✅ 全部顯示")
            ba.clicked.connect(lambda: self.set_all(True))
            ctrl.addWidget(ba)
            bh = QtWidgets.QPushButton("⬜ 全部隱藏")
            bh.clicked.connect(lambda: self.set_all(False))
            ctrl.addWidget(bh)
            if red is not None:
                # 疊合圖：把「重置」放在這個空位
                rb = QtWidgets.QPushButton("⟲ 重置")
                rb.clicked.connect(lambda: self.panel.reset())
                ctrl.addWidget(rb)
            elif getattr(app, "_drill_loaded", False):
                # 依序隱藏依 DrillDataSet 順序播放，載入 DrillDataSet 後才顯示
                self.anim_btn = QtWidgets.QPushButton("▶️ 依序隱藏")
                self.anim_btn.clicked.connect(self.toggle_anim)
                ctrl.addWidget(self.anim_btn)
        self.flip_chk = QtWidgets.QCheckBox("🔄 翻轉到背面")
        self.flip_chk.setChecked(app.flip)
        self.flip_chk.toggled.connect(self._fr)
        ctrl.addWidget(self.flip_chk)
        ctrl.addWidget(QtWidgets.QLabel("逆時針旋轉(度)"))
        self.rot_spin = QtWidgets.QDoubleSpinBox()
        self.rot_spin.setRange(-360, 360)
        self.rot_spin.setValue(app.rot)
        self.rot_spin.valueChanged.connect(self._fr)
        ctrl.addWidget(self.rot_spin)
        ctrl.addStretch(1)
        lv.addLayout(ctrl)

        if red is not None:
            # 疊合圖：不放內建重置（已移到上方工具列），並加第二行說明
            self.panel = PlotPanel(
                self._title or "全部 CSV（合併）", label_prefix="",
                show_reset=False,
                hint="點圖上的點即鎖定並放大；滾輪以游標為中心縮放、可拖曳平移\n"
                     "該疊合圖主要提供粗略觀測超規位置坐落於何微孔區")
        else:
            self.panel = PlotPanel(
                self._title or ("全部 CSV（合併）" if combined else datasets[0][0]),
                label_prefix="" if combined else "#")
        self.panel.lockChanged.connect(self._on_chart_lock)
        lv.addWidget(self.panel)
        split.addWidget(left)

        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        if combined:
            rv.addWidget(QtWidgets.QLabel("各檔案（勾選=顯示，展開點列即鎖定）"))
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            inner = QtWidgets.QWidget()
            iv = QtWidgets.QVBoxLayout(inner)
            for name, df, xc, yc, color in datasets:
                fr = FileRow(name, df, color,
                             lambda i, n=name: self._on_table(n, i),
                             self.on_toggle)
                iv.addWidget(fr)
                self.files[name] = fr
            iv.addStretch(1)
            scroll.setWidget(inner)
            self.scroll = scroll
            rv.addWidget(scroll)
        else:
            name, df, xc, yc, color = datasets[0]
            self.single = make_table(with_index(df),
                                     lambda i: self._on_table(name, i))
            self.single.horizontalHeader().setSectionResizeMode(
                QtWidgets.QHeaderView.Stretch)
            rv.addWidget(self.single)
        split.addWidget(right)
        split.setSizes([800, 460])

        self.panel.set_series(self._series())
        if self.red is not None:
            self.panel.set_red(self.red["rx"], self.red["ry"],
                               self.red.get("ids"))
        self.panel.flip = app.flip
        self.panel.rot = app.rot
        self.panel.draw()

    def _series(self):
        out = []
        for name, df, xc, yc, color in self.datasets:
            if xc is None or yc is None:
                continue
            rx = pd.to_numeric(df[xc], errors="coerce").to_numpy(float)
            ry = pd.to_numeric(df[yc], errors="coerce").to_numpy(float)
            out.append({"name": name, "rx": rx, "ry": ry, "color": color,
                        "ids": None, "visible": self.visible.get(name, True)})
        return out

    def _fr(self):
        self.app.flip = self.flip_chk.isChecked()
        self.app.rot = float(self.rot_spin.value())
        for s in self.panel.series:
            s["visible"] = self.visible.get(s["name"], True)
        self.panel.set_flip_rot(self.app.flip, self.app.rot)

    def _on_chart_lock(self, name, idx):
        if self.combined:
            self.app._last_hole = (name, idx)   # 記錄供設面板錨點
            self.app.sync_to_meas(name, idx)    # 反向鎖定量測分頁（雙向）
            target = None
            for n, fr in self.files.items():
                if n == name:
                    fr.highlight(idx)      # 展開 + 灰底標示對應列
                    target = fr
                else:
                    fr.collapse()          # 其他收合
            # 展開後把該檔案列捲進可視範圍（等版面更新完再捲）
            if target is not None and self.scroll is not None:
                QtCore.QTimer.singleShot(
                    0, lambda t=target: self._scroll_to_file(t))
        else:
            highlight_table_row(self.single, idx)

    def _scroll_to_file(self, fr):
        if self.scroll is None:
            return
        top = fr.mapTo(self.scroll.widget(), QtCore.QPoint(0, 0)).y()
        self.scroll.verticalScrollBar().setValue(max(0, top - 4))

    def lock_hole(self, name, idx):
        """外部（量測矩形對應）鎖定某區域第 idx 個孔：紅圈放大 + 展開該檔案列。"""
        self.panel.lock_series_point(name, idx)
        target = None
        for n, fr in self.files.items():
            if n == name:
                fr.highlight(idx)
                target = fr
            else:
                fr.collapse()
        if target is not None and self.scroll is not None:
            QtCore.QTimer.singleShot(0, lambda t=target: self._scroll_to_file(t))

    def _on_table(self, name, idx):
        self.panel.lock_series_point(name, idx)

    def on_toggle(self, name, vis):
        self.visible[name] = vis
        self.panel.set_series_visible(name, vis)

    def set_all(self, vis):
        for name in self.visible:
            self.visible[name] = vis
            if name in self.files:
                self.files[name].set_check(vis)
            self.panel.set_series_visible(name, vis)

    def toggle_anim(self):
        if self._anim_active:
            # 再按一次：停止並復原全部顯示
            if self._anim is not None:
                self._anim.stop()
                self._anim = None
            self.set_all(True)
            self._anim_active = False
            self.anim_btn.setText("▶️ 依序隱藏")
        else:
            self._anim_active = True
            self.anim_btn.setText("⏹ 停止依序隱藏並復原")
            self.set_all(True)
            self._seq = [d[0] for d in self.datasets]
            self._ai = 0
            self._anim = QtCore.QTimer(self)
            self._anim.timeout.connect(self._step)
            self._anim.start(HIDE_INTERVAL_MS)

    def _step(self):
        if self._ai >= len(self._seq):
            if self._anim is not None:
                self._anim.stop()
            self._anim = None
            return  # 播完保持「停止並復原」狀態，再按一次即復原
        name = self._seq[self._ai]
        self.visible[name] = False
        if name in self.files:
            self.files[name].set_check(False)
        self.panel.set_series_visible(name, False)
        self._ai += 1


# ===================== 量測檔分析分頁 =====================
class DistPanel(QtWidgets.QWidget):
    """各項分佈圖：把勾選的欄位各自用不同顏色畫在同一張圖。
    X=矩形順序（第幾個）、Y=該欄數值。滾輪縮放、拖曳平移，放大後刻度自動變細。
    點圖上的點（或點右側列表）會鎖定該矩形：紅圈標示、放大置中，並同步列表。"""

    lockChanged = QtCore.pyqtSignal(int)   # 送出被鎖定矩形的「列位置」

    def __init__(self):
        super().__init__()
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QtWidgets.QHBoxLayout()
        rb = QtWidgets.QPushButton("⟲ 重置")
        rb.clicked.connect(self.reset)
        bar.addWidget(rb)
        self.info = QtWidgets.QLabel(
            "勾選右側資料表表頭上方的欄位，再按「產生各項分佈圖」。")
        bar.addWidget(self.info)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setTitle("各項分佈圖")
        self.plot.setLabel("bottom", "矩形順序（第幾個）")
        self.plot.setLabel("left", "數值")
        self.legend = self.plot.addLegend(offset=(10, 10))
        lay.addWidget(self.plot)
        self.vb = self.plot.getViewBox()
        self._items = []
        self._gx = self._gy = self._gpos = None
        self._xr = (0.0, 1.0)
        self._yr = (0.0, 1.0)
        self.lock_ring = pg.ScatterPlotItem(
            symbol="o", size=26, brush=None, pen=pg.mkPen("r", width=2))
        self.lock_ring.setZValue(50)
        self.plot.addItem(self.lock_ring)
        self.plot.scene().sigMouseClicked.connect(self._on_click)

    def set_series(self, series):
        """series: list of (name, y(np.array), color_hex)。"""
        for it in self._items:
            self.plot.removeItem(it)   # 一併從 legend 移除
        self._items = []
        self.lock_ring.setData([], [])
        if not series:
            self.info.setText("沒有勾選任何欄位，請勾選後再按「產生各項分佈圖」。")
            self._gx = self._gy = self._gpos = None
            return
        gx, gy, gp = [], [], []
        for name, y, color in series:
            y = np.asarray(y, float)
            x = np.arange(len(y), dtype=float)
            m = ~np.isnan(y)
            it = self.plot.plot(x[m], y[m], pen=None, symbol="o",
                                symbolSize=6, symbolPen=None,
                                symbolBrush=pg.mkBrush(color), name=name)
            self._items.append(it)
            gx.append(x[m])
            gy.append(y[m])
            gp.append(x[m])
        self._gx = np.concatenate(gx) if gx else None
        self._gy = np.concatenate(gy) if gy else None
        self._gpos = (np.concatenate(gp).astype(int)
                      if gp else None)
        self.info.setText(f"已畫出 {len(series)} 個欄位（不同顏色）；"
                          "點圖上的點或右側列表即可鎖定、紅圈標示並放大。")
        self.plot.autoRange()
        (vx0, vx1), (vy0, vy1) = self.vb.viewRange()
        self._xr = (vx0, vx1)
        self._yr = (vy0, vy1)

    def _on_click(self, ev):
        try:
            if ev.button() != QtCore.Qt.LeftButton:
                return
        except Exception:  # noqa: BLE001
            return
        if self._gx is None or not len(self._gx):
            return
        pt = self.vb.mapSceneToView(ev.scenePos())
        x, y = pt.x(), pt.y()
        (vx0, vx1), (vy0, vy1) = self.vb.viewRange()
        sx = (vx1 - vx0) or 1.0
        sy = (vy1 - vy0) or 1.0
        d2 = ((self._gx - x) / sx) ** 2 + ((self._gy - y) / sy) ** 2
        gi = int(np.argmin(d2))
        if d2[gi] > 0.05 ** 2:      # 距離太遠不鎖
            return
        self._lock_xy(float(self._gx[gi]), float(self._gy[gi]))
        self.lockChanged.emit(int(self._gpos[gi]))

    def lock_pos(self, pos):
        """外部（點列表）指定鎖定某列位置：圈出該矩形的點並放大。"""
        if self._gpos is None:
            return
        idxs = np.where(self._gpos == int(pos))[0]
        if not len(idxs):
            return
        gi = int(idxs[0])
        self._lock_xy(float(self._gx[gi]), float(self._gy[gi]))

    def _lock_xy(self, lx, ly):
        self.lock_ring.setData([lx], [ly])
        xspan = (self._xr[1] - self._xr[0]) / FOCUS_ZOOM
        yspan = (self._yr[1] - self._yr[0]) / FOCUS_ZOOM
        self.vb.setRange(xRange=(lx - xspan / 2, lx + xspan / 2),
                         yRange=(ly - yspan / 2, ly + yspan / 2), padding=0)

    def reset(self):
        self.lock_ring.setData([], [])
        if self._gx is not None:
            self.vb.setRange(xRange=self._xr, yRange=self._yr, padding=0)


class HeaderCheckBar(QtWidgets.QWidget):
    """浮在 QTableView 表頭正上方、與各欄對齊的一排勾選框，用來選要畫的欄位。
    label_col 那一欄不放勾選框，改放一段（可換行、置中）說明文字。"""

    def __init__(self, view, ncols, label_col=0, label_text="", parent=None):
        super().__init__(parent)
        self.view = view
        self.header = view.horizontalHeader()
        self.ncols = ncols
        self.label_col = label_col
        self.boxes = {}          # col -> QCheckBox
        self.label = None
        for i in range(ncols):
            if i == label_col:
                self.label = QtWidgets.QLabel(label_text, self)
                self.label.setAlignment(QtCore.Qt.AlignCenter)
                self.label.setStyleSheet("color:#c8ccd0;")
            else:
                # 用「可勾選的小按鈕」當勾選框，完全不依賴主題的 checkbox 圖片，
                # 所以不會被裁成「[」。未勾=深色空框、勾=藍底。
                cb = QtWidgets.QPushButton(self)
                cb.setCheckable(True)
                cb.setCursor(QtCore.Qt.PointingHandCursor)
                cb.setToolTip("勾選以納入「各項分佈圖」（可複選）")
                cb.setStyleSheet(
                    "QPushButton{padding:0;margin:0;border:2px solid #9098a2;"
                    "border-radius:3px;background:#20242a;}"
                    "QPushButton:hover{border:2px solid #c8ccd0;}"
                    "QPushButton:checked{background:#3d6ea5;"
                    "border:2px solid #6aa9e0;}")
                self.boxes[i] = cb
        self.setFixedHeight(40)
        self.header.sectionResized.connect(self._reposition)
        self.header.sectionMoved.connect(self._reposition)
        self.header.geometriesChanged.connect(self._reposition)
        view.horizontalScrollBar().valueChanged.connect(self._reposition)
        QtCore.QTimer.singleShot(0, self._reposition)

    def _reposition(self, *a):
        W = self.width()
        H = self.height()
        for i in range(self.ncols):
            x = self.header.sectionViewportPosition(i)
            w = self.header.sectionSize(i)
            vis = w > 0 and (x + w) > 0 and x < W
            if i == self.label_col and self.label is not None:
                self.label.setGeometry(x, 0, max(w, 1), H)
                self.label.setVisible(vis)
            elif i in self.boxes:
                cb = self.boxes[i]
                cx = int(x + w / 2 - 10)
                cb.setVisible(vis)
                if vis:
                    cb.setGeometry(cx, H // 2 - 10, 20, 20)

    def resizeEvent(self, e):
        self._reposition()
        super().resizeEvent(e)

    def checked(self):
        return [i for i, cb in self.boxes.items() if cb.isChecked()]


class MeasTab(QtWidgets.QWidget):
    def __init__(self, mdf, app, fname=""):
        super().__init__()
        self.mdf = mdf
        self.app = app
        self.fname = fname
        self.ids = mdf["矩形編號"].tolist()
        self.xv = pd.to_numeric(mdf["X中心座標(量測值)"], errors="coerce").to_numpy(float)
        self.yv = pd.to_numeric(mdf["Y中心座標(量測值)"], errors="coerce").to_numpy(float)
        self.over = np.zeros(len(mdf), bool)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(4)
        big_btn = (
            "QPushButton{background:#2a2e34;border:1px solid #4a90d9;"
            "border-radius:12px;padding:12px 18px;font-weight:bold;}"
            "QPushButton:hover{background:#343b44;}")

        # 左邊：子分頁（量測中心座標分佈圖 / 各項分佈圖），只切換左半邊；
        #       右邊：所有控制項 + 資料表 疊成一欄，不隨子分頁切換
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.left_tabs = QtWidgets.QTabWidget()
        # 讓分頁標籤明顯像「可切換的分頁」
        self.left_tabs.setStyleSheet(
            "QTabWidget::pane{border:1px solid #454b54;top:-1px;}"
            "QTabBar::tab{background:#2a2e34;color:#c8ccd0;"
            "border:1px solid #454b54;border-bottom:none;"
            "border-top-left-radius:6px;border-top-right-radius:6px;"
            "padding:8px 18px;margin-right:4px;font-weight:bold;font-size:13px;}"
            "QTabBar::tab:selected{background:#3d6ea5;color:white;}"
            "QTabBar::tab:hover{background:#343b44;}")
        self.panel = PlotPanel("量測中心座標分布圖（量測值）", label_prefix="矩形 ")
        self.panel.lockChanged.connect(self._on_chart_lock)
        self.dist = DistPanel()
        self.dist.lockChanged.connect(self._on_dist_lock)
        self.left_tabs.addTab(self.panel, "📈 量測中心座標分佈圖")
        self.left_tabs.addTab(self.dist, "📊 各項分佈圖")
        self.left_tabs.setCurrentIndex(0)
        split.addWidget(self.left_tabs)

        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(6)

        # 檔案資訊 + 旋轉
        top = QtWidgets.QHBoxLayout()
        lbl = QtWidgets.QLabel(f"<b>檔案：{fname}</b>　已解析 {len(mdf)} 個矩形")
        lbl.setTextFormat(QtCore.Qt.RichText)
        top.addWidget(lbl)
        top.addStretch(1)
        top.addWidget(QtWidgets.QLabel("逆時針旋轉(度)"))
        self.rot_spin = QtWidgets.QDoubleSpinBox()
        self.rot_spin.setRange(-360, 360)
        self.rot_spin.valueChanged.connect(self.apply)
        top.addWidget(self.rot_spin)
        rv.addLayout(top)

        # 超規標紅設定：X中心/Y中心 第一列；間距/高度 換到第二列且對齊 X中心
        _spec_lbl = QtWidgets.QLabel(
            "🔴 超規紅標（輸入單位：mm；欄位輸入 0＝不檢查）　"
            "X/Y中心超規判定：量測值−CAD值＞上限、量測值−CAD值＜下限；　"
            "間距/高度超規判定：量測值＞上限、量測值＜下限")
        _spec_lbl.setWordWrap(True)
        rv.addWidget(_spec_lbl)
        sg = QtWidgets.QGridLayout()
        sg.setContentsMargins(0, 0, 0, 0)
        sg.setHorizontalSpacing(6)
        sg.setVerticalSpacing(4)
        self.bounds = {}
        items = [("X中心", "X中心座標(量測值-CAD值)", "diff", 0, 0),
                 ("Y中心", "Y中心座標(量測值-CAD值)", "diff", 0, 6),
                 ("間距", "間距(量測值)", "abs", 1, 0),
                 ("高度", "高度(量測值)", "abs", 1, 6)]
        for lab, col, mode, r, c in items:
            sg.addWidget(QtWidgets.QLabel(lab + "："), r, c)
            sg.addWidget(QtWidgets.QLabel("上限"), r, c + 1)
            up = QtWidgets.QLineEdit("0")
            up.setFixedWidth(70)
            up.setToolTip(f"{lab} 上限：超過則標紅")
            sg.addWidget(up, r, c + 2)
            sg.addWidget(QtWidgets.QLabel("下限"), r, c + 3)
            lo = QtWidgets.QLineEdit("0")
            lo.setFixedWidth(70)
            lo.setToolTip(f"{lab} 下限：低於則標紅")
            sg.addWidget(lo, r, c + 4)
            self.bounds[col] = (up, lo, mode)
        # 每列左項結束後放「；」再接右項
        sep0 = QtWidgets.QLabel("；")
        sep1 = QtWidgets.QLabel("；")
        sg.addWidget(sep0, 0, 5)
        sg.addWidget(sep1, 1, 5)
        ap = QtWidgets.QPushButton("套用標紅")
        ap.setStyleSheet(big_btn)
        ap.clicked.connect(self.apply)
        sg.addWidget(ap, 0, 11, 2, 1)      # 跨兩列，放在右側
        sg.setColumnStretch(12, 1)
        rv.addLayout(sg)

        # 兩個大圓角按鈕各佔一半：產生各項分佈圖（左） / 產生疊合圖（右）
        btn_row = QtWidgets.QHBoxLayout()
        bdist = QtWidgets.QPushButton("📊 產生各項分佈圖（勾選欄位）")
        bdist.setStyleSheet(big_btn)
        bdist.setMinimumHeight(52)
        bdist.clicked.connect(self._make_dist)
        btn_row.addWidget(bdist, 1)
        self.bov = QtWidgets.QPushButton("🟥 產生疊合圖（紅點疊到座標檢視器）")
        self.bov.setStyleSheet(big_btn)
        self.bov.setMinimumHeight(52)
        self.bov.clicked.connect(lambda: app.show_overlay(self))
        btn_row.addWidget(self.bov, 1)
        rv.addLayout(btn_row)

        # 分面板錨點引導：每個面板各設一個錨點後，才能產生疊合圖
        self._panels = _cluster_blocks(self.xv, self.yv)
        self.n_panels = max(len(self._panels), 1)
        self.anchor_hint = QtWidgets.QLabel()
        self.anchor_hint.setWordWrap(True)
        self.anchor_hint.setStyleSheet(
            "background:#2a2e34;border:1px solid #4a90d9;border-radius:6px;"
            "padding:6px;")
        rv.addWidget(self.anchor_hint)
        self.refresh_anchor_state()

        # 資料表：表頭上方每欄一個勾選框；「矩形編號」那欄改放「資料表(點列即鎖定)」說明。
        # 欄位改為等寬填滿（不橫向捲動），確保每一欄的勾選框都看得到。
        self.table = make_table(mdf, self._on_table)
        hh = self.table.horizontalHeader()
        # 欄寬自動依內容調整（表頭文字不被切到）；不夠寬時橫向捲動，勾選框會跟著捲
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        # 「矩形編號」欄固定加寬，讓上方「資料表(點列即鎖定)」以正常字級塞得下
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.Fixed)
        hh.resizeSection(0, 104)
        # 點表頭 → 底部顯示該欄平均值
        hh.setSectionsClickable(True)
        hh.sectionClicked.connect(self._on_header_clicked)
        self.checkbar = HeaderCheckBar(
            self.table, len(mdf.columns), label_col=0,
            label_text="資料表\n(點列即鎖定)")
        rv.addWidget(self.checkbar)
        rv.addWidget(self.table, 1)
        self.info = QtWidgets.QLabel("")
        self.info.setWordWrap(True)
        rv.addWidget(self.info)

        split.addWidget(right)
        # 左右固定各半、隨視窗等比例縮放，且不讓使用者拖動分隔線
        split.setSizes([1000, 1000])
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setChildrenCollapsible(False)
        _h = split.handle(1)
        if _h is not None:
            _h.setEnabled(False)
        lay.addWidget(split, 1)

        self.apply()

    def _anchored_panels(self):
        """回傳這個量測檔已被錨定的面板編號集合。"""
        done = set()
        for (mt, rid, region, hidx) in self.app._anchors:
            if mt is not self or rid not in self.ids:
                continue
            p = self.ids.index(rid)
            x, y = self.xv[p], self.yv[p]
            for bi, b in enumerate(self._panels):
                if (b["xlo"] - 0.3 <= x <= b["xhi"] + 0.3
                        and b["ylo"] - 0.3 <= y <= b["yhi"] + 0.3):
                    done.add(bi)
                    break
        return done

    def refresh_anchor_state(self):
        """依已錨定面板數，更新引導提示與『產生疊合圖』按鈕的可用狀態。"""
        done = len(self._anchored_panels())
        ok = done >= self.n_panels
        self.bov.setEnabled(ok)
        if ok:
            self.anchor_hint.setText(
                f"✅ 已錨定 {done}/{self.n_panels} 塊面板——可以按"
                "「🟥 產生疊合圖」了。")
            self.anchor_hint.setStyleSheet(
                "background:#22331f;border:1px solid #4caf50;"
                "border-radius:6px;padding:6px;")
        else:
            self.anchor_hint.setText(
                f"🔒 產生疊合圖前，請為每個面板各設一個錨點"
                f"（已完成 {done}/{self.n_panels} 塊）。\n"
                "步驟：① 在上方『量測中心座標分佈圖』點該面板一個矩形"
                "（角落最好）→ ② 切到『合併顯示』點它對應的孔"
                "→ ③ 按工具列「➕ 設面板錨點」。每塊各做一次。")
            self.anchor_hint.setStyleSheet(
                "background:#33291f;border:1px solid #d0a020;"
                "border-radius:6px;padding:6px;")

    def _make_dist(self):
        """把勾選的欄位各自用不同顏色畫進「各項分佈圖」，並切到該子分頁。
        右半邊（控制項/資料表）不切換。"""
        cols = self.checkbar.checked()
        self.left_tabs.setCurrentWidget(self.dist)
        if not cols:
            self.dist.set_series([])
            return
        names = list(self.mdf.columns)
        pal = make_palette(len(cols))
        series = []
        for j, ci in enumerate(cols):
            name = names[ci]
            y = pd.to_numeric(self.mdf[name], errors="coerce").to_numpy(float)
            series.append((name, y, pal[j % len(pal)]))
        self.dist.set_series(series)

    @staticmethod
    def _pf(s):
        try:
            return float(str(s).strip())
        except Exception:  # noqa: BLE001
            return 0.0

    def _over_mask(self):
        over = np.zeros(len(self.mdf), bool)
        active = False
        for col, (up_w, lo_w, mode) in self.bounds.items():
            up = self._pf(up_w.text())
            lo = self._pf(lo_w.text())
            if mode == "abs":
                # 間距/高度：量測值直接和上下限比較（0 = 不檢查該方向）
                val = pd.to_numeric(self.mdf[col], errors="coerce").to_numpy(float)
                if up != 0:
                    active = True
                    over = over | (val > up)
                if lo != 0:
                    active = True
                    over = over | (val < lo)
            else:
                # X/Y 中心：量測−CAD 的偏差和上下限比較（上限填正、下限填負）
                dev = pd.to_numeric(self.mdf[col], errors="coerce").to_numpy(float)
                if up > 0:
                    active = True
                    over = over | (dev > up)
                if lo < 0:
                    active = True
                    over = over | (dev < lo)
        return over, active

    def apply(self):
        self.app.meas_rot = float(self.rot_spin.value())
        over, active = self._over_mask()
        self.over = over
        normal = ~over
        series = []
        if normal.any():
            series.append({"name": "正常", "rx": self.xv[normal],
                           "ry": self.yv[normal], "color": "#1f77b4",
                           "ids": [self.ids[i] for i in np.where(normal)[0]],
                           "visible": True})
        if over.any():
            series.append({"name": "超規", "rx": self.xv[over],
                           "ry": self.yv[over], "color": "#d62728",
                           "ids": [self.ids[i] for i in np.where(over)[0]],
                           "visible": True})
        if not series:
            series = [{"name": "全部", "rx": self.xv, "ry": self.yv,
                       "color": "#1f77b4", "ids": self.ids, "visible": True}]
        self.panel.set_series(series)
        self.panel.flip = False
        self.panel.rot = self.app.meas_rot
        self.panel.draw()
        self.info.setText(
            f"超出規格 {int(over.sum())} / {len(self.mdf)} 個（紅）" if active
            else "未設定門檻")

    def _lock_meas_panel(self, pos):
        """鎖定量測中心座標分佈圖到第 pos 列（找到對應矩形編號的系列點）。"""
        rid = self.ids[pos]
        for s in self.panel.series:
            ids = s.get("ids")
            if ids is not None and rid in ids:
                self.panel.lock_series_point(s["name"], ids.index(rid))
                break

    def _on_chart_lock(self, name, idx):
        # 量測中心座標分佈圖被點 → 同步列表 + 各項分佈圖
        s = next((s for s in self.panel.series if s["name"] == name), None)
        if s is None or s.get("ids") is None:
            return
        rid = s["ids"][idx]
        if rid in self.ids:
            pos = self.ids.index(rid)
            highlight_table_row(self.table, pos)
            self.dist.lock_pos(pos)
            self._show(pos)
            self.app.sync_to_combined(self, pos)   # 同步鎖定合併顯示對應孔

    def _on_dist_lock(self, pos):
        # 各項分佈圖被點 → 同步列表 + 量測中心座標分佈圖
        if not (0 <= pos < len(self.ids)):
            return
        highlight_table_row(self.table, pos)
        self._lock_meas_panel(pos)
        self._show(pos)
        self.app.sync_to_combined(self, pos)

    def _on_table(self, idx):
        # 點列表 → 同步兩張圖（紅圈 + 放大）
        self._lock_meas_panel(idx)
        self.dist.lock_pos(idx)
        self._show(idx)
        self.app.sync_to_combined(self, idx)

    def lock_from_external(self, pos):
        """由合併顯示點孔反查而來，鎖定本量測分頁（不回呼合併顯示避免循環）。"""
        if not (0 <= pos < len(self.ids)):
            return
        self._lock_meas_panel(pos)
        self.dist.lock_pos(pos)
        highlight_table_row(self.table, pos)
        self._show(pos)

    def _on_header_clicked(self, section):
        cols = list(self.mdf.columns)
        if not (0 <= section < len(cols)):
            return
        name = cols[section]
        if section == 0:      # 矩形編號欄：顯示矩形總數
            self.info.setText(f"📊 {name}：共 {len(self.mdf)} 個矩形")
            return
        vals = pd.to_numeric(self.mdf[name], errors="coerce")
        n = int(vals.notna().sum())
        if n == 0:
            self.info.setText(f"📊 {name}：此欄無有效數值")
            return
        self.info.setText(
            f"📊 {name}　平均 = {vals.mean():.4f}　"
            f"（有效 {n} 筆；最小 {vals.min():.4f}、最大 {vals.max():.4f}）")

    def _show(self, pos):
        r = self.mdf.iloc[pos]

        def f(v):
            try:
                return f"{float(v):.4f}"
            except Exception:  # noqa: BLE001
                return "—"
        self.info.setText(
            f"🔒 矩形 {r['矩形編號']}　"
            f"X中心={f(r['X中心座標(量測值)'])}(差 {f(r['X中心座標(量測值-CAD值)'])})  "
            f"Y中心={f(r['Y中心座標(量測值)'])}(差 {f(r['Y中心座標(量測值-CAD值)'])})  "
            f"間距={f(r['間距(量測值)'])}(差 {f(r['間距(量測值-CAD值)'])})  "
            f"高度={f(r['高度(量測值)'])}(差 {f(r['高度(量測值-CAD值)'])})")


# ===================== 疊合圖分頁 =====================
class OverlayTab(QtWidgets.QWidget):
    def __init__(self, loaded, mtab, over, app):
        super().__init__()
        lay = QtWidgets.QVBoxLayout(self)
        self.panel = PlotPanel("座標檢視器 ＋ 超規紅點", label_prefix="")
        lay.addWidget(self.panel)

        cvx, cvy, series = [], [], []
        for name, df, xc, yc, color in loaded:
            if xc is None or yc is None:
                continue
            rx = pd.to_numeric(df[xc], errors="coerce").to_numpy(float)
            ry = pd.to_numeric(df[yc], errors="coerce").to_numpy(float)
            series.append({"name": name, "rx": rx, "ry": ry, "color": color,
                           "ids": None, "visible": True})
            cvx += rx[~np.isnan(rx)].tolist()
            cvy += ry[~np.isnan(ry)].tolist()
        f = align_measurement_to_cv(mtab.xv, mtab.yv, cvx, cvy)
        rids = [mtab.ids[i] for i in np.where(over)[0]]
        mrx, mry = [], []
        for x, y in zip(mtab.xv[over], mtab.yv[over]):
            a, b = f(x, y)
            mrx.append(a)
            mry.append(b)
        self.panel.set_series(series)
        self.panel.set_red(mrx, mry, rids)
        self.panel.flip = app.flip
        self.panel.rot = app.rot
        self.panel.draw()
        self.panel.info.setText(
            f"超規紅點 {len(mrx)} 個，已自動對映到座標檢視器（含鏡像偵測）；"
            "點圖鎖定、滾輪縮放；翻轉/旋轉沿用座標檢視器。")


# ===================== 延遲分頁 =====================
class LazyTab(QtWidgets.QWidget):
    def __init__(self, builder):
        super().__init__()
        self._builder = builder
        self._built = False
        self.inner = None
        self._lay = QtWidgets.QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)

    def build(self):
        if not self._built:
            self._built = True
            self.inner = self._builder()
            self._lay.addWidget(self.inner)
        return self.inner


# ===================== 主視窗 =====================
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("座標檢視器（桌面版）")
        self.resize(1320, 860)
        self.datasets = []
        self.meas_files = []      # list of (檔名, mdf)
        self.flip = False
        self.rot = 0.0
        self.meas_rot = 0.0
        self._combined_lazy = None   # 合併顯示的 LazyTab
        self._sync = None            # 疊合圖對齊參數（供量測→合併孔對應）
        self._anchors = []           # 分面板錨點：(meastab, rid, region, hole_idx)
        self._last_meas = None       # 最近點選的量測矩形 (meastab, rid)
        self._last_hole = None       # 最近點選的合併顯示孔 (region, idx)
        self._meas_tabs = []         # 目前的量測分頁（供更新錨點狀態）
        self._drill_loaded = False   # 是否已載入 DrillDataSet（依序隱藏才顯示）

        tb = self.addToolBar("main")
        tb.addAction("📁 上傳座標 CSV（可多選）", self.load_files)
        tb.addAction("📑 上傳 DrillDataSet（排序）", self.load_drill)
        tb.addAction("📐 上傳量測原始檔", self.load_meas)
        tb.addAction("清除全部", self.clear_all)
        tb.addSeparator()
        tb.addAction("➕ 設面板錨點", self.add_anchor)
        tb.addAction("🧹 清除錨點", self.clear_anchors)

        central = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(central)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(4)
        # 可橫向捲動的分頁按鈕列（卷軸可拖曳）
        self.sel = QtWidgets.QScrollArea()
        self.sel.setFixedHeight(48)
        self.sel.setWidgetResizable(False)
        self.sel.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.sel.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        inner = QtWidgets.QWidget()
        self.sel_lay = QtWidgets.QHBoxLayout(inner)
        self.sel_lay.setContentsMargins(2, 2, 2, 2)
        self.sel_lay.setSpacing(3)
        self.sel_lay.setSizeConstraint(QtWidgets.QLayout.SetMinimumSize)
        self.sel.setWidget(inner)
        v.addWidget(self.sel)
        self.stack = QtWidgets.QStackedWidget()
        v.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.btn_group = QtWidgets.QButtonGroup(self)
        self.btn_group.setExclusive(True)
        self._page_btn = {}

        self.status = self.statusBar()
        self.status.showMessage("請上傳座標 CSV 或量測原始檔")
        self.rebuild()

    def _clear_pages(self):
        for b in list(self.btn_group.buttons()):
            self.btn_group.removeButton(b)
            self.sel_lay.removeWidget(b)
            b.deleteLater()
        while self.stack.count():
            w = self.stack.widget(0)
            self.stack.removeWidget(w)
            w.deleteLater()
        self._page_btn = {}

    def add_page(self, title, page, pos=None, select=False):
        btn = QtWidgets.QPushButton(title)
        btn.setCheckable(True)
        btn.setCursor(QtCore.Qt.PointingHandCursor)
        btn.setStyleSheet(
            "QPushButton{padding:5px 12px;border-radius:4px;}"
            "QPushButton:checked{background:#3d6ea5;color:white;"
            "font-weight:bold;}")
        self.btn_group.addButton(btn)
        self.stack.addWidget(page)
        self._page_btn[page] = btn
        if pos is None:
            self.sel_lay.addWidget(btn)
        else:
            self.sel_lay.insertWidget(pos, btn)
        btn.clicked.connect(lambda: self._select(page))
        if select:
            self._select(page)
        return btn

    def _select(self, page):
        if isinstance(page, LazyTab):
            page.build()
        self.stack.setCurrentWidget(page)
        btn = self._page_btn.get(page)
        if btn is not None:
            btn.setChecked(True)

    def _warn(self, title, text):
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle(title)
        box.setText(text)
        box.exec_()

    def load_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "選擇座標 CSV", "", "CSV (*.csv);;All (*)")
        if not paths:
            return
        errs = []
        for p in paths:
            name = os.path.basename(p)
            try:
                df = read_csv(p)
            except Exception as e:  # noqa: BLE001
                errs.append(f"• {name}\n    讀取失敗：{e}")
                continue
            xc, yc = find_xy_columns(df)
            if xc is None or yc is None:
                cols = "、".join(str(c) for c in list(df.columns)[:8]) or "（無）"
                errs.append(f"• {name}\n    找不到 X／Y 座標欄位。"
                            f"現有欄位：{cols}")
                continue
            self.datasets.append((name, df, xc, yc, "#1f77b4"))
        self._drill_loaded = False   # 新載入的座標打亂順序，需重新載 DrillDataSet
        self._recolor()
        self.rebuild()
        if errs:
            self._warn("座標 CSV 載入失敗", "以下檔案無法載入：\n\n" + "\n".join(errs))

    def load_drill(self):
        if not self.datasets:
            self._warn("請先上傳座標 CSV",
                       "要依 DrillDataSet 排序前，請先上傳至少一個座標 CSV。")
            return
        p, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "選擇 DrillDataSet", "", "CSV (*.csv);;All (*)")
        if not p:
            return
        order = parse_drill_order(p)
        if not order:
            self._warn("DrillDataSet 載入失敗",
                       f"{os.path.basename(p)}\n\n"
                       "讀不到 ArrayName 或 FilePath 欄位，無法取得加工順序。\n"
                       "請確認這是 DrillDataSet 檔（需含 ArrayName 或 FilePath 欄）。")
            return
        rank = {nm: i for i, nm in enumerate(order)}
        matched = sum(1 for d in self.datasets if _basename(d[0]) in rank)
        if matched == 0:
            self._warn(
                "DrillDataSet 與座標檔對不上",
                f"{os.path.basename(p)} 讀到 {len(order)} 個區域，但沒有一個"
                "和目前載入的座標檔名對得上，順序未套用、依序隱藏也不會啟用。\n\n"
                f"DrillDataSet 的區域名（例）：{order[0]}\n"
                f"載入的座標檔名（例）：{_basename(self.datasets[0][0])}\n\n"
                "請確認 DrillDataSet 的區域名與座標 CSV 檔名一致"
                "（例如同一組 89x70_250um-… ）。")
            return
        self.datasets.sort(key=lambda d: rank.get(_basename(d[0]), len(rank) + 1))
        self._recolor()
        self._drill_loaded = True    # 依序隱藏按鈕解鎖
        self.rebuild()
        note = "" if matched == len(self.datasets) else \
            f"（{len(self.datasets) - matched} 個座標檔在 DrillDataSet 找不到，排在最後）"
        self.status.showMessage(
            f"已依 DrillDataSet 排序（{matched}/{len(self.datasets)}）{note}")

    def _recolor(self):
        pal = make_palette(len(self.datasets))
        self.datasets = [(n, df, xc, yc, pal[i % len(pal)])
                         for i, (n, df, xc, yc, _c) in enumerate(self.datasets)]

    def load_meas(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "選擇量測原始檔（可多選）", "",
            "量測檔 (*.csv *.xlsx *.xlsm *.xls);;CSV (*.csv);;"
            "Excel (*.xlsx *.xlsm *.xls);;所有檔案 (*)")
        if not paths:
            return
        errs = []
        for p in paths:
            name = os.path.basename(p)
            try:
                mdf = parse_measurement_file(p)
            except Exception as e:  # noqa: BLE001
                errs.append(f"• {name}\n    解析發生錯誤：{e}")
                continue
            if mdf.empty:
                errs.append(f"• {name}\n    {diagnose_meas(p)}")
                continue
            self.meas_files.append((name, mdf))
        self.status.showMessage(f"已載入 {len(self.meas_files)} 個量測檔")
        self.rebuild()
        if errs:
            self._warn("量測原始檔載入失敗", "以下檔案無法載入：\n\n" + "\n".join(errs))

    def clear_all(self):
        self.datasets = []
        self.meas_files = []
        self._drill_loaded = False
        self.rebuild()

    def show_overlay(self, meastab):
        if not self.datasets:
            self.status.showMessage("疊合圖需要先上傳座標 CSV")
            return
        # 收集所有孔座標 + 每個孔所屬區域（供配準與矩形→孔對應）
        hx, hy, hmeta = [], [], []
        for nm, df, xc, yc, _c in self.datasets:
            if xc is None or yc is None:
                continue
            xa = pd.to_numeric(df[xc], errors="coerce").to_numpy(float)
            ya = pd.to_numeric(df[yc], errors="coerce").to_numpy(float)
            hx.append(xa)
            hy.append(ya)
            hmeta.extend((nm, j) for j in range(len(xa)))
        if not hx:
            self.status.showMessage("座標 CSV 沒有可用的 X/Y 欄")
            return
        hx = np.concatenate(hx)
        hy = np.concatenate(hy)
        # 解析此量測檔的面板錨點（量測矩形 ↔ 孔）
        anchors = self._resolve_anchors(meastab)
        if anchors:
            self.status.showMessage(f"分面板錨點對位中…（{len(anchors)} 個錨點）")
            QtWidgets.QApplication.processEvents()
            T = register_meas_panels(meastab.xv, meastab.yv, hx, hy, anchors)
            info = (f"使用 {len(anchors)} 個面板錨點分面板對位。\n\n"
                    "每個面板依它的錨點各自對齊。要產生疊合圖嗎？")
            fmsg = f"分面板錨點對位（{len(anchors)} 錨點）"
        else:
            # 自動配準：找旋轉(+水平翻轉+微平移)讓最多量測點落在孔上
            self.status.showMessage("自動配準中…")
            QtWidgets.QApplication.processEvents()
            reg = register_meas_to_cv(meastab.xv, meastab.yv, hx, hy)
            if reg is None:
                self._warn("疊合圖", "無法配準（資料不足）。")
                return
            T = reg["T"]
            frac = reg["frac"]
            info = (f"自動配準結果：\n\n"
                    f"　旋轉 {reg['deg']:.2f}°、"
                    f"{'需水平翻轉' if reg['flip'] else '不需翻轉'}\n"
                    f"　擬合率 {frac * 100:.1f}%（量測點落在孔上的比例）\n\n")
            if frac >= 0.75:
                info += "擬合良好，要產生疊合圖嗎？"
            else:
                info += ("⚠ 擬合率偏低（未達 75%）——密集孔陣列自動配準可能"
                         "選錯相位（上半對、下半錯）。\n若如此，請用工具列的"
                         "「➕ 設面板錨點」為每個面板各設一個錨點再產生。仍要產生嗎？")
            fmsg = f"自動配準 擬合率 {frac * 100:.1f}%"
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setWindowTitle("產生疊合圖")
        box.setText(info)
        box.setStandardButtons(
            QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(QtWidgets.QMessageBox.Ok)
        box.button(QtWidgets.QMessageBox.Ok).setText("確定")
        box.button(QtWidgets.QMessageBox.Cancel).setText("取消")
        if box.exec_() != QtWidgets.QMessageBox.Ok:
            self.status.showMessage("已取消")
            return
        # 移除舊的疊合圖頁
        for page, btn in list(self._page_btn.items()):
            if btn.text() == "🟥 疊合圖":
                self.btn_group.removeButton(btn)
                self.sel_lay.removeWidget(btn)
                btn.deleteLater()
                self.stack.removeWidget(page)
                page.deleteLater()
                del self._page_btn[page]
                break
        # 超規紅點 = 轉換後的座標
        over = meastab.over
        idxs = np.where(over)[0]
        rids = [meastab.ids[i] for i in idxs]
        rx, ry = T(meastab.xv[over], meastab.yv[over])
        red = {"rx": np.asarray(rx, float), "ry": np.asarray(ry, float),
               "ids": rids}
        page = CoordTab(self.datasets, True, self, red=red,
                        title="座標檢視器 ＋ 超規紅點")
        self.add_page("🟥 疊合圖", page, pos=1, select=True)
        self.status.showMessage(f"疊合圖：{fmsg}、超規紅點 {len(rids)} 個")
        # 快取轉換 + 每個量測矩形對映到的孔座標（供雙向鎖定）
        mcx, mcy = T(meastab.xv, meastab.yv)
        self._sync = {"meastab": meastab, "T": T,
                      "hx": hx, "hy": hy, "hmeta": hmeta,
                      "mcx": np.asarray(mcx, float),
                      "mcy": np.asarray(mcy, float)}

    def _resolve_anchors(self, meastab):
        """把此 meastab 的錨點解析成 (ax, ay, cvx, cvy) 清單。"""
        out = []
        for (mt, rid, region, hidx) in self._anchors:
            if mt is not meastab or rid not in meastab.ids:
                continue
            p = meastab.ids.index(rid)
            ds = next((d for d in self.datasets if d[0] == region), None)
            if ds is None:
                continue
            _n, df, xc, yc, _c = ds
            if xc is None or yc is None or not (0 <= hidx < len(df)):
                continue
            cvx = float(pd.to_numeric(df[xc], errors="coerce").iloc[hidx])
            cvy = float(pd.to_numeric(df[yc], errors="coerce").iloc[hidx])
            out.append((float(meastab.xv[p]), float(meastab.yv[p]), cvx, cvy))
        return out

    def add_anchor(self):
        """把最近點選的量測矩形 ↔ 合併顯示孔，設為該面板的錨點。"""
        if self._last_meas is None or self._last_hole is None:
            self._warn("設面板錨點",
                       "步驟：\n1. 在『量測中心座標分佈圖』點一個矩形\n"
                       "2. 到『合併顯示』點它對應的孔\n3. 再按「➕ 設面板錨點」")
            return
        mtab, rid = self._last_meas
        region, idx = self._last_hole
        self._anchors = [a for a in self._anchors
                         if not (a[0] is mtab and a[1] == rid)]
        self._anchors.append((mtab, rid, region, idx))
        self._refresh_anchor_states()
        self.status.showMessage(
            f"已設錨點：矩形 {rid} ↔ {region} 第 {idx} 孔"
            f"（共 {len(self._anchors)} 個）。每個面板各設一個後按「產生疊合圖」。")

    def clear_anchors(self):
        self._anchors = []
        self._refresh_anchor_states()
        self.status.showMessage("已清除所有面板錨點")

    def _refresh_anchor_states(self):
        for mt in getattr(self, "_meas_tabs", []):
            try:
                mt.refresh_anchor_state()
            except Exception:  # noqa: BLE001
                pass

    def sync_to_combined(self, meastab, pos):
        """點量測矩形 → 用配準轉換換算座標、找最近孔、在合併顯示鎖定對應孔。"""
        if 0 <= pos < len(meastab.ids):
            self._last_meas = (meastab, meastab.ids[pos])   # 記錄供設錨點
        s = self._sync
        if s is None or s.get("meastab") is not meastab or s.get("hx") is None:
            return
        if not (0 <= pos < len(meastab.xv)):
            return
        mx, my = meastab.xv[pos], meastab.yv[pos]
        if not (np.isfinite(mx) and np.isfinite(my)):
            return
        cx, cy = s["T"](mx, my)
        cx, cy = float(cx), float(cy)
        d2 = (s["hx"] - cx) ** 2 + (s["hy"] - cy) ** 2
        d2 = np.where(np.isfinite(d2), d2, np.inf)
        k = int(np.argmin(d2))
        if not np.isfinite(d2[k]):
            return
        name, idx = s["hmeta"][k]
        ct = self._combined_lazy.build() if self._combined_lazy else None
        if ct is None:
            return
        ct.lock_hole(name, idx)
        rid = meastab.ids[pos]
        self.status.showMessage(
            f"矩形 {rid} → {name} 第 {idx} 個孔（合併顯示已鎖定）")

    def sync_to_meas(self, region, idx):
        """點合併顯示的孔 → 反查最近的量測矩形，鎖定量測分頁（雙向鎖定）。"""
        s = self._sync
        if s is None or s.get("mcx") is None:
            return
        ds = next((d for d in self.datasets if d[0] == region), None)
        if ds is None:
            return
        _n, df, xc, yc, _c = ds
        if xc is None or yc is None or not (0 <= idx < len(df)):
            return
        hx0 = float(pd.to_numeric(df[xc], errors="coerce").iloc[idx])
        hy0 = float(pd.to_numeric(df[yc], errors="coerce").iloc[idx])
        d2 = (s["mcx"] - hx0) ** 2 + (s["mcy"] - hy0) ** 2
        d2 = np.where(np.isfinite(d2), d2, np.inf)
        pos = int(np.argmin(d2))
        if not np.isfinite(d2[pos]):
            return
        meastab = s["meastab"]
        meastab.lock_from_external(pos)
        rid = meastab.ids[pos]
        self.status.showMessage(
            f"{region} 第 {idx} 個孔 → 矩形 {rid}（量測分頁已鎖定）")

    def rebuild(self):
        self._clear_pages()
        self._combined_lazy = None
        self._sync = None
        self._meas_tabs = []
        first = None
        for fname, mdf in self.meas_files:
            mt = MeasTab(mdf, self, fname)
            self._meas_tabs.append(mt)
            self.add_page("📐 " + fname, mt)
            if first is None:
                first = mt
        if self.datasets:
            merged = LazyTab(lambda: CoordTab(self.datasets, True, self))
            self._combined_lazy = merged
            self.add_page("🔗 合併顯示", merged)
            if first is None:
                first = merged
            for d in self.datasets:
                self.add_page(d[0],
                              LazyTab(lambda d=d: CoordTab([d], False, self)))
        if self.stack.count() == 0:
            hint = QtWidgets.QWidget()
            grid = QtWidgets.QGridLayout(hint)
            grid.setContentsMargins(28, 18, 28, 18)
            grid.setHorizontalSpacing(40)
            grid.setVerticalSpacing(20)
            style = ("font-size:14px; line-height:1.7; background:#23262b;"
                     "border:1px solid #3a3f47; border-radius:8px;")

            def cell(html):
                w = QtWidgets.QLabel(
                    f"<div style='{style} padding:12px;'>{html}</div>")
                w.setTextFormat(QtCore.Qt.RichText)
                w.setWordWrap(True)
                w.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
                return w

            tl = cell(
                "<b>📁 上傳座標 CSV（可多選）</b><br>"
                "上傳 LCH 加工 case 的微孔座標檔案，位於 ProgramObjects "
                "資料夾內（ex：69x71_250um-aliceblue.csv）。<br><br>"
                "合併顯示或各檔分頁檢視；點圖上的點或右側列表即可鎖定、"
                "放大並紅圈標示，右側清單會自動展開並捲到對應列。")
            tr = cell(
                "<b>📑 上傳 DrillDataSet（排序）</b><br>"
                "DrillDataSet.csv 是 LCH 微孔加工區域的順序，"
                "可自行排定加工順序，調換欄位即可。")
            bl = cell(
                "<b>📐 上傳量測原始檔（可多選，支援 .csv 與 .xlsx）</b><br>"
                "台超量測產出的原始檔直接載入即可，可一次選多個檔"
                "（每個檔一個分頁）。CSV 原始檔為 UTF-16／Tab 分隔，"
                "被 Excel 另存成 Big5／逗號或 .xlsx 也會自動辨識；"
                "載入後依矩形編號排序，載入失敗會跳出對話框說明原因。<br><br>"
                "分析頁含兩個子分頁：<b>量測中心座標分佈圖</b>（可旋轉、"
                "鎖定放大、超規標紅）與 <b>各項分佈圖</b>（勾選欄位，"
                "各欄用不同顏色畫在同一張圖）。")
            br = cell(
                "<b>🟥 產生疊合圖（把量測結果疊到座標檢視器上）</b><br>"
                "量測資料通常分成上下兩塊面板。因為孔陣列密集又對稱，"
                "電腦無法自己分辨正確的對位（會有整排位移的歧義），"
                "所以要你<b>用眼睛替每個面板各指定一個「錨點」</b>：<br>"
                "　① 在『量測中心座標分佈圖』點該面板一個矩形（角落最好）<br>"
                "　② 切到『合併顯示』點它對應的那個孔<br>"
                "　③ 按工具列「➕ 設面板錨點」<br>"
                "上下兩塊各做一次；兩塊都設好後「🟥 產生疊合圖」才會解鎖。<br>"
                "產生後系統依各面板錨點精準對齊（超規處紅點標示）；"
                "點量測矩形，合併顯示也會鎖定對應的孔，方便對照"
                "「矩形編號 ↔ 哪一區的哪個孔」。")
            grid.addWidget(tl, 0, 0)
            grid.addWidget(tr, 0, 1)
            grid.addWidget(bl, 1, 0)
            grid.addWidget(br, 1, 1)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            grid.setRowStretch(0, 0)
            grid.setRowStretch(1, 1)
            self.add_page("說明", hint)
            first = hint
        if first is not None:
            self._select(first)


def apply_dark(app):
    """套用深色主題：優先用 pyqtdarktheme，失敗則用內建深色 palette。"""
    try:
        import qdarktheme
        try:
            qdarktheme.setup_theme("dark")
        except Exception:  # noqa: BLE001
            app.setStyleSheet(qdarktheme.load_stylesheet("dark"))
        return
    except Exception:  # noqa: BLE001
        pass
    c = QtGui.QColor
    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.Window, c(31, 33, 37))
    pal.setColor(QtGui.QPalette.WindowText, c(220, 222, 224))
    pal.setColor(QtGui.QPalette.Base, c(26, 28, 31))
    pal.setColor(QtGui.QPalette.AlternateBase, c(40, 43, 47))
    pal.setColor(QtGui.QPalette.Text, c(220, 222, 224))
    pal.setColor(QtGui.QPalette.Button, c(48, 51, 56))
    pal.setColor(QtGui.QPalette.ButtonText, c(220, 222, 224))
    pal.setColor(QtGui.QPalette.ToolTipBase, c(40, 43, 47))
    pal.setColor(QtGui.QPalette.ToolTipText, c(220, 222, 224))
    pal.setColor(QtGui.QPalette.Highlight, c(74, 144, 217))
    pal.setColor(QtGui.QPalette.HighlightedText, c(255, 255, 255))
    pal.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Text, c(120, 120, 120))
    app.setStyle("Fusion")
    app.setPalette(pal)


def main():
    app = QtWidgets.QApplication(sys.argv)
    apply_dark(app)
    # 所有按鈕統一填色背景（與輸入框相近），大按鈕/分頁鈕有自己的樣式會覆蓋
    app.setStyleSheet(app.styleSheet() +
                      "QPushButton{background:#2a2e34;border:1px solid #454b54;"
                      "border-radius:6px;padding:5px 12px;}"
                      "QPushButton:hover{background:#343b44;}"
                      "QPushButton:pressed{background:#3c434d;}")
    font = app.font()
    font.setPointSize(max(font.pointSize(), 10))
    app.setFont(font)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
