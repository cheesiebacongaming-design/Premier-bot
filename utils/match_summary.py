import random


def _play_recap(map_name, won, our_score, their_score, tracked, all_teammates, duration, detail=None, registered_only=False):
    result = "snatched the win" if won else "took the L"
    subject = "Your team" if registered_only else "Your queue"
    recap = f"{subject} {result} on {map_name} after {duration} of competitive Valorant, {our_score}-{their_score}."
    if not all_teammates:
        return recap
    ranked = sorted(all_teammates, key=lambda p: (p.get("stats") or {}).get("score", 0), reverse=True)
    strongest, weakest = ranked[0], ranked[-1]
    top_stats, low_stats = strongest.get("stats") or {}, weakest.get("stats") or {}
    strongest_name = strongest.get("name", "A teammate")
    top_acs = round(top_stats.get("score", 0) / max(our_score + their_score, 1))
    top_kd = top_stats.get("kills", 0) / max(top_stats.get("deaths", 0), 1)
    top_title = "sweaty bastard" if top_acs >= 220 or top_kd >= 1.2 else "designated tryhard"
    standing = "leading the registered players" if registered_only else "topping the team"
    if registered_only and len(ranked) == 1:
        standing = "finishing"
    recap += (f" {strongest_name} was the {top_title}, {standing} with {top_acs} ACS, "
              f"{top_stats.get('kills', 0)}/{top_stats.get('deaths', 0)}/{top_stats.get('assists', 0)} "
              f"and a {top_kd:.2f} KD.")
    if strongest.get("puuid") != weakest.get("puuid"):
        low_name = weakest.get("name", "a teammate")
        low_acs = round(low_stats.get("score", 0) / max(our_score + their_score, 1))
        low_kd = low_stats.get("kills", 0) / max(low_stats.get("deaths", 0), 1)
        if low_stats.get("deaths", 0) >= 18:
            roast = random.choice(("spent the whole damn match sightseeing in spawn", "fed so hard the enemy should send a thank-you card", "turned dying into a full-time fucking job"))
        elif low_acs < 100:
            roast = random.choice(("couldn't hit shit if the target stood still", "apparently brought a fucking water pistol", "their crosshair was on holiday and their aim was in hell"))
        elif low_kd < 0.6:
            roast = random.choice(("got their arse kicked in practically every duel", "donated kills like a fucking charity", "made the enemy look like a professional team"))
        else:
            roast = random.choice(("the scoreboard called and it wants an apology", "that was a pretty shit shift", "even the bots are talking trash about that performance"))
        recap += (f" {low_name} had the roughest shift: {low_acs} ACS and "
                  f"{low_stats.get('kills', 0)}/{low_stats.get('deaths', 0)}/{low_stats.get('assists', 0)} "
                  f"({low_kd:.2f} KD); {roast}.")
    highlights = _round_highlights(detail or {}, all_teammates, weakest)
    if highlights:
        recap += " " + " ".join(highlights)
    events = []
    for item in tracked:
        name = item["p"].get("name", "A queued player")
        for tag in item["tags"]:
            if tag in ("QUAD", "ACE"):
                events.append(f"{name} landed a {tag.lower()} and immediately became impossible to queue with")
            elif tag == "VICTIM":
                events.append(f"{name} died in every round, a truly committed performance")
            elif tag == "ENTRY":
                events.append(f"{name} kicked open {item['fb']} rounds with first bloods")
            elif tag == "DINK MACHINE":
                events.append(f"{name} hit {item['hs']}% headshots and is now legally allowed to say 'nice crosshair'")
            elif tag == "ASSIST KING":
                events.append(f"{name} handed out {item['st'].get('assists', 0)} assists like party invites")
            elif tag == "DAMAGE DEALER":
                events.append(f"{name} put up {item['adr']} ADR and kept the damage numbers employed")
            elif tag == "SURVIVOR":
                events.append(f"{name} only went down {item['st'].get('deaths', 0)} times and stayed out of the respawn queue")
            elif tag.startswith("CLUTCH"):
                events.append(f"{name} won {tag.rsplit('x', 1)[-1]} clutch round(s) and will mention it all week")
    if events and not highlights:
        recap += " " + "; ".join(events[:2]) + "."
    return recap


