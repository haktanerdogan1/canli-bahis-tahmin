import os
import aiohttp
import asyncio
import json

async def main():
    API_KEY = os.environ.get("RAPIDAPI_KEY")
    if not API_KEY:
        raise RuntimeError("RAPIDAPI_KEY ortam degiskeni tanimli degil.")
    HOST = "free-api-live-football-data.p.rapidapi.com"
    url = f"https://{HOST}/football-current-live"
    headers = {"x-rapidapi-host": HOST, "x-rapidapi-key": API_KEY}
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            live = data.get("response", {}).get("live", [])
            for m in live:
                if "Conference League" in str(m.get("leagueId", "")) or "937351" == str(m.get("leagueId", "")):
                    print(json.dumps(m, indent=2))
                    
asyncio.run(main())
