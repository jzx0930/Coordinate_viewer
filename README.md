# 座標檢視器 Coordinate Viewer

LCH 微孔加工座標的視覺化與量測分析工具。可載入各區域的微孔座標、依加工順序排列、檢視分布，並把台超量測產出的結果與超出規格的位置標示在座標圖上。

提供兩個版本，功能一致：

- **網頁版** `coordinate_viewer.py`（Streamlit + Plotly）
- **桌面版** `coordinate_viewer_desktop.py`（PyQt5 + pyqtgraph，深色介面、可流暢處理上萬點）

## 功能

- 載入多個座標 CSV，合併或分檔檢視；每個區域以高對比顏色區分。
- 依 `DrillDataSet` 的加工順序排列各區域。
- 點圖上的點或表格列即可鎖定、放大置中並標示；滾輪以游標為中心縮放、可拖曳平移。
- 翻轉到背面（X 鏡像）、繞原點逆時針旋轉。
- 全部顯示/隱藏、依序隱藏動畫。
- 量測檔分析：解析台超量測原始檔，列出資料表與 X/Y 中心分布圖。
- 超出規格標紅：X/Y 中心以「量測−CAD」、間距/高度以「量測−平均」判定，可各設上下限。
- 疊合圖：把超規紅點自動對映到座標檢視器座標系（含鏡像偵測），疊在彩色區域上。

## 安裝與執行

需先安裝 [Python](https://www.python.org/downloads/)（安裝時勾選 *Add Python to PATH*）。

Windows 使用者可直接雙擊批次檔，第一次執行會自動安裝套件：

- 網頁版：`start.bat`
- 桌面版：`start_desktop.bat`

或手動安裝後執行：

```bash
pip install -r requirements.txt

# 網頁版
streamlit run coordinate_viewer.py

# 桌面版
python coordinate_viewer_desktop.py
```

## 輸入檔案格式

- **座標 CSV（可多選）**：各區域的微孔座標，含 `x`、`y` 欄位（例：`69x71_250um-aliceblue.csv`）。
- **DrillDataSet（選用）**：加工區域順序，含 `ArrayName` 或 `FilePath` 欄，可自行調換順序。
- **量測原始檔（選用）**：台超量測產出的原始檔（UTF-16／Tab 分隔），直接載入即可。

## 檔案說明

```
coordinate_viewer.py          網頁版主程式
coordinate_viewer_desktop.py  桌面版主程式
start.bat                     網頁版啟動（自動安裝套件）
start_desktop.bat             桌面版啟動（自動安裝套件）
.streamlit/config.toml        網頁版深色主題設定
requirements.txt              相依套件
```

> 實際的量測資料、座標 CSV 與產出檔案已由 `.gitignore` 排除，不會上傳到版本庫。
