"""Warcraft Logs API v2 client using OAuth2 client credentials + GraphQL."""

import requests


class WCLClient:
    TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
    API_URL = "https://www.warcraftlogs.com/api/v2/client"
    TIMEOUT_S = 30

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None

    def authenticate(self):
        resp = requests.post(
            self.TOKEN_URL,
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
            timeout=self.TIMEOUT_S,
        )
        resp.raise_for_status()
        self.token = resp.json()["access_token"]

    def _post_query(self, payload: dict) -> requests.Response:
        return requests.post(
            self.API_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
            timeout=self.TIMEOUT_S,
        )

    def query(self, graphql_query: str, variables: dict | None = None) -> dict:
        if not self.token:
            self.authenticate()
        payload = {"query": graphql_query}
        if variables:
            payload["variables"] = variables
        resp = self._post_query(payload)
        # Tokens expire, which matters for a long-running server
        if resp.status_code == 401:
            self.authenticate()
            resp = self._post_query(payload)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL errors: {data['errors']}")
        return data["data"]

    def get_report_fights_and_players(self, report_code: str) -> dict:
        result = self.query("""
            query($code: String!) {
                reportData {
                    report(code: $code) {
                        title
                        startTime
                        endTime
                        fights(translate: true) {
                            id
                            name
                            startTime
                            endTime
                            kill
                            difficulty
                            encounterID
                            keystoneLevel
                        }
                        masterData(translate: true) {
                            actors {
                                id
                                name
                                server
                                subType
                                type
                            }
                            abilities {
                                gameID
                                name
                                type
                            }
                        }
                    }
                }
            }
        """, {"code": report_code})
        return result["reportData"]["report"]

    def get_cast_events(
        self,
        report_code: str,
        fight_id: int,
        source_id: int,
        start_time: float,
        end_time: float,
    ) -> list[dict]:
        """Fetch all cast events for a player in a fight, handling pagination."""
        all_events = []
        current_start = start_time

        while True:
            result = self.query("""
                query($code: String!, $startTime: Float!, $endTime: Float!,
                      $fightIDs: [Int!], $sourceID: Int!) {
                    reportData {
                        report(code: $code) {
                            events(
                                startTime: $startTime
                                endTime: $endTime
                                fightIDs: $fightIDs
                                dataType: Casts
                                sourceID: $sourceID
                                limit: 10000
                                includeResources: true
                            ) {
                                data
                                nextPageTimestamp
                            }
                        }
                    }
                }
            """, {
                "code": report_code,
                "startTime": current_start,
                "endTime": end_time,
                "fightIDs": [fight_id],
                "sourceID": source_id,
            })

            events_data = result["reportData"]["report"]["events"]
            all_events.extend(events_data["data"])

            if events_data["nextPageTimestamp"] is None:
                break
            current_start = events_data["nextPageTimestamp"]

        return all_events

    def get_events(
        self,
        report_code: str,
        fight_id: int,
        data_type: str,
        start_time: float,
        end_time: float,
        source_id: int | None = None,
        hostility: str = "Friendlies",
    ) -> list[dict]:
        """Fetch all events of any type in a fight, handling pagination."""
        all_events = []
        current_start = start_time

        while True:
            result = self.query("""
                query($code: String!, $startTime: Float!, $endTime: Float!,
                      $fightIDs: [Int!], $sourceID: Int,
                      $dataType: EventDataType, $hostility: HostilityType) {
                    reportData {
                        report(code: $code) {
                            events(
                                startTime: $startTime
                                endTime: $endTime
                                fightIDs: $fightIDs
                                dataType: $dataType
                                sourceID: $sourceID
                                hostilityType: $hostility
                                limit: 10000
                                includeResources: true
                            ) {
                                data
                                nextPageTimestamp
                            }
                        }
                    }
                }
            """, {
                "code": report_code,
                "startTime": current_start,
                "endTime": end_time,
                "fightIDs": [fight_id],
                "sourceID": source_id,
                "dataType": data_type,
                "hostility": hostility,
            })

            events_data = result["reportData"]["report"]["events"]
            all_events.extend(events_data["data"])

            if events_data["nextPageTimestamp"] is None:
                break
            current_start = events_data["nextPageTimestamp"]

        return all_events

    def get_fight_details(self, report_code: str, fight_id: int) -> dict:
        """Fetch one fight with its keystone info and dungeon pulls."""
        result = self.query("""
            query($code: String!, $fightIDs: [Int!]) {
                reportData {
                    report(code: $code) {
                        fights(fightIDs: $fightIDs) {
                            id
                            name
                            startTime
                            endTime
                            kill
                            keystoneLevel
                            averageItemLevel
                            dungeonPulls {
                                id
                                name
                                startTime
                                endTime
                                kill
                            }
                        }
                    }
                }
            }
        """, {"code": report_code, "fightIDs": [fight_id]})
        return result["reportData"]["report"]["fights"][0]

    def get_fight_summary(self, report_code: str, fight_id: int) -> dict:
        """Fetch the summary table, which holds the roster for that fight."""
        result = self.query("""
            query($code: String!, $fightIDs: [Int!]) {
                reportData {
                    report(code: $code) {
                        table(
                            fightIDs: $fightIDs
                            dataType: Summary
                            startTime: 0
                            endTime: 999999999999
                        )
                    }
                }
            }
        """, {"code": report_code, "fightIDs": [fight_id]})
        return result["reportData"]["report"]["table"]["data"]

    def get_damage_done_table(
        self,
        report_code: str,
        fight_id: int,
        source_id: int,
        start_time: float,
        end_time: float,
    ) -> dict:
        """Fetch the damage-done table for a player, broken down by target."""
        result = self.query("""
            query($code: String!, $startTime: Float!, $endTime: Float!,
                  $fightIDs: [Int!], $sourceID: Int!) {
                reportData {
                    report(code: $code) {
                        table(
                            startTime: $startTime
                            endTime: $endTime
                            fightIDs: $fightIDs
                            dataType: DamageDone
                            sourceID: $sourceID
                            hostilityType: Enemies
                        )
                    }
                }
            }
        """, {
            "code": report_code,
            "startTime": start_time,
            "endTime": end_time,
            "fightIDs": [fight_id],
            "sourceID": source_id,
        })
        return result["reportData"]["report"]["table"]["data"]

    def get_damage_by_target(
        self,
        report_code: str,
        fight_id: int,
        source_id: int,
        start_time: float,
        end_time: float,
    ) -> dict:
        """Fetch damage-done events and aggregate by target."""
        all_events = []
        current_start = start_time

        while True:
            result = self.query("""
                query($code: String!, $startTime: Float!, $endTime: Float!,
                      $fightIDs: [Int!], $sourceID: Int!) {
                    reportData {
                        report(code: $code) {
                            events(
                                startTime: $startTime
                                endTime: $endTime
                                fightIDs: $fightIDs
                                dataType: DamageDone
                                sourceID: $sourceID
                                limit: 10000
                            ) {
                                data
                                nextPageTimestamp
                            }
                        }
                    }
                }
            """, {
                "code": report_code,
                "startTime": current_start,
                "endTime": end_time,
                "fightIDs": [fight_id],
                "sourceID": source_id,
            })

            events_data = result["reportData"]["report"]["events"]
            all_events.extend(events_data["data"])

            if events_data["nextPageTimestamp"] is None:
                break
            current_start = events_data["nextPageTimestamp"]

        return all_events
