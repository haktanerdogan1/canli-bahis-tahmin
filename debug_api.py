import requests
import json

HOST = "free-api-live-football-data.p.rapidapi.com"
HEADERS = {
    "x-rapidapi-host": HOST,
    "x-rapidapi-key": "0621aca909msh11528db8e0c6d8ap14b8a4jsn5581eeb52cc9"
}

resp = requests.get(f"https://{HOST}/football-current-live", headers=HEADERS)
data = resp.json()
for m in data.get("response", {}).get("live", []):
    m_id = m["id"]
    st_resp = requests.get(f"https://{HOST}/football-get-match-event-all-stats?eventid={m_id}", headers=HEADERS)
    st_data = st_resp.json()
    print(f"Match {m['home']['name']} vs {m['away']['name']} (ID: {m_id})")
    for group in st_data.get("response", {}).get("stats", []):
        for item in group.get("stats", []):
            if item.get("key") == "BallPossesion":
                print("  Possession:", item.get("stats"))
            elif item.get("key") == "corners":
                print("  Corners:", item.get("stats"))
            elif item.get("key") == "total_shots":
                print("  Total Shots:", item.get("stats"))
