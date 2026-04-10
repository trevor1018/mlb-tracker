# 「分析」Tab 開發規格

## 資料來源

Python 腳本 `mlb_analyzer.py` 會產出 `mlb_analysis.json`，web app 讀取此 JSON 渲染 UI。

執行方式：
```bash
cd D:/python/mlb-tracker
python -X utf8 mlb_analyzer.py all
```

輸出檔案：`D:/python/mlb-tracker/mlb_analysis.json`

---

## JSON Schema

```json
{
  "generated_at": "2026-04-10T16:30:00",
  "date": "2026-04-10",
  "total_season_games": 190,

  "correlations": {
    "bp_fatigue": {
      "correct": 38, "total": 63, "pct": 60.3,
      "z_score": 1.63, "significant": false,
      "direction": "低者勝"
    },
    "home_adv": {
      "correct": 61, "total": 113, "pct": 54.0,
      "z_score": 0.85, "significant": false,
      "direction": "主場"
    },
    "ops": {
      "correct": 47, "total": 113, "pct": 41.6,
      "z_score": -1.79, "significant": false,
      "direction": "高者勝"
    }
    // ... 其他指標同結構
  },

  "matchups": [
    {
      "away": "Atlanta Braves",
      "away_zh": "勇士",
      "home": "Los Angeles Angels",
      "home_zh": "天使",
      "away_sp": "Grant Holmes",
      "home_sp": "Reid Detmers",
      "away_record": "8-5",
      "home_record": "6-7",
      "comparisons": {
        "ops":              { "away": 0.721, "home": 0.698, "edge": "away" },
        "slg":              { "away": 0.410, "home": 0.389, "edge": "away" },
        "obp":              { "away": 0.311, "home": 0.309, "edge": "away" },
        "era":              { "away": 3.45,  "home": 4.10,  "edge": "away" },
        "whip":             { "away": 1.22,  "home": 1.35,  "edge": "away" },
        "fip":              { "away": 3.80,  "home": 4.25,  "edge": "away" },
        "k9":               { "away": 8.5,   "home": 7.2,   "edge": "away" },
        "run_diff_per_game": { "away": 0.8,  "home": -0.3,  "edge": "away" },
        "pyth_pct":         { "away": 0.560, "home": 0.470, "edge": "away" },
        "bp_fatigue":       { "away": 0.32,  "home": 0.71,  "edge": "away" }
      },
      "away_edges": 8,
      "home_edges": 2,
      "edge_summary": "away"
    }
    // ... 其他場次
  ]
}
```

---

## UI 設計規格

### Tab 加入方式

在現有 tabs 陣列（約 line 1030）新增：
```js
{id: "analysis", icon: "🔬", label: "分析"}
```

對應渲染（約 line 1050）：
```js
{tab === "analysis" && <AnalysisView />}
```

### AnalysisView 元件結構

兩個子頁籤（用現有的 `Pill` 元件）：`指標驗證` | `今日分析`

#### 子頁1：指標驗證

顯示回測相關性排名表，從 `correlations` 物件渲染。

```
每一列:
  排名 | 指標名 | 命中率(%) | 進度條 | 樣本數 | 顯著性標記

排序: 依 pct 降序

顏色規則:
  pct > 55%  → 綠色 (有效信號)
  50-55%     → 灰色 (持平)
  45-50%     → 淡紅 (弱反向)
  < 45%      → 紅色 + ⚠️ 標記 (強反向，不要跟!)

特別標注:
  - bp_fatigue 顯示為「🔋 牛棚疲勞」
  - home_adv 顯示為「🏠 主場優勢」
  - 命中率 < 45% 的打擊指標標注「反向指標 - 高反而輸」
```

#### 子頁2：今日分析

從 `matchups` 陣列渲染，每場比賽一張卡片。

```
卡片結構:
┌─────────────────────────────────────┐
│ 勇士 (8-5)  @  天使 (6-7)          │
│ Holmes      vs  Detmers            │
│                                     │
│ 🔋 牛棚疲勞 (權重最高指標)            │
│   勇士 0.32 ▓▓▓░░░░░░░             │
│   天使 0.71 ▓▓▓▓▓▓▓░░░  ← 疲勞!   │
│                                     │
│ 指標比較                             │
│   OPS    0.721 ◀ 0.698  (⚠️反向)    │
│   ERA    3.45  ◀ 4.10              │
│   FIP    3.80  ◀ 4.25              │
│   WHIP   1.22  ◀ 1.35              │
│   K9     8.5   ◀ 7.2               │
│   ...                               │
│                                     │
│ 優勢: 勇士 8項 vs 天使 2項           │
└─────────────────────────────────────┘

edge 箭頭:
  ◀ = away 有優勢（away 值更好）
  ▶ = home 有優勢
  = = 持平

顏色:
  edge 方的數值用綠色
  劣勢方的數值用紅色
  反向指標(pct<45%的) 在指標名旁加 ⚠️ 提醒使用者不要被誤導
```

#### 牛棚疲勞條 (bp_fatigue)

```
值域: 0.0 ~ 1.0
  0.0-0.3 → 綠色 (新鮮)
  0.3-0.6 → 黃色 (普通)  
  0.6-1.0 → 紅色 (疲勞)

計算方式 (Python 端已算好):
  過去 3 天牛棚投球局數 / 9局基準 * 0.4
  + 過去 3 天牛棚投球數 / 135球基準 * 0.6
```

### 卡片排序

依「兩隊牛棚疲勞差距」降序排列。差距大的場次 = 更有分析價值的場次。

---

## 資料載入方式

兩個選項（擇一實作）：

### 選項 A：手動貼上 JSON（最簡單）

在分析頁面放一個 textarea，使用者把 `mlb_analysis.json` 內容貼進去。
```js
const [analysisData, setAnalysisData] = useState(null);
// textarea onChange → JSON.parse → setAnalysisData
```

### 選項 B：從 URL 載入

把 JSON 放到 GitHub Pages 或其他 hosting，fetch 載入。
```js
useEffect(() => {
  fetch('mlb_analysis.json').then(r => r.json()).then(setAnalysisData);
}, []);
```

建議先做選項 A（零部署成本），之後再升級。

---

## 回測發現摘要（供 UI 文案參考）

| 指標 | 命中率 | 結論 |
|------|--------|------|
| 🔋 牛棚疲勞 | **60.3%** | ✅ 唯一有效信號，牛棚新鮮的隊更常贏 |
| 🏠 主場優勢 | 54.0% | ✅ 有微弱正相關 |
| ERA/FIP | ~50% | ➖ 硬幣翻面 |
| OPS/SLG/OBP/AVG | 38-42% | ❌ **反向！高的反而輸（回歸均值效應）** |

這些數據基於 2026 開季 190 場比賽的 boxscore 回測。
樣本仍小，隨賽季進行可能改變。每天跑 `mlb_analyzer.py all` 會自動更新。

---

## 相關檔案

- `D:/python/mlb-tracker/mlb_analyzer.py` — 分析引擎（fetch + 回測 + 產 JSON）
- `D:/python/mlb-tracker/cache/` — API boxscore 快取目錄
- `D:/python/mlb-tracker/mlb_analysis.json` — 輸出的分析結果
- `D:/python/mlb-tracker/analyze_strategies.py` — 舊版策略回測（獨立工具）
