# ⚾ MLB 投注追蹤

台灣運彩 MLB 投注追蹤與賽果分析工具。目前為**重建中的空白骨架**，等待新的資料分析方式接入。

## 線上使用

<https://trevor1018.github.io/mlb-tracker/>

## 目前狀態

| 頁面 | 內容 |
|------|------|
| 首頁 | Hello World + 環境資訊（React / Firebase 版本、日期、球隊對照表筆數） |
| 維護 | 雲端資料筆數檢視、一鍵清除 `bets` / `games` 全部文件與 localStorage 快取 |

舊版的投注 / 紀錄 / 賽果 / 統計 / 分析五個頁面與 Python 分析程式（`mlb_analyzer.py`、`backtest_user_bets.py`、`convergence_analysis.py`、`mlb_analysis.json`、`cache/`）已移除，需要時可從 git 歷史 commit `22b2ffe` 取回。

## 保留的架構

- **前端**：React 18 + Babel standalone（單一 HTML 檔，CDN 載入）
- **樣式**：CSS-in-JS（inline styles）+ 深色主題常數 `C`
- **UI atoms**：`Field` / `Inp` / `Sel` / `Pill` / `Stat` / `Card`
- **認證**：Firebase Auth（Google 登入，popup 失敗自動 fallback redirect）
- **資料庫**：Firebase Firestore + `useFirestore(uid, col, localKey, orderField)` hook（即時同步 + localStorage 備援）
- **常數**：30 隊中文名 `TEAMS`、MLB team id 對照 `TEAM_MAP`、`MLB_API` base URL
- **工具**：`load` / `save` / `genId` / `todayStr`（台北）/ `todayPT`（美西）/ `wipeCollection`
- **部署**：GitHub Pages（main 分支根目錄）

### 資料結構（Firestore）

```
users/{uid}/bets/{betId}     — 投注紀錄
users/{uid}/games/{gameId}   — 賽果紀錄
```

### 資料來源

[MLB Stats API](https://statsapi.mlb.com) — 免費、無需 API key。

| 用途 | 端點 |
|------|------|
| 當日賽程 + 比分 | `/api/v1/schedule?date=YYYY-MM-DD&sportId=1&hydrate=linescore,decisions` |
| 單場 Boxscore | `/api/v1/game/{gamePk}/boxscore` |
| 單場逐球紀錄 | `/api/v1/game/{gamePk}/playByPlay` |
| 各區戰績 | `/api/v1/standings?leagueId=103,104&season=YYYY&standingsTypes=regularSeason` |

## 本地開發

Firebase Auth 不支援 `file://` 協定，需透過本地伺服器：

```bash
python -m http.server 8080
```

開啟 <http://localhost:8080>

## 上 code 流程

```bash
git add -A
git commit -m "訊息"
git push
```

推上 `main` 後 GitHub Pages 自動部署，約 1 分鐘後線上生效（瀏覽器需強制重新載入以避開快取）。

## Firebase 設定

- Firestore 規則見 `firestore.rules`：僅允許使用者讀寫自己的 `users/{uid}` 子集合
- Authentication：啟用 Google 登入，已授權網域 `localhost`、`trevor1018.github.io`

## 授權

MIT License
