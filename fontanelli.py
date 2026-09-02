import requests
import json

# Bounding box approssimativo del Comune di Firenze (south, west, north, east)
bbox = "43.72,11.13,43.85,11.32"

query = f"""
[out:json][timeout:60];
(
  node["amenity"="drinking_water"]({bbox});
  node["man_made"="water_tap"]["drinking_water"="yes"]({bbox});
);
out body;
"""

url = "https://overpass-api.de/api/interpreter"

r = requests.post(url, data={"data": query})

if r.status_code != 200:
    print(f"Errore {r.status_code}")
    print(r.text[:2000])
else:
    data = r.json()
    with open("fontanelli_firenze.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Trovati {len(data['elements'])} fontanelli, salvati in fontanelli_firenze.json")