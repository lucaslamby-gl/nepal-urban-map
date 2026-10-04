"""Step 4: bundle processed data into web/data for the static page."""
import json, shutil, os, pandas as pd, geopandas as gpd
OUT, WEB = 'data/processed', 'docs/data'
os.makedirs(WEB, exist_ok=True)
tot = json.load(open(f'{OUT}/totals.json')); Y0, Y1 = tot['years']
for y in (Y0, Y1):
    df = pd.read_csv(f'{OUT}/hex_{y}.csv')
    json.dump({'h3': df['h3'].tolist(), 'pop': df['pop'].tolist()}, open(f'{WEB}/hex_{y}.json', 'w'), separators=(',', ':'))
uc = gpd.read_file(f'{OUT}/urban_centres.geojson').sort_values('id')
rt = json.load(open(f'{OUT}/routes.json'))
uc['elev'] = rt['elevation']
for k in ('municipalities', 'mun_pop2021'):
    uc[k] = uc[k].apply(lambda m: m if isinstance(m, list) else json.loads(m) if isinstance(m, str) and m.startswith('[') else [m])
uc.to_file(f'{WEB}/urban_centres.geojson', driver='GeoJSON')
for f in ('roads.geojson', 'crossings.geojson', 'nepal_outline.geojson', 'municipalities.geojson', f'urban_centres_{Y0}.geojson'): shutil.copy(f'{OUT}/{f}', f'{WEB}/{f}')
json.dump({'matrix': rt['matrix'], 'routes': rt['routes']}, open(f'{WEB}/routes.json', 'w'), separators=(',', ':'))
json.dump(tot, open(f'{WEB}/totals.json', 'w'))
for f in sorted(os.listdir(WEB)): print(f, round(os.path.getsize(f'{WEB}/{f}') / 1024), 'KB')
