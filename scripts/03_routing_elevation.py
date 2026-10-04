"""
Step 3: driving distance matrix (OSRM, OpenStreetMap roads), route geometries for the links shown on the
map, and elevation of each urban centre (Open-Meteo elevation API, Copernicus DEM 90 m).

Outputs: data/processed/routes.json  {matrix: {km: [[...]], min: [[...]]}, routes: {"i-j": [[lon,lat],...]}, elevation: [...]}
"""
import json, time, requests, geopandas as gpd
UA = {'User-Agent': 'GrowthLab-NepalUrbanMap/0.1 (research)'}
uc = gpd.read_file('data/processed/urban_centres.geojson').sort_values('id')
coords = [(r.lon, r.lat) for r in uc.itertuples()]
n = len(coords)
print('centres', n)

# --- elevation (Copernicus DEM via Open-Meteo)
r = requests.get('https://api.open-meteo.com/v1/elevation', params={'latitude': ','.join(str(c[1]) for c in coords), 'longitude': ','.join(str(c[0]) for c in coords)}, headers=UA, timeout=60)
elev = [round(e) for e in r.json()['elevation']]
print('elevation', elev)

# --- OSRM table (public demo server; one request)
cs = ';'.join(f'{x:.5f},{y:.5f}' for x, y in coords)
r = requests.get(f'https://router.project-osrm.org/table/v1/driving/{cs}', params={'annotations': 'distance,duration'}, headers=UA, timeout=120)
t = r.json(); assert t['code'] == 'Ok', t
km = [[(d or 0) / 1000 for d in row] for row in t['distances']]
mins = [[(d or 0) / 60 for d in row] for row in t['durations']]
print('matrix ok; Kathmandu row (km):', [round(x) for x in km[0]])

# --- route geometries for each centre's 6 nearest neighbours by road (dedupe pairs)
pairs = set()
for i in range(n):
    order = sorted((km[i][j], j) for j in range(n) if j != i)[:6]
    for _, j in order: pairs.add(tuple(sorted((i, j))))
routes = {}
for i, j in sorted(pairs):
    a, b = coords[i], coords[j]
    r = requests.get(f'https://router.project-osrm.org/route/v1/driving/{a[0]:.5f},{a[1]:.5f};{b[0]:.5f},{b[1]:.5f}', params={'overview': 'simplified', 'geometries': 'geojson'}, headers=UA, timeout=60)
    d = r.json()
    if d.get('code') == 'Ok':
        g = d['routes'][0]['geometry']['coordinates']
        routes[f'{i}-{j}'] = [[round(x, 4), round(y, 4)] for x, y in g[::max(1, len(g) // 150)]] + [[round(g[-1][0], 4), round(g[-1][1], 4)]]
    else:
        print('route failed', i, j, d.get('code'))
    time.sleep(0.6)
print('routes', len(routes))
json.dump({'matrix': {'km': [[round(x, 1) for x in row] for row in km], 'min': [[round(x) for x in row] for row in mins]}, 'routes': routes, 'elevation': elev},
          open('data/processed/routes.json', 'w'))
print('done')
