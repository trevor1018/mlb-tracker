"""賽前個別更新：抓實際先發打線與最新先發投手，重算該場的預測與對位分析。

為什麼需要：先發打線通常賽前 1 小時左右才公布（實測 MLB API 在 T-40 分就有了），
半夜那次全量更新只能用「近 30 天推估打線」。

執行方式：由工作排程每 5 分鐘叫一次，這支腳本自己判斷有沒有比賽進入更新窗口：
  T-60 分（±10 分）→ 第一次個別更新（先發可能剛換、打線可能已出）
  T-30 分（±10 分）→ 第二次個別更新（打線幾乎一定出來了）
沒有比賽在窗口內就直接結束（幾乎不耗資源）。

每場更新完會在 slate.json 寫入 updated_at 時間戳，網頁上看得到資料多新。
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

from common import API, DATA, OUTPUT, ROOT, fetch_json, jdump, jload, log

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOWS = [("t60", 60, 10), ("t30", 30, 10)]   # (代號, 分鐘, 容差)
HYDRATE = "lineups,probablePitcher,linescore"


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def fetch_slate_status(dates):
    """回傳 {pk: {...}}：開賽時間、狀態、實際打線、先發投手。"""
    out = {}
    for d in dates:
        try:
            data = fetch_json(f"{API}/schedule?sportId=1&date={d}&hydrate={HYDRATE}")
        except Exception as e:
            log(f"抓 {d} 賽程失敗：{repr(e)[:100]}")
            continue
        for day in data.get("dates", []):
            for g in day.get("games", []):
                lu = g.get("lineups") or {}
                out[int(g["gamePk"])] = {
                    "date": day["date"],
                    "start": g.get("gameDate"),
                    "status": g["status"]["detailedState"],
                    "away_lineup": [p["id"] for p in lu.get("awayPlayers", [])],
                    "home_lineup": [p["id"] for p in lu.get("homePlayers", [])],
                    "away_sp": ((g["teams"]["away"].get("probablePitcher") or {}).get("id")),
                    "home_sp": ((g["teams"]["home"].get("probablePitcher") or {}).get("id")),
                }
    return out


def due_games(status, state, force=False):
    """判斷哪些比賽進入更新窗口。"""
    now = now_utc()
    due = []
    for pk, s in status.items():
        if s["status"] not in ("Scheduled", "Pre-Game", "Warmup", "Delayed Start"):
            continue
        if not s.get("start"):
            continue
        start = dt.datetime.fromisoformat(s["start"].replace("Z", "+00:00"))
        mins = (start - now).total_seconds() / 60
        done = state.get(str(pk), {})
        for tag, target, tol in WINDOWS:
            if force or (abs(mins - target) <= tol and tag not in done):
                due.append((pk, tag, round(mins)))
                break
    return due


def run(step, extra=()):
    r = subprocess.run([sys.executable, f"{step}.py", *extra], cwd=HERE,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        log(f"  {step} 失敗（exit {r.returncode}）")
        for ln in (r.stdout or "").splitlines()[-6:]:
            log(f"    {ln}")
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="不管窗口，立刻更新所有未開打場次")
    ap.add_argument("--push", action="store_true", help="更新後自動 commit + push")
    ap.add_argument("--season", type=int, default=2026)
    args = ap.parse_args()

    try:
        pending = jload(f"{DATA}/pending.json")
    except Exception:
        log("沒有 pending.json，先跑 fetch_season")
        return 1
    dates = sorted({g["date"] for g in pending})
    if not dates:
        log("沒有未開打的場次")
        return 0

    state_f = os.path.join(DATA, "live_state.json")
    state = jload(state_f) if os.path.exists(state_f) else {}

    status = fetch_slate_status(dates)
    due = due_games(status, state, force=args.force)
    if not due:
        log(f"沒有比賽進入更新窗口（監看 {len(status)} 場）")
        return 0

    log(f"進入窗口的比賽：{len(due)} 場")
    for pk, tag, mins in due:
        s = status[pk]
        n_lu = len(s["home_lineup"]) + len(s["away_lineup"])
        log(f"  pk={pk} {tag}（距開賽 {mins} 分）狀態 {s['status']}"
            f" 打線 {len(s['away_lineup'])}/{len(s['home_lineup'])}")

    # 寫出實際打線供 build_matchup_report 使用
    stamp = now_utc().astimezone().strftime("%Y-%m-%d %H:%M")
    lineups_f = os.path.join(DATA, "lineups.json")
    lineups = jload(lineups_f) if os.path.exists(lineups_f) else {}
    updated_pks = []
    for pk, tag, mins in due:
        s = status[pk]
        lineups[str(pk)] = {"away": s["away_lineup"], "home": s["home_lineup"],
                            "updated_at": stamp, "minutes_to_start": mins,
                            "status": s["status"]}
        updated_pks.append(pk)
        state.setdefault(str(pk), {})[tag] = stamp
    jdump(lineups, lineups_f)
    jdump(state, state_f)

    # 重跑必要步驟（用聚合快取，整條約 10 秒）
    log("重算：fetch_season → build_dataset → predict_slate → build_matchup_report → build_app_data")
    for step, extra in (("fetch_season", ()), ("build_dataset", ()),
                        ("predict_slate", ()), ("build_matchup_report", ()),
                        ("build_app_data", ())):
        if not run(step, extra):
            return 1

    # 在 slate.json 標上每場的更新時間
    slate = jload(f"{OUTPUT}/slate.json")
    for g in slate["games"]:
        lu = lineups.get(str(g["pk"]))
        if lu:
            g["updated_at"] = lu["updated_at"]
            g["lineup_ready"] = len(lu.get("home", [])) >= 8 and len(lu.get("away", [])) >= 8
            g["minutes_to_start"] = lu.get("minutes_to_start")
    slate["live_updated_at"] = stamp
    jdump(slate, f"{OUTPUT}/slate.json")
    run("build_app_data")

    log(f"完成：{len(updated_pks)} 場已更新（{stamp}）")

    if args.push:
        subprocess.run(["git", "add", "-A"], cwd=ROOT)
        msg = f"賽前更新 {stamp}：{len(updated_pks)} 場"
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ROOT)
        r = subprocess.run(["git", "push", "-q", "origin", "main"], cwd=ROOT)
        log("已推上 GitHub Pages" if r.returncode == 0 else "push 失敗")
    return 0


if __name__ == "__main__":
    sys.exit(main())
