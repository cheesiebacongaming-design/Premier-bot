"""VALORANT match and Premier history retrieval."""

import asyncio
from urllib.parse import quote


class MatchHistoryService:
    """Retrieves match histories and details through injected API helpers."""

    def __init__(self, cached_api, api, region, roster=(), team_id=None,
                 team_name="", team_tag=""):
        self.cached_api = cached_api
        self.api = api
        self.region = region
        self.roster = tuple(roster)
        self.team_id = team_id
        self.team_name = team_name
        self.team_tag = team_tag

    def team_path(self):
        if self.team_id:
            return f"/valorant/v1/premier/{self.team_id}"
        return f"/valorant/v1/premier/{quote(self.team_name)}/{quote(self.team_tag)}"

    async def team_data(self):
        """Live roster lookup (v2), falling back to the v1 team index."""
        v2 = (
            f"/valorant/v2/premier/teams/{self.region}/{self.team_id}"
            if self.team_id
            else f"/valorant/v2/premier/teams/{self.region}/{quote(self.team_name)}/{quote(self.team_tag)}"
        )
        try:
            return await self.cached_api(v2, 3600)
        except Exception:
            return await self.cached_api(self.team_path(), 3600)

    async def team_members(self):
        try:
            team = await self.team_data()
        except Exception:
            if self.roster:
                return {entry.lower() for entry in self.roster}
            raise
        keys = set()
        for member in team.get("member") or team.get("members") or []:
            if member.get("puuid"):
                keys.add(member["puuid"].lower())
            if member.get("name"):
                keys.add(f"{member['name']}#{member.get('tag', '')}".lower())
        return keys

    async def history_from_players(self):
        """Collect and deduplicate match IDs from configured player histories."""
        if self.roster:
            found, error = {}, None

            async def fetch_player(entry):
                name, _, tag = entry.rpartition("#")
                return await self.cached_api(
                    f"/valorant/v1/stored-matches/{self.region}/{quote(name.strip())}/{quote(tag.strip())}",
                    600, mode="premier", size=20,
                )

            results = await asyncio.gather(
                *(fetch_player(entry) for entry in self.roster), return_exceptions=True
            )
            for items in results:
                if isinstance(items, Exception):
                    error = items
                    continue
                for item in items or []:
                    meta = item.get("meta", {})
                    if meta.get("id"):
                        found[meta["id"]] = {
                            "id": meta["id"], "started_at": meta.get("started_at")
                        }
            if not found and error:
                raise error
            return sorted(found.values(), key=lambda match: match["started_at"] or "", reverse=True)

        team = await self.team_data()
        puuids = [
            member["puuid"]
            for member in (team.get("member") or team.get("members") or [])
            if member.get("puuid")
        ][:5]

        async def fetch_player_matches(puuid):
            return await self.cached_api(
                f"/valorant/v4/by-puuid/matches/{self.region}/pc/{puuid}",
                600, mode="premier", size=10,
            )

        results = await asyncio.gather(
            *(fetch_player_matches(puuid) for puuid in puuids), return_exceptions=True
        )
        seen, last_error = {}, None
        for items in results:
            if isinstance(items, Exception):
                last_error = items
                continue
            for item in items or []:
                meta = item.get("metadata", {})
                match_id = meta.get("match_id")
                if not match_id:
                    continue
                rosters = [
                    (team.get("premier_roster") or {}).get("id")
                    for team in item.get("teams", [])
                ]
                rosters = [roster_id for roster_id in rosters if roster_id]
                if self.team_id and rosters and self.team_id not in rosters:
                    continue
                seen[match_id] = {"id": match_id, "started_at": meta.get("started_at")}
        if not seen and last_error:
            raise last_error
        return sorted(seen.values(), key=lambda match: match["started_at"] or "", reverse=True)

    async def history(self):
        try:
            data = await self.cached_api(self.team_path() + "/history", 600)
            matches = data.get("league_matches", []) if isinstance(data, dict) else data
            if matches:
                return sorted(matches, key=lambda match: match.get("started_at") or "", reverse=True)
        except Exception:
            # Team-index misses fall back to the individual player histories.
            pass
        return await self.history_from_players()

    async def match_detail(self, match_id, region=None):
        return await self.api(f"/valorant/v4/match/{region or self.region}/{match_id}")
