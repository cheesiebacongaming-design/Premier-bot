"""Pure calculations for VALORANT match scoreboards."""


def build_row(player, total_rounds, first_bloods=0):
    stats = player.get("stats", {})
    shots = stats.get("headshots", 0) + stats.get("bodyshots", 0) + stats.get("legshots", 0)
    agent = player.get("agent") or {}
    tier = player.get("tier") or {}
    return {
        "puuid": player.get("puuid", ""),
        "name": player.get("name", "?"),
        "tag": player.get("tag", ""),
        "agent": agent.get("name", "?"),
        "agent_id": (agent.get("id") or "").lower(),
        "tier": tier.get("id", 0),
        "tier_name": tier.get("name", ""),
        "k": stats.get("kills", 0),
        "d": stats.get("deaths", 0),
        "a": stats.get("assists", 0),
        "acs": round(stats.get("score", 0) / total_rounds),
        "hs": round(100 * stats.get("headshots", 0) / shots) if shots else 0,
        "adr": round((stats.get("damage") or {}).get("dealt", 0) / total_rounds),
        "fb": first_bloods,
    }


def player_matches_membership(player, members):
    """Match a game player against a normalized set of PUUIDs or Riot IDs."""
    return (
        (player.get("puuid") or "").lower() in members
        or f"{player.get('name', '')}#{player.get('tag', '')}".lower() in members
    )


def identify_player_team(players, members):
    """Return the team containing the greatest number of supplied members."""
    ours = [player for player in players if player_matches_membership(player, members)]
    if not ours:
        return None, ours
    side = max(
        {player.get("team_id") for player in ours},
        key=lambda team_id: sum(1 for player in ours if player.get("team_id") == team_id),
    )
    return side, ours


def _identify_premier_team(teams, premier_team_id, premier_team_name, premier_team_tag):
    if premier_team_id:
        side = next((
            team.get("team_id") for team in teams
            if str((team.get("premier_roster") or {}).get("id", "")) == str(premier_team_id)
        ), None)
        if side is not None:
            return side
    if premier_team_name:
        target_name = premier_team_name.casefold()
        target_tag = (premier_team_tag or "").casefold()
        return next((
            team.get("team_id") for team in teams
            if (team.get("premier_roster") or {}).get("name", "").casefold() == target_name
            and (not target_tag or not (team.get("premier_roster") or {}).get("tag")
                 or (team.get("premier_roster") or {}).get("tag", "").casefold() == target_tag)
        ), None)
    return None


def _first_blood_counts(kills):
    by_round = {}
    for kill in kills or []:
        by_round.setdefault(kill.get("round", 0), []).append(kill)
    counts = {}
    for round_kills in by_round.values():
        first = min(round_kills, key=lambda kill: kill.get("time_in_round_in_ms", 0))
        killer = (first.get("killer") or {}).get("puuid")
        counts[killer] = counts.get(killer, 0) + 1
    return counts


def _rows_for(players, team_id, total_rounds, first_bloods):
    rows = [
        build_row(player, total_rounds, first_bloods.get(player.get("puuid"), 0))
        for player in players
        if player.get("team_id") == team_id
    ]
    return sorted(rows, key=lambda row: row["acs"], reverse=True)


def summarise_match(detail, members, premier_team_id=None, premier_team_name=None,
                    premier_team_tag=None, default_team_name=""):
    """Return the legacy match summary shape from HenrikDev match detail."""
    meta = detail.get("metadata", {})
    players = detail.get("players", [])
    teams = detail.get("teams", [])
    side, _ = identify_player_team(players, members)
    if side is None:
        side = _identify_premier_team(teams, premier_team_id, premier_team_name, premier_team_tag)
    team_by_id = {team.get("team_id"): team for team in teams}
    mine = team_by_id.get(side, {})
    other_id, theirs = next(
        ((team_id, team) for team_id, team in team_by_id.items() if team_id != side),
        (None, {}),
    )
    rounds_won = (mine.get("rounds") or {}).get("won", 0)
    rounds_lost = (mine.get("rounds") or {}).get("lost", 0)
    total_rounds = max(rounds_won + rounds_lost, 1)
    first_bloods = _first_blood_counts(detail.get("kills"))
    return {
        "map": (meta.get("map") or {}).get("name", "Unknown"),
        "started": meta.get("started_at"),
        "won": bool(mine.get("won")),
        "score": f"{rounds_won}-{rounds_lost}",
        "rw": rounds_won,
        "rl": rounds_lost,
        "us": (mine.get("premier_roster") or {}).get("name") or default_team_name or "Your team",
        "them": (theirs.get("premier_roster") or {}).get("name") or "Opponent",
        "rows": _rows_for(players, side, total_rounds, first_bloods),
        "enemy_rows": _rows_for(players, other_id, total_rounds, first_bloods),
    }
