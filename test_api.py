import aiohttp
import asyncio
import json

async def main():
    API_KEY = "0621aca909msh11528db8e0c6d8ap14b8a4jsn5581eeb52cc9"
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
