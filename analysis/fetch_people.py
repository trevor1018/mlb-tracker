"""抓所有出場球員的左右投打與基本資料 → data/people.json"""
import sys

from common import API, DATA, cached_json, jdump, jload, log

BATCH = 80


def main():
    ids = jload(f"{DATA}/player_ids.json")
    log(f"球員 {len(ids)} 人")
    people = {}
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        url = (f"{API}/people?personIds={','.join(map(str, chunk))}"
               f"&hydrate=currentTeam")
        key = f"people/{chunk[0]}_{len(chunk)}"
        data = cached_json(key, url)
        for p in data.get("people", []):
            people[str(p["id"])] = {
                "name": p.get("fullName"),
                "bats": ((p.get("batSide") or {}).get("code")),      # L / R / S
                "throws": ((p.get("pitchHand") or {}).get("code")),  # L / R
                "pos": ((p.get("primaryPosition") or {}).get("abbreviation")),
                "team": ((p.get("currentTeam") or {}).get("id")),
                "birth": p.get("birthDate"),
            }
        log(f"  {i + len(chunk)}/{len(ids)}")
    jdump(people, f"{DATA}/people.json")
    bats = {}
    throws = {}
    for v in people.values():
        bats[v["bats"]] = bats.get(v["bats"], 0) + 1
        throws[v["throws"]] = throws.get(v["throws"], 0) + 1
    log(f"寫出 data/people.json（{len(people)} 人）打席 {bats} 投球 {throws}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
