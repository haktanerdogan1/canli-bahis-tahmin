import asyncio
import aiohttp
import json

API_KEY = "0621aca909msh11528db8e0c6d8ap14b8a4jsn5581eeb52cc9"
HOST = "free-api-live-football-data.p.rapidapi.com"
HEADERS = {
    "x-rapidapi-host": HOST,
    "x-rapidapi-key": API_KEY
}

async def fetch_live():
    url_live = f"https://{HOST}/football-current-live"
    async with aiohttp.ClientSession() as session:
        async with session.get(url_live, headers=HEADERS) as resp:
            data = await resp.json()
            matches = data.get("response", {}).get("live", [])
            if not matches:
                print("No live matches")
                return
            
            # Find a match that is in progress to ensure stats exist
            match = None
            for m in matches:
                if m.get("status", {}).get("liveTime", {}).get("short"):
                    try:
                        minute = int(m["status"]["liveTime"]["short"].replace("'", "").replace("’", "").strip())
                        if minute > 10:
                            match = m
                            break
                    except:
                        pass
            
            if not match:
                match = matches[0]

            print(f"Fetching stats for {match['home']['name']} vs {match['away']['name']} (ID: {match['id']})")
            
            url_stats = f"https://{HOST}/football-get-match-event-firstHalf-stats"
            params = {"eventid": match["id"]}
            
            # also test the all-stats endpoint to see if they differ
            url_stats_all = f"https://{HOST}/football-get-match-event-all-stats"
            
            async with session.get(url_stats_all, params=params, headers=HEADERS) as resp2:
                stats_data = await resp2.json()
                print(json.dumps(stats_data, indent=2))

if __name__ == "__main__":
    asyncio.run(fetch_live())
