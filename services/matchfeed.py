"""Pure matchfeed helpers shared by polling and notification tests."""


def group_registered_players(players, linked_accounts):
    """Group linked players found in a match by team ID.

    ``linked_accounts`` maps case-folded Riot IDs to account entries containing
    a set of Discord ``user_ids``.
    """
    groups = {}
    for player in players or []:
        riot_key = f"{player.get('name', '')}#{player.get('tag', '')}".casefold()
        if riot_key not in linked_accounts or not player.get("team_id"):
            continue
        group = groups.setdefault(str(player["team_id"]), {
            "account_keys": set(), "user_ids": set(), "players": [],
        })
        group["account_keys"].add(riot_key)
        group["user_ids"].update(linked_accounts[riot_key].get("user_ids", set()))
        group["players"].append(player)
    return groups


def format_match_notification(user_ids, result_word, score, map_name):
    """Format a matchfeed notification while keeping mentions explicit."""
    mentions = " ".join(f"<@{user_id}>" for user_id in dict.fromkeys(user_ids))
    return f"{mentions} {result_word} {score} on {map_name}!"
