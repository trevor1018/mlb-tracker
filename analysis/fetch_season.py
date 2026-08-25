"""Layer 1：抓整季賽程 + 逐局比分 + 先發預告，輸出 data/games.json。

一場比賽一列，包含：日期、日夜場、球場、雙方隊 id、最終比分、逐局得分、
先發投手 id、系列賽狀態、天氣。這是所有盤口結果（ML / 大小分 / 讓分 /
單隊大小分 / 前5局）的計算基礎。
"""
import calendar
import datetime as dt
import sys

from common import (API, DATA_THROUGH, SEASON, SEASON_START, cached_json, fetch_json,
                    jdump, log, TEAM_ZH)

HYDRATE = "linescore,probablePitcher,decisions,weather,venue,seriesStatus,flags,game(content(summary))"


def fetch_range(start, end):
    url = (f"{API}/schedule?sportId=1&startDate={start}&endDate={end}"
           f"&gameType=R&hydrate={HYDRATE}")
    key = f"sched/{start}_{end}"
    return cached_json(key, url)


def parse_game(g, date):
    ls = g.get("linescore") or {}
    away, home = g["teams"]["away"], g["teams"]["home"]
    if away["team"]["id"] not in TEAM_ZH or home["team"]["id"] not in TEAM_ZH:
        return None  # 全明星賽等非 30 隊對戰
    innings = []
    for inn in ls.get("innings", []):
        innings.append({
            "n": inn.get("num"),
            "a": (inn.get("away") or {}).get("runs"),
            "h": (inn.get("home") or {}).get("runs"),
        })
    w = g.get("weather") or {}
    row = {
        "pk": g["gamePk"],
        "date": date,
        "dt": g.get("gameDate"),
        "dayNight": g.get("dayNight"),
        "status": g["status"]["detailedState"],
        "doubleHeader": g.get("doubleHeader"),
        "gameNumber": g.get("gameNumber"),
        "venueId": (g.get("venue") or {}).get("id"),
        "venue": (g.get("venue") or {}).get("name"),
        "away": away["team"]["id"],
        "home": home["team"]["id"],
        "awayScore": away.get("score"),
        "homeScore": home.get("score"),
        "awayRec": [away["leagueRecord"]["wins"], away["leagueRecord"]["losses"]],
        "homeRec": [home["leagueRecord"]["wins"], home["leagueRecord"]["losses"]],
        "innings": innings,
        "scheduledInnings": ls.get("scheduledInnings"),
        "awaySpProb": ((away.get("probablePitcher") or {}).get("id")),
        "homeSpProb": ((home.get("probablePitcher") or {}).get("id")),
        "temp": w.get("temp"),
        "cond": w.get("condition"),
        "wind": w.get("wind"),
        "seriesGame": g.get("seriesGameNumber"),
        "seriesLen": g.get("gamesInSeries"),
        "dayGame": (g.get("dayNight") == "day"),
    }
    return row


def main():
    start = SEASON_START
    end = DATA_THROUGH
    future_end = (dt.date.fromisoformat(DATA_THROUGH) + dt.timedelta(days=4)).isoformat()
    log(f"抓取賽程 {start} → {end}")
    # 一次抓一個月，避開單一回應過大
    months = []
    y = SEASON
    for m in range(3, 13):
        s = f"{y}-{m:02d}-01"
        e = f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"
        if s > end:
            break
        months.append((s, min(e, end)))

    games = []
    for s, e in months:
        data = fetch_range(s, e)
        n = 0
        for d in data.get("dates", []):
            for g in d.get("games", []):
                row = parse_game(g, d["date"])
                if row:
                    games.append(row)
                    n += 1
        log(f"  {s}~{e}: {n} 場")

    games.sort(key=lambda r: (r["date"], r["pk"]))
    finals = [g for g in games if g["status"].startswith("Final") or g["status"] == "Completed Early"]
    log(f"合計 {len(games)} 場，其中完賽 {len(finals)} 場")
    if finals:
        log(f"最後完賽日：{finals[-1]['date']}")
        # 每隊場次
        cnt = {}
        for g in finals:
            for t in (g["away"], g["home"]):
                cnt[t] = cnt.get(t, 0) + 1
        lo = min(cnt.values())
        hi = max(cnt.values())
        log(f"每隊完賽場次 {lo}~{hi}（共 {len(cnt)} 隊）")
    jdump(finals, f"{DATA}/games.json")
    log(f"寫出 data/games.json（{len(finals)} 場完賽）")

    # 未來場次（供「今日推薦」用）：資料截止日之後 4 天，含先發預告
    fut_url = (f"{API}/schedule?sportId=1&startDate={DATA_THROUGH}&endDate={future_end}"
               f"&gameType=R&hydrate={HYDRATE}")
    fut_raw = fetch_json(fut_url)
    pending = []
    for d in fut_raw.get("dates", []):
        for g in d.get("games", []):
            row = parse_game(g, d["date"])
            if row and row["homeScore"] is None and row["awayScore"] is None:
                pending.append(row)
    pending.sort(key=lambda r: (r["date"], r["pk"]))
    jdump(pending, f"{DATA}/pending.json")
    have_sp = sum(1 for r in pending if r["awaySpProb"] and r["homeSpProb"])
    log(f"寫出 data/pending.json（{len(pending)} 場未開打，其中 {have_sp} 場雙方先發已公布）")


if __name__ == "__main__":
    from common import DATA
    sys.exit(main())
