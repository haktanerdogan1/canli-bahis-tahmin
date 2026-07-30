import aiohttp
import asyncio
import json

async def main():
    API_KEY = "0621aca909msh11528db8e0c6d8ap14b8a4jsn5581eeb52cc9"
    HOST = "free-api-live-football-data.p.rapidapi.com"
    # test match ID for Györi was 5789191
    endpoints = [
        "/football-get-match-odds",
        "/football-match-odds",
        "/football-get-odds"
    ]
    
    async with aiohttp.ClientSession() as session:
        for ep in endpoints:
            url = f"https://{HOST}{ep}"
            async with session.get(url, params={"matchid": "5789191"}, headers={"x-rapidapi-host": HOST, "x-rapidapi-key": API_KEY}) as resp:
                data = await resp.json()
                print(f"{ep}: {data.get('message', 'SUCCESS (has data)')}")
                    
asyncio.run(main())
