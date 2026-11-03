import random


def _play_recap(map_name, won, our_score, their_score, tracked, all_teammates, duration):
    result = "snatched the win" if won else "took the L"
    recap = f"Your queue {result} on {map_name} after {duration} of competitive Valorant, {our_score}-{their_score}."
    if not all_teammates:
        return recap
    ranked = sorted(all_teammates, key=lambda p: (p.get("stats") or {}).get("score", 0), reverse=True)
    strongest, weakest = ranked[0], ranked[-1]
    top_stats, low_stats = strongest.get("stats") or {}, weakest.get("stats") or {}
    strongest_name = strongest.get("name", "A teammate")
    top_acs = round(top_stats.get("score", 0) / max(our_score + their_score, 1))
    top_kd = top_stats.get("kills", 0) / max(top_stats.get("deaths", 0), 1)
    top_title = "the team's certified tryhard" if top_acs >= 220 or top_kd >= 1.2 else "the designated carry"
    recap += (f" {strongest_name} clocked in as {top_title}, topping the team with {top_acs} ACS, "
              f"{top_stats.get('kills', 0)}/{top_stats.get('deaths', 0)}/{top_stats.get('assists', 0)} "
              f"and a {top_kd:.2f} KD. Someone's definitely watching the VOD back tonight.")
    if strongest.get("puuid") != weakest.get("puuid"):
        low_name = weakest.get("name", "a teammate")
        low_acs = round(low_stats.get("score", 0) / max(our_score + their_score, 1))
        low_kd = low_stats.get("kills", 0) / max(low_stats.get("deaths", 0), 1)
        if low_stats.get("deaths", 0) >= 18:
            roast = random.choice(("the respawn screen got more quality time than the team", "collected deaths like loyalty stamps", "the enemy team learned their route to spawn by heart"))
        elif low_acs < 100:
            roast = random.choice(("their crosshair was apparently sightseeing", "their aim took an unscheduled lunch break", "the bullets were social distancing"))
        elif low_kd < 0.6:
            roast = random.choice(("the kill feed was not their biggest fan", "their KD ratio needs a pep talk", "the duels were a generous donation program"))
        else:
            roast = random.choice(("it was a character-building shift", "the scoreboard has chosen violence", "one to quietly blame on the warm-up"))
        recap += (f" {low_name} had the roughest shift: {low_acs} ACS and "
                  f"{low_stats.get('kills', 0)}/{low_stats.get('deaths', 0)}/{low_stats.get('assists', 0)} "
                  f"({low_kd:.2f} KD); {roast}.")
    high_deaths = max(all_teammates, key=lambda p: (p.get("stats") or {}).get("deaths", 0))
    high_death_stats = high_deaths.get("stats") or {}
    if high_deaths.get("puuid") != weakest.get("puuid") and high_death_stats.get("deaths", 0) >= 18:
        recap += (f" {high_deaths.get('name', 'A teammate')} also took {high_death_stats.get('deaths', 0)} trips "
                  f"back to spawn ({high_death_stats.get('kills', 0)}/{high_death_stats.get('deaths', 0)}/{high_death_stats.get('assists', 0)}); "
                  "the walk back is basically muscle memory now.")
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
    if events:
        recap += " " + "; ".join(events[:2]) + "."
    margin = abs(our_score - their_score)
    recap += f" The final margin was {margin} round{'s' if margin != 1 else ''}; the group chat has enough material for the post-game review."
    return recap


def _play_headline(all_teammates, our_score, their_score):
    if not all_teammates:
        return "MATCH RECAP"
    weakest = min(all_teammates, key=lambda p: (p.get("stats") or {}).get("score", 0))
    strongest = max(all_teammates, key=lambda p: (p.get("stats") or {}).get("score", 0))
    stats = weakest.get("stats") or {}
    acs = round(stats.get("score", 0) / max(our_score + their_score, 1))
    name = weakest.get("name", "PLAYER").upper()
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
