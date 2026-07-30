import aiohttp
import asyncio

API_KEY = "0621aca909msh11528db8e0c6d8ap14b8a4jsn5581eeb52cc9"
HOST = "free-api-live-football-data.p.rapidapi.com"
MATCH_ID = "5900866" # Parma vs Arezzo

HEADERS = {
    "x-rapidapi-host": HOST,
    "x-rapidapi-key": API_KEY
}

ENDPOINTS = [
    "football-get-match-statistics",
    "football-match-statistics",
    "football-statistics",
    "statistics",
    "football-get-statistics",
    "football-get-match-details",
    "football-match-details",
    "football-get-events",
    "football-get-match-events",
    "football-match-events",
    "football-events"
]

PARAMS_LIST = [
    {"matchId": MATCH_ID},
    {"match_id": MATCH_ID},
    {"id": MATCH_ID},
    {"fixtureId": MATCH_ID},
    {"fixture_id": MATCH_ID},
]

async def probe():
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        for ep in ENDPOINTS:
            for p in PARAMS_LIST:
                url = f"https://{HOST}/{ep}"
                try:
                    async with session.get(url, params=p, timeout=5) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if "message" not in data or "does not exist" not in str(data):
                                print(f"SUCCESS: {url} with params {p}")
                                print(str(data)[:500])
                                return
                        elif resp.status != 404:
                            print(f"FAILED {url} with {p}: {resp.status}")
                except Exception as e:
                    pass
        print("No endpoint found.")

if __name__ == "__main__":
    asyncio.run(probe())