def _round_highlights(detail, teammates, weakest):
    """Describe observed kill events without inventing unavailable match details."""
    kills = [event for event in (detail.get("kills") or [])
             if isinstance(event.get("round"), int) and event.get("round") >= 0]
    if not kills:
        return []
    by_id = {player.get("puuid"): player for player in teammates if player.get("puuid")}
    round_offset = 1 if any(event["round"] == 0 for event in kills) else 0
    grouped = {}
    for event in kills:
        killer = (event.get("killer") or {}).get("puuid")
        if killer in by_id:
            key = (event["round"], killer)
            grouped[key] = grouped.get(key, 0) + 1
    highlights = []
    if grouped:
        (round_number, killer), count = max(grouped.items(), key=lambda item: item[1])
        if count >= 3:
            name = by_id[killer].get("name", "A teammate")
            highlights.append(f"In round {round_number + round_offset}, {name} dropped {count} kills; "
                              "the sweaty bastard will never shut up about it.")
    first_by_round = {}
    for event in kills:
        timestamp = event.get("time_in_round_in_ms")
        if not isinstance(timestamp, (int, float)):
            continue
        old = first_by_round.get(event["round"])
        if old is None or timestamp < old["time_in_round_in_ms"]:
            first_by_round[event["round"]] = event
    victim_id = weakest.get("puuid")
    early_deaths = [number for number, event in first_by_round.items()
                    if victim_id and (event.get("victim") or {}).get("puuid") == victim_id]
    if early_deaths:
        number = min(early_deaths) + round_offset
        name = weakest.get("name", "A teammate")
        highlights.append(f"{name} donated first blood {len(early_deaths)} time(s), including round {number}; "
                          "apparently waiting for the team was too fucking complicated.")
    if not highlights and first_by_round:
        counts = {}
        for event in first_by_round.values():
            killer = (event.get("killer") or {}).get("puuid")
            if killer in by_id:
                counts[killer] = counts.get(killer, 0) + 1
        if counts:
            killer = max(counts, key=counts.get)
            highlights.append(f"{by_id[killer].get('name', 'A teammate')} opened {counts[killer]} rounds "
                              "with first blood while the rest found their damn keyboards.")
    return highlights


def _play_headline(all_teammates, our_score, their_score, registered_only=False):
    if not all_teammates:
        return "MATCH RECAP"
    weakest = min(all_teammates, key=lambda p: (p.get("stats") or {}).get("score", 0))
    strongest = max(all_teammates, key=lambda p: (p.get("stats") or {}).get("score", 0))
    stats = weakest.get("stats") or {}
    acs = round(stats.get("score", 0) / max(our_score + their_score, 1))
    name = weakest.get("name", "PLAYER").upper()
    if registered_only and len(all_teammates) == 1:
        return f"{name}'S MATCH RECAP"
    if weakest.get("puuid") == strongest.get("puuid"):
        return f"{name} WAS BOTH THE CARRY AND THE CONTENT"
    if stats.get("deaths", 0) >= 18:
        return f"{name}'S RESPAWN BUTTON MVP RUN"
    if acs < 100:
        return f"{name}'S CROSSHAIR TOOK THE NIGHT OFF"
    return f"{name}'S LOWEST IMPACT: {acs} ACS"


def _deaths_by_round(detail):
    deaths = {}
    for event in detail.get("kills") or []:
        victim = (event.get("victim") or {}).get("puuid")
        if victim:
            round_number = event.get("round", 0) or 0
            deaths.setdefault(round_number, {})[victim] = deaths.setdefault(round_number, {}).get(victim, 0) + 1
    return deaths


def _clutch_wins(detail, players):
    """Count won rounds where a player was left alone against at least two opponents."""
    roster = detail.get("players") or players
    team_by_puuid = {p.get("puuid"): p.get("team_id") for p in roster if p.get("puuid") and p.get("team_id")}
    members_by_team = {}
    for puuid, team_id in team_by_puuid.items():
        members_by_team.setdefault(team_id, set()).add(puuid)
    if len(members_by_team) < 2:
        return {}
    kills_by_round = {}
    for event in detail.get("kills") or []:
        kills_by_round.setdefault(event.get("round", 0) or 0, []).append(event)
    clutch_counts = {}
    for index, round_data in enumerate(detail.get("rounds") or []):
        round_number = round_data.get("id", index)
        winner_text = str(round_data.get("winning_team") or "").casefold()
        winner = next((team for team in members_by_team if str(team).casefold() == winner_text), None)
        if winner is None:
            continue
        alive = {team: set(puuids) for team, puuids in members_by_team.items()}
        possible_clutchers = set()
        events = sorted(kills_by_round.get(round_number, []), key=lambda event: event.get("time_in_round_in_ms", 0))
        for event in events:
            victim = (event.get("victim") or {}).get("puuid")
            victim_team = team_by_puuid.get(victim)
            if victim and victim_team in alive:
                alive[victim_team].discard(victim)
            for team, team_players in alive.items():
                opponents_alive = sum(len(other) for other_id, other in alive.items() if other_id != team)
                if len(team_players) == 1 and opponents_alive >= 2:
                    possible_clutchers.update(team_players)
        for puuid in possible_clutchers:
            if team_by_puuid.get(puuid) == winner and puuid in alive.get(winner, set()):
                clutch_counts[puuid] = clutch_counts.get(puuid, 0) + 1
    return clutch_counts
