"""End-to-end check against a running server: `python -m ott serve --move-delay-ms 0` then `python tests/e2e_api.py [--auto-submit]`.

Uses the demo accounts from data/seed-accounts.json (run `python -m ott seed` first).
Plays a full tournament between the sample bots in samples/bots/.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOTS = {"team_alpha": "greedy", "team_beta": "hunter", "team_gamma": "rusher", "team_delta": "random"}


class Client:
    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.cookie = ""
        self.csrf = ""

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
        con = http.client.HTTPConnection(self.host, self.port, timeout=60)
        headers = {"Content-Type": "application/json", "Cookie": self.cookie, "X-CSRF-Token": self.csrf}
        con.request(method, "/api/v1" + path, json.dumps(body) if body is not None else None, headers)
        resp = con.getresponse()
        raw = resp.read().decode("utf-8")
        for k, v in resp.getheaders():
            if k.lower() == "set-cookie" and v.startswith("ott_session=") and "Max-Age=0" not in v:
                self.cookie = v.split(";")[0]
        con.close()
        if resp.getheader("Content-Type", "").startswith("application/json") and raw:
            return resp.status, json.loads(raw)
        return resp.status, raw

    def login(self, user: str, password: str) -> None:
        status, data = self.call("POST", "/auth/login", {"username": user, "password": password})
        assert status == 200, (user, status, data)
        self.csrf = data["csrf_token"]


def expect(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def wait_for_submission(client: Client, tid: str) -> dict | None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status, data = client.call("GET", f"/tournaments/{tid}/code")
        if status != 200:
            return None
        pending = data.get("pending_submission") or {}
        if data.get("submitted") or data.get("submit_note") or pending.get("status") == "REVIEW":
            return data
        time.sleep(0.25)
    return None


def submit_and_wait(client: Client, tid: str, source: str, name: str) -> str:
    status, data = client.call("POST", f"/tournaments/{tid}/code/submit", {"source": source, "confirm": True})
    expect(status in (200, 202) and data.get("accepted"), f"{name} final submission accepted")
    result = wait_for_submission(client, tid)
    expect(result is not None and result.get("submitted"), f"{name} final code compiled and locked")
    return data["receipt_id"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--data", type=Path, default=ROOT / "data", help="data dir holding seed-accounts.json")
    ap.add_argument("--auto-submit", action="store_true", help="also test auto-submit at the coding deadline (waits ~3 min)")
    args = ap.parse_args()
    creds = json.loads((args.data / "seed-accounts.json").read_text(encoding="utf-8"))
    org = Client("127.0.0.1", args.port)
    org.login("organizer", creds["organizer"]["password"])
    teams = {}
    for name in BOTS:
        c = Client("127.0.0.1", args.port)
        c.login(name, creds[name]["password"])
        teams[name] = c

    # --- permissions
    st, _ = teams["team_alpha"].call("POST", "/tournaments", {"name": "x", "clock_seconds": 300})
    expect(st == 403, "contestant cannot create tournament")
    st, _ = org.call("POST", "/tournaments", {"name": "x", "clock_seconds": 123})
    expect(st == 400, "invalid clock rejected")
    anon = Client("127.0.0.1", args.port)
    st, _ = anon.call("POST", "/tournaments", {"name": "x", "clock_seconds": 300})
    expect(st == 401, "anonymous cannot create tournament")

    # --- lifecycle up to coding
    coding_minutes = 3 if args.auto_submit else 60
    st, t = org.call("POST", "/tournaments", {"name": "E2E Cup", "clock_seconds": 300, "coding_minutes": coding_minutes})
    expect(st == 201 and t["status"] == "DRAFT" and t["coding_seconds"] == coding_minutes * 60, "tournament created (DRAFT)")
    tid = t["id"]
    st, _ = teams["team_alpha"].call("POST", f"/tournaments/{tid}/entry")
    expect(st == 409, "cannot register while DRAFT")
    expect(org.call("POST", f"/tournaments/{tid}/open-registration")[0] == 200, "registration opened")
    for name, c in teams.items():
        expect(c.call("POST", f"/tournaments/{tid}/entry")[0] == 201, f"{name} registered")
    expect(teams["team_alpha"].call("POST", f"/tournaments/{tid}/entry")[0] == 409, "duplicate entry rejected")
    st, _ = teams["team_alpha"].call("PUT", f"/tournaments/{tid}/code", {"source": "int main(){}"})
    expect(st == 409, "cannot edit code before coding phase")
    st, d = org.call("POST", f"/tournaments/{tid}/start-coding")
    expect(st == 200 and d["status"] == "CODING" and d["coding_deadline"] - d["coding_started_at"] == coding_minutes * 60, f"coding phase started ({coding_minutes} min deadline)")
    expect(org.call("POST", f"/tournaments/{tid}/lock")[0] == 409, "cannot lock while coding is open and nobody submitted")

    # --- coding: draft, compile check, limit, submit, immutability
    alpha = teams["team_alpha"]
    st, code = alpha.call("GET", f"/tournaments/{tid}/code")
    expect(st == 200 and code["editable"] and code["is_template"] and "choose" in code["source"], "template served as starting draft")
    st, d = alpha.call("PUT", f"/tournaments/{tid}/code", {"source": "this is not c++"})
    expect(st == 200, "draft autosave works")
    time.sleep(3.1)
    st, d = alpha.call("POST", f"/tournaments/{tid}/code/check", {"source": "this is not c++"})
    expect(st == 200 and d["ok"] is False and d["compile_used"] == 1, "compile check reports error and uses 1 attempt")
    st, d = alpha.call("POST", f"/tournaments/{tid}/code/submit", {"source": "this is not c++", "confirm": True})
    expect(st == 202 and d["accepted"], "invalid final source accepted for queued compilation")
    failed = wait_for_submission(alpha, tid)
    expect(failed is not None and failed.get("submit_note") and not failed["submitted"],
           "invalid final source fails asynchronously")
    expect(failed["compile_used"] == 2, "failed submit consumed an attempt")

    sources = {n: (ROOT / "samples" / "bots" / f"{b}.cpp").read_text(encoding="utf-8") for n, b in BOTS.items()}
    time.sleep(3.1)
    st, d = alpha.call("POST", f"/tournaments/{tid}/code/check", {"source": sources["team_alpha"]})
    expect(st == 200 and d["ok"] is True, "sample bot compiles on check")
    st, d = alpha.call("POST", f"/tournaments/{tid}/code/submit", {"source": sources["team_alpha"]})
    expect(st == 400, "submit requires confirm=true")

    # beta: burn the compile limit with checks, then submit the last good source for free
    beta = teams["team_beta"]
    for i in range(9):
        time.sleep(3.1)
        st, d = beta.call("POST", f"/tournaments/{tid}/code/check", {"source": sources["team_beta"] if i == 8 else "int main(){ return }"})
        expect(st == 200 and d["ok"] == (i == 8), f"beta compile check #{i + 1}")
    time.sleep(3.1)
    st, d = beta.call("POST", f"/tournaments/{tid}/code/check", {"source": sources["team_beta"]})
    expect(st == 200 and d["compile_used"] == 10, "10th compile attempt allowed")
    time.sleep(3.1)
    st, d = beta.call("POST", f"/tournaments/{tid}/code/check", {"source": sources["team_beta"]})
    expect(st == 429 and d["error"]["code"] == "COMPILE_LIMIT", "11th compile attempt rejected (limit 10)")
    st, d = beta.call("POST", f"/tournaments/{tid}/code/submit", {"source": sources["team_beta"] + "\n// edited\n", "confirm": True})
    expect(st == 429, "after the limit, edited source cannot be submitted")
    submit_and_wait(beta, tid, sources["team_beta"], "beta")

    skip = "team_delta" if args.auto_submit else None
    alpha_receipt = None
    for name in ("team_alpha", "team_gamma", "team_delta"):
        c = teams[name]
        if name == skip:
            st, _ = c.call("PUT", f"/tournaments/{tid}/code", {"source": sources[name]})
            expect(st == 200, f"{name} saved a draft but will NOT submit")
            continue
        receipt_id = submit_and_wait(c, tid, sources[name], name)
        if name == "team_alpha":
            alpha_receipt = receipt_id

    st, d = alpha.call("PUT", f"/tournaments/{tid}/code", {"source": "// tamper"})
    expect(st == 409 and d["error"]["code"] == "ALREADY_SUBMITTED", "code cannot change after submission")
    st, d = alpha.call("POST", f"/tournaments/{tid}/code/submit", {"source": sources["team_alpha"], "confirm": True})
    expect(st == 200 and d["accepted"] and d["receipt_id"] == alpha_receipt,
           "repeated final submit returns the same immutable receipt")
    st, d = alpha.call("GET", f"/tournaments/{tid}/code")
    expect(d["submitted"] and not d["editable"] and d["source"] == sources["team_alpha"], "submitted code is read-only and unchanged")
    st, d = org.call("GET", f"/tournaments/{tid}/entries")
    expect(all("source" not in e for e in d["items"]), "organizer roster never contains source")
    bot_id = next(e["bot_version_id"] for e in d["items"] if e["username"] == "team_alpha")
    expect(org.call("GET", f"/bots/{bot_id}/source")[0] == 403, "organizer cannot read bot source")
    expect(teams["team_beta"].call("GET", f"/bots/{bot_id}/source")[0] == 403, "other team cannot read bot source")
    expect(alpha.call("GET", f"/bots/{bot_id}/source")[0] == 200, "owner can read own source")

    if args.auto_submit:
        print("waiting for the coding deadline (auto-submit of team_delta's draft)...")
        deadline = org.call("GET", f"/tournaments/{tid}")[1]["coding_deadline"]
        while time.time() < deadline:
            time.sleep(1)
        delta = None
        for _ in range(40):  # the watcher compiles the draft, which takes a few seconds
            st, d = org.call("GET", f"/tournaments/{tid}/entries")
            delta = next(e for e in d["items"] if e["username"] == "team_delta")
            if delta["submitted_at"]:
                break
            time.sleep(1)
        expect(delta["submitted_at"] is not None and delta["status"] == "READY", "draft auto-submitted at the deadline")
        expect(teams["team_delta"].call("PUT", f"/tournaments/{tid}/code", {"source": "// late"})[0] == 409, "saving after the deadline rejected")
    else:
        pass

    # --- lock, schedule, start, play
    if not args.auto_submit:
        st, d = org.call("POST", f"/tournaments/{tid}/lock")
        expect(st == 200 and d["status"] == "LOCKED", "roster locked once all submitted")
    else:
        st, d = org.call("POST", f"/tournaments/{tid}/lock")
        expect(st == 200 and d["status"] == "LOCKED", "roster locked after the deadline")
    st, d = org.call("POST", f"/tournaments/{tid}/schedule")
    expect(st == 201 and d["matches"] == 12, "double round robin generated: 12 matches for 4 teams")
    st, d = org.call("POST", f"/tournaments/{tid}/start")
    expect(st == 200 and d["status"] == "RUNNING", "tournament started")

    print("playing matches...")
    start = time.time()
    while True:
        st, t = org.call("GET", f"/tournaments/{tid}")
        print(f"  {t['status']} finished {t['matches_finished']}/{t['matches_total']} running {t['matches_running']}", end="\r")
        if t["status"] == "FINISHED":
            break
        if time.time() - start > 900:
            expect(False, "tournament finished within 15 minutes")
        time.sleep(2)
    print()
    st, d = org.call("GET", f"/tournaments/{tid}/schedule")
    reasons: dict[str, int] = {}
    for m in d["matches"]:
        reasons[f"{m['result']}/{m['result_reason']}"] = reasons.get(f"{m['result']}/{m['result_reason']}", 0) + 1
    print("results:", reasons)
    expect(all(m["status"] == "FINISHED" and m["result"] for m in d["matches"]), "all 12 matches finished with a result")
    expect(not any((m["result_reason"] or "").startswith("INFRA") for m in d["matches"]), "no infrastructure failures")
    expect(not any(m["result_reason"] in ("ILLEGAL_ACTION", "INVALID_ACTION", "BOT_CRASH", "TIMEOUT") for m in d["matches"]), "no bot protocol errors")
    st, s = org.call("GET", f"/tournaments/{tid}/standings")
    for row in s["items"]:
        print(f"  #{row['rank']} {row['username']:<12} {row['points']:>3} pts  W{row['wins']} D{row['draws']} L{row['losses']}")
    expect(sum(r["points"] for r in s["items"]) == sum(3 if m["result"] != "DRAW" else 2 for m in d["matches"]), "points add up")

    # --- spectator view: placement revealed after PLAY
    mid = d["matches"][0]["id"]
    st, m = anon.call("GET", f"/matches/{mid}")
    expect(st == 200 and m["revealed"] and m["placements"] and len(m["placements"]) == 20, "finished match exposes placements and moves publicly")
    print("ALL OK")


if __name__ == "__main__":
    main()
