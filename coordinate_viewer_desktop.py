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


def parse_measurement_file(path):
    raw = open(path, "rb").read()
    text = None
    for enc in ("utf-16", "utf-16-le", "utf-8-sig", "utf-8", "big5", "cp950"):
        try:
            text = raw.decode(enc)
            break
        except Exception:  # noqa: BLE001
            continue
    if text is None:
        text = raw.decode("latin-1")
    rows = list(csv.reader(io.StringIO(text), delimiter="\t"))

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


def align_measurement_to_cv(meas_x, meas_y, cvx, cvy):
    meas_x = np.asarray(meas_x, float)
    meas_y = np.asarray(meas_y, float)
    cvx = np.asarray(cvx, float)
    cvy = np.asarray(cvy, float)
    if not (len(meas_x) and len(cvx)):
        return lambda x, y: (x, y)

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
        return None

    def headerData(self, sec, orient, role=QtCore.Qt.DisplayRole):
        if role == QtCore.Qt.DisplayRole and orient == QtCore.Qt.Horizontal:
            return str(self._df.columns[sec])
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
    view.horizontalHeader().setStretchLastSection(True)
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

    def __init__(self, title="", label_prefix="#"):
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
        b = QtWidgets.QPushButton("⟲ 重置")
        b.clicked.connect(self.reset)
        bar.addWidget(b)
        self.info = QtWidgets.QLabel(
            "點圖上的點即鎖定並放大；滾輪以游標為中心縮放、可拖曳平移")
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
            d2 = np.where(self._gvisible, d2, np.inf)
            gi = int(np.argmin(d2))
            best = (float(d2[gi]), "series", self._gmeta[gi])
        if self._red_disp is not None and len(self._red_disp[0]):
            rx, ry = self._red_disp
            d2 = (rx - x) ** 2 + (ry - y) ** 2
            ri = int(np.argmin(d2))
            if best is None or d2[ri] < best[0]:
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
            rv.addWidget(scroll)
        else:
            name, df, xc, yc, color = datasets[0]
            self.single = make_table(with_index(df),
                                     lambda i: self._on_table(name, i))
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
            for n, fr in self.files.items():
                if n == name:
                    fr.highlight(idx)      # 展開 + 灰底標示對應列
                else:
                    fr.collapse()          # 其他收合
        else:
            highlight_table_row(self.single, idx)

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
class MeasTab(QtWidgets.QWidget):
    def __init__(self, mdf, app):
        super().__init__()
        self.mdf = mdf
        self.app = app
        self.ids = mdf["矩形編號"].tolist()
        self.xv = pd.to_numeric(mdf["X中心座標(量測值)"], errors="coerce").to_numpy(float)
        self.yv = pd.to_numeric(mdf["Y中心座標(量測值)"], errors="coerce").to_numpy(float)
        self.over = np.zeros(len(mdf), bool)

        lay = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel(f"已解析 {len(mdf)} 個矩形"))
        top.addWidget(QtWidgets.QLabel("逆時針旋轉(度)"))
        self.rot_spin = QtWidgets.QDoubleSpinBox()
        self.rot_spin.setRange(-360, 360)
        self.rot_spin.valueChanged.connect(self.apply)
        top.addWidget(self.rot_spin)
        bov = QtWidgets.QPushButton("🟥 產生疊合圖（紅點疊到座標檢視器）")
        bov.clicked.connect(lambda: app.show_overlay(self.over))
        top.addWidget(bov)
        top.addStretch(1)
        lay.addLayout(top)

        lay.addWidget(QtWidgets.QLabel(
            "🔴 超出規格標紅設定（填 0=不檢查，單位 mm）；"
            "X/Y中心 = 量測−CAD，間距/高度 = 量測−平均"))
        spec = QtWidgets.QGroupBox()
        spec.setMaximumHeight(96)
        sl = QtWidgets.QHBoxLayout(spec)
        sl.setContentsMargins(8, 6, 8, 6)
        sl.setSpacing(10)
        self.bounds = {}
        for lab, col, mode in [("X中心", "X中心座標(量測值-CAD值)", "diff"),
                               ("Y中心", "Y中心座標(量測值-CAD值)", "diff"),
                               ("間距", "間距(量測值)", "mean"),
                               ("高度", "高度(量測值)", "mean")]:
            grid = QtWidgets.QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(2)
            grid.addWidget(QtWidgets.QLabel(lab), 0, 0, 1, 2)
            grid.addWidget(QtWidgets.QLabel("上限+"), 1, 0)
            up = QtWidgets.QLineEdit("0")
            up.setFixedWidth(90)
            grid.addWidget(up, 1, 1)
            grid.addWidget(QtWidgets.QLabel("下限−"), 2, 0)
            lo = QtWidgets.QLineEdit("0")
            lo.setFixedWidth(90)
            grid.addWidget(lo, 2, 1)
            sl.addLayout(grid)
            self.bounds[col] = (up, lo, mode)
        ap = QtWidgets.QPushButton("套用標紅")
        ap.clicked.connect(self.apply)
        sl.addWidget(ap)
        sl.addStretch(1)
        lay.addWidget(spec)

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.panel = PlotPanel("量測中心座標分布圖（量測值）", label_prefix="矩形 ")
        self.panel.lockChanged.connect(self._on_chart_lock)
        split.addWidget(self.panel)
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.addWidget(QtWidgets.QLabel("資料表（點列即鎖定）"))
        self.table = make_table(mdf, self._on_table)
        rv.addWidget(self.table)
        self.info = QtWidgets.QLabel("")
        self.info.setWordWrap(True)
        rv.addWidget(self.info)
        split.addWidget(right)
        split.setSizes([820, 460])
        lay.addWidget(split)

        self.apply()

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
            if mode == "diff":
                dev = pd.to_numeric(self.mdf[col], errors="coerce").to_numpy(float)
            else:
                meas = pd.to_numeric(self.mdf[col], errors="coerce").to_numpy(float)
                dev = meas - np.nanmean(meas)
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

    def _on_chart_lock(self, name, idx):
        s = next((s for s in self.panel.series if s["name"] == name), None)
        if s is None or s.get("ids") is None:
            return
        rid = s["ids"][idx]
        if rid in self.ids:
            pos = self.ids.index(rid)
            highlight_table_row(self.table, pos)
            self._show(pos)

    def _on_table(self, idx):
        rid = self.ids[idx]
        for s in self.panel.series:
            ids = s.get("ids")
            if ids is not None and rid in ids:
                self.panel.lock_series_point(s["name"], ids.index(rid))
                break
        self._show(idx)

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
        self._lay = QtWidgets.QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)

    def build(self):
        if not self._built:
            self._built = True
            self._lay.addWidget(self._builder())


