import aiohttp
import asyncio
import json

async def main():
    API_KEY = "0621aca909msh11528db8e0c6d8ap14b8a4jsn5581eeb52cc9"
    HOST = "free-api-live-football-data.p.rapidapi.com"
    
    endpoints = [
        "/football-get-team-recent-matches",
        "/football-get-matches-by-date",
        "/football-get-all-matches-by-team"
    ]
    
    async with aiohttp.ClientSession() as session:
        for ep in endpoints:
            url = f"https://{HOST}{ep}"
            async with session.get(url, params={"teamid": "9893"}, headers={"x-rapidapi-host": HOST, "x-rapidapi-key": API_KEY}) as resp:
                data = await resp.json()
                print(f"{ep}: {data.get('message', 'SUCCESS')}")
                    
asyncio.run(main())
