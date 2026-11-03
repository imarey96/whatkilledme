"""Warcraft Logs API v2 client using OAuth2 client credentials + GraphQL."""

import os
import re

import requests


def load_env():
    """Load .env file if it exists (simple key=value parser)."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())


def extract_report_code(url_or_code: str) -> str:
    """Extract report code from a URL or return as-is if already a code."""
    match = re.search(r"warcraftlogs\.com/reports/((?:a:)?[A-Za-z0-9]+)", url_or_code)
    if match:
        return match.group(1)
    # Strip any URL fragments like #fight=...
    return url_or_code.split("#")[0].split("?")[0].strip()


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
                            fightPercentage
                        }
                        phases {
                            encounterID
                            phases {
                                id
                                name
                            }
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

    def get_events(
        self,
        report_code: str,
        fight_id: int,
        data_type: str,
        start_time: float,
        end_time: float,
        source_id: int | None = None,
        hostility: str = "Friendlies",
        target_id: int | None = None,
    ) -> list[dict]:
        """Fetch all events of any type in a fight, handling pagination."""
        all_events = []
        current_start = start_time

        while True:
            result = self.query("""
                query($code: String!, $startTime: Float!, $endTime: Float!,
                      $fightIDs: [Int!], $sourceID: Int, $targetID: Int,
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
                                targetID: $targetID
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
                "targetID": target_id,
            })

            events_data = result["reportData"]["report"]["events"]
            all_events.extend(events_data["data"])

            if events_data["nextPageTimestamp"] is None:
                break
            current_start = events_data["nextPageTimestamp"]

        return all_events

    def get_fight_details(self, report_code: str, fight_id: int) -> dict:
        """Fetch one fight with its keystone info, dungeon pulls and raid phases."""
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
                            difficulty
                            encounterID
                            keystoneLevel
                            averageItemLevel
                            fightPercentage
                            dungeonPulls {
                                id
                                name
                                startTime
                                endTime
                                kill
                            }
                            phaseTransitions {
                                id
                                startTime
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
