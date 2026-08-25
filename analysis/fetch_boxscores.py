"""Layer 2：抓每場 boxscore，抽出精簡欄位 → data/boxes.json.gz

每場保留：
- 實際先發投手（含投球數據）、牛棚每人投球數據
- 打線（打序 1-9）與每位打者的打擊數據
- 球隊層級打擊/投球加總
另外收集所有出場球員 id，交給 fetch_people.py 抓左右投打。
"""
import sys

from common import (API, DATA, cache_read, cached_json, jdump, jload, log, pmap)


def sp_and_bullpen(team_box):
    """回傳 (先發投手 dict, 牛棚 list)"""
    pitchers = team_box.get("pitchers") or []
    out_sp, bp = None, []
    for i, pid in enumerate(pitchers):
        p = (team_box.get("players") or {}).get("ID" + str(pid))
        if not p:
            continue
        s = (p.get("stats") or {}).get("pitching") or {}
        if not s:
            continue
        row = {
            "id": pid,
            "ip": s.get("inningsPitched"),
            "h": s.get("hits"), "r": s.get("runs"), "er": s.get("earnedRuns"),
            "bb": s.get("baseOnBalls"), "so": s.get("strikeOuts"),
            "hr": s.get("homeRuns"), "pitches": s.get("pitchesThrown"),
            "strikes": s.get("strikes"), "bf": s.get("battersFaced"),
            "gs": s.get("gamesStarted"),
        }
        if i == 0:
            out_sp = row
        else:
            bp.append(row)
    return out_sp, bp


def lineup(team_box):
    rows = []
    for key, p in (team_box.get("players") or {}).items():
        bo = p.get("battingOrder")
        s = (p.get("stats") or {}).get("batting") or {}
        if not s or not s.get("plateAppearances"):
            continue
        rows.append({
            "id": p["person"]["id"],
            "ord": int(bo) // 100 if bo else None,   # 100→1棒, 201→2棒替補
            "sub": bool(bo) and int(bo) % 100 != 0,
            "pos": (p.get("position") or {}).get("abbreviation"),
            "pa": s.get("plateAppearances"), "ab": s.get("atBats"),
            "h": s.get("hits"), "r": s.get("runs"), "rbi": s.get("rbi"),
            "hr": s.get("homeRuns"), "bb": s.get("baseOnBalls"),
            "so": s.get("strikeOuts"), "tb": s.get("totalBases"),
            "d": s.get("doubles"), "t": s.get("triples"), "sb": s.get("stolenBases"),
        })
    rows.sort(key=lambda r: (r["ord"] is None, r["ord"] or 99, r["sub"]))
    return rows


def team_totals(team_box):
    bt = ((team_box.get("teamStats") or {}).get("batting") or {})
    pt = ((team_box.get("teamStats") or {}).get("pitching") or {})
    return {
        "bat": {k: bt.get(k) for k in
                ("runs", "hits", "homeRuns", "baseOnBalls", "strikeOuts", "atBats",
                 "totalBases", "leftOnBase", "rbi", "doubles", "triples",
                 "stolenBases", "avg", "obp", "slg", "ops")},
        "pit": {k: pt.get(k) for k in
                ("runs", "earnedRuns", "hits", "homeRuns", "baseOnBalls", "strikeOuts",
                 "inningsPitched", "pitchesThrown", "strikes", "battersFaced", "era")},
        "errors": (team_box.get("teamStats", {}).get("fielding") or {}).get("errors"),
    }


def one(pk):
    box = cached_json(f"box/{pk}", f"{API}/game/{pk}/boxscore")
    out = {"pk": pk}
    for side in ("away", "home"):
        tb = box["teams"][side]
        sp, bp = sp_and_bullpen(tb)
        out[side] = {
            "sp": sp, "bp": bp,
            "lineup": lineup(tb),
            "tot": team_totals(tb),
        }
    return out


def main():
    games = jload(f"{DATA}/games.json")
    pks = [g["pk"] for g in games]
    log(f"boxscore 抓取 {len(pks)} 場（已快取的會直接讀）")
    res = pmap(one, pks, workers=10, label="box", every=200)
    ok = {r[0]: r[1] for r in res if r[2] is None}
    errs = [(r[0], repr(r[2])[:120]) for r in res if r[2] is not None]
    log(f"成功 {len(ok)}，失敗 {len(errs)}")
    for pk, e in errs[:10]:
        log("  fail", pk, e)
    boxes = [ok[pk] for pk in pks if pk in ok]
    jdump(boxes, f"{DATA}/boxes.json.gz", gz=True)

    # 收集所有球員 id
    ids = set()
    for b in boxes:
        for side in ("away", "home"):
            s = b[side]
            if s["sp"]:
                ids.add(s["sp"]["id"])
            for p in s["bp"]:
                ids.add(p["id"])
            for p in s["lineup"]:
                ids.add(p["id"])
    jdump(sorted(ids), f"{DATA}/player_ids.json")
    log(f"寫出 data/boxes.json.gz（{len(boxes)} 場），球員 {len(ids)} 人")


if __name__ == "__main__":
    sys.exit(main())
