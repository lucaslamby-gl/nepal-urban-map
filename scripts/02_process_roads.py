"""Step 2: simplify OSM major roads and write border crossings."""
import json, geopandas as gpd
from shapely.geometry import LineString, Point
from shapely.ops import linemerge, unary_union

d = json.load(open('data/raw/roads_osm.json', encoding='utf-8'))
nepal = gpd.read_file('data/raw/npl_adm0.geojson').geometry.iloc[0].buffer(0.01)
rows = []
for e in d['elements']:
    if e['type'] != 'way' or 'geometry' not in e or len(e['geometry']) < 2: continue
    t = e['tags']; cls = t.get('highway')
    rows.append({'cls': cls, 'ref': t.get('ref', ''), 'name': t.get('name', ''), 'geometry': LineString([(p['lon'], p['lat']) for p in e['geometry']])})
g = gpd.GeoDataFrame(rows, crs=4326)
g = g[g.intersects(nepal)]
print('ways in Nepal', len(g), g['cls'].value_counts().to_dict())

out = []
for cls, grp in g.groupby('cls'):
    merged = linemerge(unary_union(grp.geometry))
    lines = list(merged.geoms) if merged.geom_type == 'MultiLineString' else [merged]
    for ln in lines:
        ln = ln.simplify(0.0008)
        if ln.length > 0.02: out.append({'cls': cls, 'geometry': ln})
roads = gpd.GeoDataFrame(out, crs=4326)
roads['geometry'] = roads.geometry.set_precision(0.0001)
roads.to_file('data/processed/roads.geojson', driver='GeoJSON')
print('road segments', len(roads), 'file KB', round(__import__('os').path.getsize('data/processed/roads.geojson') / 1024))

# Border crossings (hand-curated; main road crossings with India and China)
xings = [
 ('Kakarbhitta – Panitanki', 88.154, 26.646, 'IN'), ('Biratnagar – Jogbani', 87.273, 26.403, 'IN'),
 ('Birgunj – Raxaul', 84.878, 26.995, 'IN'), ('Bhairahawa – Sunauli', 83.425, 27.481, 'IN'),
 ('Nepalgunj – Rupaidiha', 81.633, 28.004, 'IN'), ('Dhangadhi – Gauriphanta', 80.577, 28.636, 'IN'),
 ('Mahendranagar – Banbasa', 80.077, 28.956, 'IN'), ('Jaleshwar – Bhitamore', 85.801, 26.631, 'IN'),
 ('Krishnanagar – Barhni', 82.942, 27.520, 'IN'), ('Rajbiraj – Kunauli', 86.750, 26.470, 'IN'),
 ('Rasuwagadhi – Kerung', 85.380, 28.277, 'CN'), ('Tatopani – Zhangmu', 85.964, 27.973, 'CN'),
]
gpd.GeoDataFrame([{'name': n, 'country': c, 'geometry': Point(x, y)} for n, x, y, c in xings], crs=4326).to_file('data/processed/crossings.geojson', driver='GeoJSON')
print('crossings written')