# ===================== 主視窗 =====================
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("座標檢視器（桌面版）")
        self.resize(1320, 860)
        self.datasets = []
        self.mdf = None
        self.flip = False
        self.rot = 0.0
        self.meas_rot = 0.0

        tb = self.addToolBar("main")
        tb.addAction("📁 上傳座標 CSV（可多選）", self.load_files)
        tb.addAction("📑 上傳 DrillDataSet（排序）", self.load_drill)
        tb.addAction("📐 上傳量測原始檔", self.load_meas)
        tb.addAction("清除全部", self.clear_all)

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

    def load_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "選擇座標 CSV", "", "CSV (*.csv);;All (*)")
        if not paths:
            return
        for p in paths:
            try:
                df = read_csv(p)
            except Exception as e:  # noqa: BLE001
                self.status.showMessage(f"讀取失敗 {os.path.basename(p)}: {e}")
                continue
            xc, yc = find_xy_columns(df)
            self.datasets.append((os.path.basename(p), df, xc, yc, "#1f77b4"))
        self._recolor()
        self.rebuild()

    def load_drill(self):
        if not self.datasets:
            self.status.showMessage("請先上傳座標 CSV")
            return
        p, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "選擇 DrillDataSet", "", "CSV (*.csv);;All (*)")
        if not p:
            return
        order = parse_drill_order(p)
        if not order:
            self.status.showMessage("無法讀出 ArrayName/FilePath")
            return
        rank = {nm: i for i, nm in enumerate(order)}
        self.datasets.sort(key=lambda d: rank.get(_basename(d[0]), len(rank) + 1))
        self._recolor()
        matched = sum(1 for d in self.datasets if _basename(d[0]) in rank)
        self.rebuild()
        self.status.showMessage(
            f"已依 DrillDataSet 排序（{matched}/{len(self.datasets)}）")

    def _recolor(self):
        pal = make_palette(len(self.datasets))
        self.datasets = [(n, df, xc, yc, pal[i % len(pal)])
                         for i, (n, df, xc, yc, _c) in enumerate(self.datasets)]

    def load_meas(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "選擇量測原始檔", "", "CSV (*.csv);;All (*)")
        if not p:
            return
        try:
            self.mdf = parse_measurement_file(p)
        except Exception as e:  # noqa: BLE001
            self.status.showMessage(f"量測檔解析失敗: {e}")
            return
        if self.mdf.empty:
            self.status.showMessage("量測檔找不到矩形資料")
            return
        self.status.showMessage(f"量測檔已解析 {len(self.mdf)} 個矩形")
        self.rebuild()

    def clear_all(self):
        self.datasets = []
        self.mdf = None
        self.rebuild()

    def show_overlay(self, over):
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
        if not self.datasets:
            self.status.showMessage("疊合圖需要先上傳座標 CSV")
            return
        # 量測超規紅點 → 自動對映到座標檢視器座標
        cvx, cvy = [], []
        for name, df, xc, yc, color in self.datasets:
            if xc is None or yc is None:
                continue
            cvx += pd.to_numeric(df[xc], errors="coerce").dropna().tolist()
            cvy += pd.to_numeric(df[yc], errors="coerce").dropna().tolist()
        f = align_measurement_to_cv(self.meastab.xv, self.meastab.yv, cvx, cvy)
        idxs = np.where(over)[0]
        rids = [self.meastab.ids[i] for i in idxs]
        mrx, mry = [], []
        for x, y in zip(self.meastab.xv[over], self.meastab.yv[over]):
            a, b = f(x, y)
            mrx.append(a)
            mry.append(b)
        red = {"rx": np.asarray(mrx, float), "ry": np.asarray(mry, float),
               "ids": rids}
        # 疊合圖 = 合併顯示(含右側檔案清單) + 超規紅點，放在第 2 個位置
        page = CoordTab(self.datasets, True, self, red=red,
                        title="座標檢視器 ＋ 超規紅點")
        self.add_page("🟥 疊合圖", page, pos=1, select=True)
        self.status.showMessage(f"疊合圖：超規紅點 {len(mrx)} 個")

    def rebuild(self):
        self._clear_pages()
        first = None
        if self.mdf is not None:
            self.meastab = MeasTab(self.mdf, self)
            self.add_page("📐 量測檔分析", self.meastab)
            first = self.meastab
        if self.datasets:
            merged = LazyTab(lambda: CoordTab(self.datasets, True, self))
            self.add_page("🔗 合併顯示", merged)
            if first is None:
                first = merged
            for d in self.datasets:
                self.add_page(d[0],
                              LazyTab(lambda d=d: CoordTab([d], False, self)))
        if self.stack.count() == 0:
            hint = QtWidgets.QWidget()
            hl = QtWidgets.QVBoxLayout(hint)
            hl.addStretch(1)
            lbl = QtWidgets.QLabel(
                "<div style='font-size:15px; line-height:1.8;'>"
                "<b>📁 上傳座標 CSV（可多選）</b><br>"
                "上傳 LCH 加工 case 的微孔座標檔案，位於 ProgramObjects "
                "資料夾內（ex：69x71_250um-aliceblue.csv）"
                "<br><br>"
                "<b>📑 上傳 DrillDataSet（排序）</b><br>"
                "DrillDataSet.csv 是 LCH 微孔加工區域的順序，"
                "可自行排定加工順序，調換欄位即可。"
                "<br><br>"
                "<b>📐 上傳量測原始檔</b><br>"
                "台超量測產出的原始檔案直接載入即可。"
                "</div>")
            lbl.setTextFormat(QtCore.Qt.RichText)
            lbl.setWordWrap(True)
            lbl.setMaximumWidth(720)
            row = QtWidgets.QHBoxLayout()
            row.addStretch(1)
            row.addWidget(lbl)
            row.addStretch(1)
            hl.addLayout(row)
            hl.addStretch(1)
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
    font = app.font()
    font.setPointSize(max(font.pointSize(), 10))
    app.setFont(font)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
