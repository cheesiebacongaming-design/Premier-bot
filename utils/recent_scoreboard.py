"""Build full-match and per-half scoreboards from HenrikDev match details."""


def _id(value):
    return (value or {}).get("puuid") if isinstance(value, dict) else None


def _round_number(round_data, index):
    value = round_data.get("id")
    return value if isinstance(value, int) else index


def has_half_data(detail):
    rounds = detail.get("rounds") or []
    players = {p.get("puuid") for p in detail.get("players") or [] if p.get("puuid")}
    return bool(players and rounds and all(isinstance(r.get("stats"), list)
                and players.issubset({_id(item.get("player")) for item in r["stats"]}) for r in rounds)
                and isinstance(detail.get("kills"), list))


def build_scoreboard(detail, period="full"):
    """Return player rows; second half includes overtime rounds."""
    players = detail.get("players") or []
    rounds = detail.get("rounds") or []
    if period != "full" and not has_half_data(detail):
        raise ValueError("Round-by-round statistics are unavailable for this match.")
    selected = [r for i, r in enumerate(rounds)
                if period == "full" or (_round_number(r, i) < 12) == (period == "first")]
    selected_ids = {_round_number(r, i) for i, r in enumerate(rounds) if r in selected}
    teams = detail.get("teams") or []
    total_rounds = sum((teams[0].get("rounds") or {}).get(k, 0) for k in ("won", "lost")) if teams else 0
    count = len(selected) if period != "full" else (total_rounds or len(rounds))
    rows = {}
    player_teams = {p.get("puuid"): p.get("team_id") for p in players}
    for p in players:
        s = p.get("stats") or {}
        row = {"player": p, "k": 0, "d": 0, "a": 0, "score": 0, "damage": 0,
               "head": 0, "body": 0, "leg": 0, "fb": 0, "fd": 0}
        if period == "full":
            row.update(k=s.get("kills", 0), d=s.get("deaths", 0), a=s.get("assists", 0),
                       score=s.get("score", 0), damage=(s.get("damage") or {}).get("dealt", 0),
                       head=s.get("headshots", 0), body=s.get("bodyshots", 0), leg=s.get("legshots", 0))
        rows[p.get("puuid")] = row
    if period != "full":
        for rnd in selected:
            for entry in rnd.get("stats") or []:
                row = rows.get(_id(entry.get("player")))
                if not row:
                    continue
                stats = entry.get("stats") or {}
                for key, source in (("score", "score"), ("head", "headshots"),
                                    ("body", "bodyshots"), ("leg", "legshots")):
                    row[key] += stats.get(source, 0) or 0
                source_team = player_teams.get(_id(entry.get("player")))
                row["damage"] += sum((event.get("damage") or 0) for event in entry.get("damage_events") or []
                                     if _id(event.get("player")) not in player_teams
                                     or player_teams[_id(event.get("player"))] != source_team)
    events = {}
    for kill in detail.get("kills") or []:
        number = kill.get("round")
        if period != "full" and number not in selected_ids:
            continue
        events.setdefault(number, []).append(kill)
        if period != "full":
            killer_id, victim_id = _id(kill.get("killer")), _id(kill.get("victim"))
            if killer_id in rows and victim_id in player_teams and player_teams.get(killer_id) != player_teams[victim_id]:
                rows[killer_id]["k"] += 1
            victim = rows.get(_id(kill.get("victim")))
            if victim:
                victim["d"] += 1
            for assistant in kill.get("assistants") or []:
                assisted = rows.get(_id(assistant))
                if assisted:
                    assisted["a"] += 1
    for group in events.values():
        first = min(group, key=lambda event: event.get("time_in_round_in_ms") or 0)
        for field, key in (("killer", "fb"), ("victim", "fd")):
            row = rows.get(_id(first.get(field)))
            if row:
                row[key] += 1
    for row in rows.values():
        shots = row["head"] + row["body"] + row["leg"]
        row["acs"] = round(row["score"] / count) if count else 0
        row["adr"] = round(row["damage"] / count) if count else 0
        row["hs"] = round(100 * row["head"] / shots) if shots else 0
    return list(rows.values())
