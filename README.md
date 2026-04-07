# ⚾ MLB 投注追蹤 V1

台灣運彩 MLB 投注追蹤與賽果分析工具。支援跨裝置同步、MLB 官方數據自動匯入。

## 線上使用

<https://trevor1018.github.io/mlb-tracker/>

## 功能總覽

### 投注紀錄
- 支援**單場**與**串關**（最多 5 關）投注紀錄
- 玩法：不讓分、讓分、大小分
- 自訂萬年曆日期選擇器（同一張單統一日期）
- 投注紀錄可完整編輯（日期、隊伍、玩法、賠率、金額等）
- 可手動填入比數，或由賽果自動帶入判定勝負

### 賽果紀錄
- **MLB API 自動匯入**：選擇日期後一鍵匯入當天所有已完賽比賽
- 匯入資料包含：
  - 雙方隊名（中英文）
  - 最終比分、安打數、失誤數
  - 逐局得分 (Linescore)
  - 先發投手數據：IP、H、R、ER、BB、SO、HR、投球數/好球數
  - 勝投 (W)、敗投 (L)、救援 (SV)
  - 全壘打明細（打者、局數、描述）
- 賽果卡片可展開「詳細」查看完整數據
- 列表頁依日期篩選（預設美西太平洋時間）
- 匯入賽果後自動同步更新對應投注紀錄的比數與勝負判定
- 不重複匯入（以 MLB gamePk 為 ID）

### 統計分析

#### 各區戰績
- 即時從 MLB API 抓取當季各分區戰績
- 美聯 / 國聯共 6 個分區
- 顯示：W、L、勝率、GB、連勝/敗、L10、主場戰績、客場戰績

#### 球隊數據
- **單隊模式**：選擇一隊查看完整數據
- **比較模式**：左右兩欄各選一隊並排比較
- 數據項目：
  - 連勝/敗、總場次
  - 近 5 / 10 / 20 場戰績
  - 主場 / 客場勝敗
  - 場均得分、失分、總分、勝場均分差
  - 盤口數據：讓 1.5 過關率、大 7.5 / 8.5 / 9.5 命中率
  - 分差分布（≤2 分、3-4 分、≥5 分）
  - 近 10 場逐場明細

#### 投注績效
- 已結算注數、勝率、總投注、累積盈虧、ROI
- 各玩法（不讓分 / 讓分 / 大小分）分別統計

## 技術架構

| 項目 | 技術 |
|------|------|
| 前端 | React 18 + Babel（單一 HTML 檔，CDN 載入） |
| 樣式 | CSS-in-JS（inline styles） |
| 字型 | Noto Sans TC + JetBrains Mono |
| 資料庫 | Firebase Firestore（即時同步） |
| 認證 | Firebase Auth（Google 登入） |
| 快取 | localStorage（離線備援） |
| 資料來源 | [MLB Stats API](https://statsapi.mlb.com)（免費、無需 key） |
| 部署 | GitHub Pages |

### 資料結構（Firestore）

```
users/{uid}/bets/{betId}     — 投注紀錄
users/{uid}/games/{gameId}   — 賽果紀錄
```

### MLB API 端點

| 用途 | 端點 |
|------|------|
| 當日賽程 + 比分 | `/api/v1/schedule?date=YYYY-MM-DD&sportId=1&hydrate=linescore,decisions` |
| 單場 Boxscore | `/api/v1/game/{gamePk}/boxscore` |
| 單場逐球紀錄 | `/api/v1/game/{gamePk}/playByPlay` |
| 各區戰績 | `/api/v1/standings?leagueId=103,104&season=YYYY&standingsTypes=regularSeason` |

## 本地開發

由於 Firebase Auth 不支援 `file://` 協定，需透過本地伺服器：

```bash
python -m http.server 8080
```

然後開啟 http://localhost:8080

## Firebase 設定

### Firestore 安全規則

```
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /users/{userId}/{document=**} {
      allow read, write: if request.auth != null && request.auth.uid == userId;
    }
  }
}
```

### Authentication

- 啟用 Google 登入
- 已授權網域：`localhost`、`trevor1018.github.io`

## 支援的 30 支 MLB 球隊

| 美聯東區 | 美聯中區 | 美聯西區 | 國聯東區 | 國聯中區 | 國聯西區 |
|---------|---------|---------|---------|---------|---------|
| 洋基 | 守護者 | 太空人 | 大都會 | 釀酒人 | 道奇 |
| 紅襪 | 皇家 | 運動家 | 勇士 | 紅人 | 教士 |
| 藍鳥 | 老虎 | 水手 | 費城人 | 海盜 | 巨人 |
| 光芒 | 雙城 | 天使 | 馬林魚 | 紅雀 | 響尾蛇 |
| 金鶯 | 白襪 | 遊騎兵 | 國民 | 小熊 | 洛磯 |

## 授權

MIT License
