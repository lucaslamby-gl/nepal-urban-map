"""
Step 1: census-constrained population hexagons and urban centres for Nepal.

Why this design. Every off-the-shelf population grid (WorldPop, Meta HRSL, GHS-POP, Kontur) was checked
against 2021 census totals for 23 municipalities using the official OCHA boundaries
(data/processed/source_validation.csv). All share the same unit-level errors because they inherit the same
GPW census input table: Butwal at ~0.2x its census population, Punarbas and Pokhariya at ~3x, Kathmandu
metro at 1.6-3x. So the grids are used only for the WITHIN-unit pattern, and each of the 753 local units is
rescaled to its census total.

  2021 layer: Kontur Population 2023 (H3 res 8, built-up informed) x census 2021 (NSO, official xlsx)
  2001 layer: GHS-POP 2000 (100 m, built-up 2000)  -> H3 res 8  x census 2001 re-aggregated to current
              units (citypopulation.de compilation of NSO figures)

Urban centres follow the Degree of Urbanisation (cells >= 1,500/km2, clusters >= 50,000) on a 1 km grid
built from the hexagons.

Outputs (data/processed): hex_2001.csv, hex_2021.csv, urban_centres.geojson, urban_centres_2001.geojson,
  municipalities.geojson, nepal_outline.geojson, totals.json, census_match.csv
"""
import json, re, unicodedata, difflib, numpy as np, pandas as pd, geopandas as gpd, rasterio, h3
from rasterio.merge import merge
from rasterio.features import shapes
from scipy import ndimage
from shapely.geometry import shape
from shapely.ops import unary_union

RAW, OUT = 'data/raw', 'data/processed'
H3_RES = 8; HEX_KM2 = 0.737
Y0, Y1 = 2001, 2021

# ---------------- boundaries ----------------
adm3 = gpd.read_file(f'{RAW}/cod_ab/npl_admin3.shp').to_crs(4326)
adm0 = gpd.read_file(f'{RAW}/cod_ab/npl_admin0.shp').to_crs(4326)
nepal_geom = adm0.geometry.iloc[0]
def norm(s):
    s = unicodedata.normalize('NFKD', str(s)).encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'\b(gaunpalika|nagarpalika|mahanagarpalika|upamahanagarpalika|upa-mahanagarpalika|rural municipality|municipality|sub-metropolis|metropolis|sub-metropolitan city|metropolitan city|sub-metropolitian city|metropolitian city)\b', '', s)
    return re.sub(r'[^a-z]', '', s)
DIST_ALIAS = {'chitwan': 'chitawan', 'dangdeukhuri': 'dang', 'dang': 'dang', 'kanchanpurdodharachandani': 'kanchanpur', 'kapilvastu': 'kapilbastu',
              'nawalpurnawalparasieast': 'nawalparasieast', 'nawalpur': 'nawalparasieast', 'parasinawalparasiwest': 'nawalparasiwest', 'parasi': 'nawalparasiwest',
              'eastrukum': 'rukumeast', 'westrukum': 'rukumwest', 'rukumeast': 'rukumeast', 'rukumwest': 'rukumwest', 'sindhupalchowk': 'sindhupalchok',
              'tanahun': 'tanahu', 'tehrathum': 'terhathum', 'nawalparasieast': 'nawalparasieast', 'nawalparasiwest': 'nawalparasiwest'}
dkey = lambda s: DIST_ALIAS.get(norm(s), norm(s))
adm3['key'] = adm3['adm3_name'].map(norm); adm3['dkey'] = adm3['adm2_name'].map(dkey)
PARKS = adm3['adm3_name'].str.contains('Park|Reserve|Koshi Tappu|Khaptad|Shuklaphanta|Chitawan$|Langtang|Dhorpatan|Shivapuri|Makalu|Sagarmatha|Rara|Shey|Banke$|Bardiya$|Parsa$', regex=True) & ~adm3['adm3_name'].isin(['Parsa'])  # refined below

# ---------------- census 2021 (NSO official) ----------------
raw = pd.read_csv(f'{RAW}/nso_census2021/raw_rows.csv')
raw.columns = ['prov', 'code', 'name', 'hh', 'pop', 'male', 'female', 'c7', 'c8']
rec, dist = [], None
for r in raw.itertuples():
    if pd.isna(r.name): continue
    nm = str(r.name).strip()
    if r.code == 0 or r.code == '0':
        if not nm.isupper() and nm != 'Nepal': dist = nm
        continue
    try: code = int(float(r.code))
    except: continue
    if code == 99 or nm.upper() == 'INSTITUTIONAL': continue
    rec.append({'district': dist, 'name': nm, 'pop2021': int(r.pop)})
c21 = pd.DataFrame(rec); c21['key'] = c21['name'].map(norm); c21['dkey'] = c21['district'].map(dkey)
print('NSO local units parsed:', len(c21), 'sum', f"{c21.pop2021.sum():,}")

# ---------------- census 2001 (re-aggregated to current units; citypopulation.de from NSO) ----------------
cp_all = pd.read_csv(f'{RAW}/census_localunits_citypopulation.csv')
cpd = pd.read_html(__import__('io').StringIO(open(f'{RAW}/citypopulation_nepal_mun_admin.html', encoding='utf-8').read()), flavor='lxml')[0].iloc[:, :7] if __import__('os').path.exists(f'{RAW}/citypopulation_nepal_mun_admin.html') else None
cp = cp_all[~cp_all['status'].isin(['National Park', 'Wildlife Reserve', 'Hunting Reserve', 'Federal Republic'])].copy()
cp['key'] = cp['name'].map(norm); cp['dkey'] = cp['district'].map(dkey)

def match(target, src, label):
    """match census rows (src) onto adm3 (target) by district + normalised name, then fuzzy within district."""
    out = {}
    used = set()
    for i, r in target.iterrows():
        cand = src[src.dkey == r.dkey]
        if cand.empty: continue
        ex = cand[cand.key == r.key]
        if len(ex) == 1: out[i] = ex.index[0]; used.add(ex.index[0]); continue
        keys = [k for k in cand.key if k]
        alt = [re.sub(r'^(khaptad|manang|shuklaphanta|dhorpatan)', '', k) for k in keys]
        if r.key in alt and alt.count(r.key) == 1:
            j = cand.index[alt.index(r.key)]
            if j not in used: out[i] = j; used.add(j); continue
        for cut in (0.75, 0.6):
            best = difflib.get_close_matches(r.key, keys, n=1, cutoff=cut)
            if best:
                j = cand[cand.key == best[0]].index[0]
                if j not in used: out[i] = j; used.add(j)
                break
    print(f'{label}: matched {len(out)} of {len(target)} units; census rows unused: {len(src) - len(used)}')
    return out

units = adm3.copy()   # parks and reserves simply find no census row and drop out below
m21 = match(units, c21, 'census 2021')
m01 = match(units, cp, 'census 2001')
units['pop2021'] = pd.Series({i: c21.loc[j, 'pop2021'] for i, j in m21.items()})
units['census_name'] = pd.Series({i: c21.loc[j, 'name'] for i, j in m21.items()})
units['pop2001'] = pd.Series({i: cp.loc[j, 'pop2001'] for i, j in m01.items()})
units['pop2011'] = pd.Series({i: cp.loc[j, 'pop2011'] for i, j in m01.items()})
un = units[units.pop2021.isna()]
print('unmatched 2021 (treated as unpopulated/park):'); print(un[['adm3_name', 'adm2_name']].to_string())
print('unmatched 2001 among populated units:'); print(units[units.pop2021.notna() & units.pop2001.isna()][['adm3_name', 'adm2_name', 'pop2021']].to_string())
print('2001 census rows unused:'); print(cp[~cp.index.isin(m01.values())][['district', 'name', 'pop2001']].to_string())
unused = c21[~c21.index.isin(m21.values())]; print('NSO rows not matched:'); print(unused[['district', 'name', 'pop2021']].to_string())
units[['adm3_pcode', 'adm3_name', 'adm2_name', 'census_name', 'pop2001', 'pop2011', 'pop2021']].to_csv(f'{OUT}/census_match.csv', index=False)
units = units[units.pop2021.notna()].copy()
units['dkey'] = units['adm2_name'].map(dkey)
# district totals 2001 (same compilation; the District rows of the table), for units without a re-aggregated 2001 figure
drow = pd.read_html(__import__('io').StringIO(__import__('requests').get('https://www.citypopulation.de/en/nepal/mun/admin/', headers={'User-Agent': 'Mozilla/5.0 GrowthLab-NepalUrbanMap/0.1 research'}, timeout=60).text), flavor='lxml')[0].iloc[:, :7]
drow.columns = ['name', 'status', 'translit', 'native', 'pop2001', 'pop2011', 'pop2021']
drow = drow[drow['status'] == 'District'].copy(); drow['dkey'] = drow['name'].map(dkey); drow['pop2001'] = pd.to_numeric(drow['pop2001'], errors='coerce')
D2001 = drow.set_index('dkey')['pop2001'].to_dict()
print('districts with 2001 total:', sum(pd.notna(v) for v in D2001.values()), 'of', len(D2001))
print('matched totals 2021', f"{int(units.pop2021.sum()):,}", ' 2001 (units with figure)', f"{int(units.pop2001.sum()):,}", 'units missing 2001:', int(units.pop2001.isna().sum()))

# ---------------- hexagons ----------------
def hex_from_raster(files, bounds):
    srcs = [rasterio.open(f) for f in files]
    arr, tr = merge(srcs, bounds=bounds); arr = arr[0]
    for s in srcs: s.close()
    arr = np.where(np.isfinite(arr) & (arr > 0), arr, 0)
    rows, cols = np.nonzero(arr > 0.01)
    xs, ys = rasterio.transform.xy(tr, rows, cols, offset='center')
    idx = [h3.latlng_to_cell(y, x, H3_RES) for x, y in zip(xs, ys)]
    return pd.DataFrame({'h3': idx, 'pop': arr[rows, cols]}).groupby('h3', as_index=False)['pop'].sum()

kon = gpd.read_file(f'{RAW}/kontur_NP_2023.gpkg')
hex21 = pd.DataFrame({'h3': kon['h3'], 'pop': kon['population'].astype(float)})
if h3.get_resolution(hex21['h3'].iloc[0]) != H3_RES: raise SystemExit('Kontur resolution unexpected')
ghs = [f'{RAW}/GHS_POP_E2000_GLOBE_R2023A_4326_3ss_V1_0_{t}/GHS_POP_E2000_GLOBE_R2023A_4326_3ss_V1_0_{t}.tif' for t in ('R6_C27', 'R7_C27')]
hex01 = hex_from_raster(ghs, (79.9, 26.2, 88.3, 30.6))
print('raw hexes', len(hex21), len(hex01))

def rescale(hx, popcol, label):
    ll = np.array([h3.cell_to_latlng(h) for h in hx['h3']])
    pts = gpd.GeoDataFrame(hx, geometry=gpd.points_from_xy(ll[:, 1], ll[:, 0]), crs=4326)
    j = gpd.sjoin(pts, units[['adm3_pcode', 'dkey', popcol, 'geometry']], predicate='within', how='inner')
    j = j[~j.index.duplicated()]
    s = j.groupby('adm3_pcode')['pop'].sum()
    U = units.set_index('adm3_pcode')
    f = (U[popcol] / s).replace([np.inf], np.nan)
    # units without a census figure for this year: share the district residual in proportion to the raw grid
    missing = U[U[popcol].isna()]
    if len(missing):
        for dk, grp in missing.groupby('dkey'):
            dtot = D2001.get(dk, np.nan) if popcol == 'pop2001' else np.nan
            known = U[(U.dkey == dk) & U[popcol].notna()][popcol].sum()
            raw_missing = s.reindex(grp.index).fillna(0).sum()
            if pd.notna(dtot) and dtot > known and raw_missing > 0:
                f.loc[grp.index] = (dtot - known) / raw_missing
            else:
                f.loc[grp.index] = 1.0
    j['f'] = j['adm3_pcode'].map(f).fillna(1.0)
    j['pop_s'] = j['pop'] * j['f']
    print(f'{label}: hexes inside units {len(j)}, raw sum {j["pop"].sum():,.0f}, scaled sum {j["pop_s"].sum():,.0f}; scale factor p10/p50/p90 =', np.round(f.quantile([.1, .5, .9]).values, 2))
    out = j[['h3', 'pop_s', 'adm3_pcode']].rename(columns={'pop_s': 'pop'})
    out = out[out['pop'] >= 0.5]; out['pop'] = out['pop'].round().astype(int)
    return out.reset_index(drop=True)

h21 = rescale(hex21, 'pop2021', '2021'); h01 = rescale(hex01, 'pop2001', '2001')
h21[['h3', 'pop']].to_csv(f'{OUT}/hex_{Y1}.csv', index=False); h01[['h3', 'pop']].to_csv(f'{OUT}/hex_{Y0}.csv', index=False)
print('max hex', h21['pop'].max(), h01['pop'].max())

# ---------------- Degree of Urbanisation on a 1 km grid ----------------
RES = 1 / 111.32  # ~1 km in degrees of latitude
def grid(hx):
    ll = np.array([h3.cell_to_latlng(h) for h in hx['h3']])
    lon0, lat1 = 79.9, 30.6
    nx, ny = int((88.4 - lon0) / RES) + 1, int((lat1 - 26.2) / RES) + 1
    c = ((ll[:, 1] - lon0) / RES).astype(int); r = ((lat1 - ll[:, 0]) / RES).astype(int)
    g = np.zeros((ny, nx)); np.add.at(g, (r, c), hx['pop'].values)
    tr = rasterio.transform.from_origin(lon0, lat1, RES, RES)
    lat = lat1 - (np.arange(ny) + 0.5) * RES
    area = (RES * 111.32 * np.cos(np.radians(lat))) * (RES * 110.57)
    return g, g / area[:, None], tr

def urban_centres(hx, dens_thr=1500, pop_min=50000):
    g, dens, tr = grid(hx)
    hd = dens >= dens_thr
    four = [[0, 1, 0], [1, 1, 1], [0, 1, 0]]
    lab, n = ndimage.label(hd, structure=four)
    keep = np.zeros_like(hd)
    sums = ndimage.sum(g, lab, index=np.arange(1, n + 1))
    for i, s in enumerate(sums, 1):
        if s >= pop_min: keep |= lab == i
    keep = ndimage.binary_fill_holes(keep)
    for _ in range(3):
        nb = ndimage.convolve(keep.astype(int), [[1, 1, 1], [1, 0, 1], [1, 1, 1]], mode='constant')
        keep = keep | (nb >= 5)
    lab, n = ndimage.label(keep, structure=four)
    polys, pops = [], []
    for i in range(1, n + 1):
        m = lab == i
        if g[m].sum() < pop_min: continue
        polys.append(unary_union([shape(s) for s, v in shapes(m.astype(np.uint8), mask=m, transform=tr) if v == 1])); pops.append(int(g[m].sum()))
    gdf = gpd.GeoDataFrame({'pop_grid': pops}, geometry=polys, crs=4326)
    return gdf[gdf.centroid.within(nepal_geom.buffer(0.01))].reset_index(drop=True)

uc1, uc0 = urban_centres(h21), urban_centres(h01)
print(f'urban centres {Y1}: {len(uc1)}  {Y0}: {len(uc0)}')

def pop_in(polys, hx):
    ll = np.array([h3.cell_to_latlng(h) for h in hx['h3']])
    pts = gpd.GeoDataFrame(hx[['pop']], geometry=gpd.points_from_xy(ll[:, 1], ll[:, 0]), crs=4326)
    j = gpd.sjoin(pts, polys[['geometry']].reset_index().rename(columns={'index': 'uc'}), predicate='within')
    return j.groupby('uc')['pop'].sum()

uc1[f'pop_{Y1}'] = pop_in(uc1, h21).reindex(uc1.index).fillna(0).astype(int)
uc1[f'pop_{Y0}'] = pop_in(uc1, h01).reindex(uc1.index).fillna(0).astype(int)
uc0['pop_own'] = pop_in(uc0, h01).reindex(uc0.index).fillna(0).astype(int)
uc0['area_own'] = (uc0.to_crs(32645).area / 1e6).round(1)
ov = gpd.overlay(uc1[['geometry']].reset_index().rename(columns={'index': 'uc'}), uc0[['geometry', 'pop_own', 'area_own']].reset_index().rename(columns={'index': 'uc0'}), how='intersection')
ov['a'] = ov.to_crs(32645).area
best = ov.sort_values('a').groupby('uc').tail(1).set_index('uc')
uc1[f'pop_{Y0}_ownfp'] = best['pop_own'].reindex(uc1.index).fillna(0).astype(int)
uc1[f'area_{Y0}'] = best['area_own'].reindex(uc1.index).fillna(0)
uc1['area_km2'] = (uc1.to_crs(32645).area / 1e6).round(1)
uc1['density'] = (uc1[f'pop_{Y1}'] / uc1['area_km2']).round(0)
c = uc1.to_crs(32645).centroid.to_crs(4326); uc1['lon'] = c.x.round(4); uc1['lat'] = c.y.round(4)

# municipalities overlapping, with their census populations
ovm = gpd.overlay(uc1[['geometry']].reset_index().rename(columns={'index': 'uc'}), units[['adm3_name', 'adm2_name', 'pop2021', 'geometry']], how='intersection')
ovm['a'] = ovm.to_crs(32645).area / 1e6
ovm = ovm.merge(uc1[['area_km2']], left_on='uc', right_index=True)
ovm = ovm[(ovm['a'] >= 2) | (ovm['a'] / ovm['area_km2'] >= 0.05)].sort_values('a', ascending=False)
uc1['municipalities'] = ovm.groupby('uc')['adm3_name'].apply(list).reindex(uc1.index).apply(lambda x: x if isinstance(x, list) else [])
uc1['mun_pop2021'] = ovm.groupby('uc')['pop2021'].apply(lambda s: [int(v) for v in s]).reindex(uc1.index).apply(lambda x: x if isinstance(x, list) else [])
uc1['district'] = ovm.groupby('uc')['adm2_name'].first().reindex(uc1.index)
NAME_FIX = {'Pokhara Lekhnath': 'Pokhara', 'Janakpurdham': 'Janakpur', 'Bhimdatta': 'Mahendranagar (Bhimdatta)', 'Rohini': 'Butwal – Bhairahawa', 'Tillotama': 'Butwal – Bhairahawa'}
def name_for(row):
    m = row['municipalities']
    if not m: return 'Unnamed'
    # prefer the overlapping municipality with the largest census population (the city proper), not the largest overlap area
    pairs = sorted(zip(row['mun_pop2021'], m), reverse=True)
    n = pairs[0][1]
    return NAME_FIX.get(n, n)
uc1['name'] = uc1.apply(name_for, axis=1)
uc1 = uc1.sort_values(f'pop_{Y1}', ascending=False).reset_index(drop=True); uc1['id'] = uc1.index
pd.set_option('display.width', 250); pd.set_option('display.max_colwidth', 90)
print(uc1[['id', 'name', f'pop_{Y1}', f'pop_{Y0}', f'pop_{Y0}_ownfp', 'area_km2', f'area_{Y0}', 'density', 'municipalities']].to_string())
uc1['geometry'] = uc1.geometry.simplify(0.0015)
uc1.to_file(f'{OUT}/urban_centres.geojson', driver='GeoJSON')
uc0o = uc0.copy(); uc0o['geometry'] = uc0o.geometry.simplify(0.0015); uc0o.to_file(f'{OUT}/urban_centres_{Y0}.geojson', driver='GeoJSON')

mun = units[['adm3_pcode', 'adm3_name', 'adm2_name', 'pop2001', 'pop2011', 'pop2021', 'geometry']].copy()
mun['geometry'] = mun.geometry.simplify(0.002); mun.to_file(f'{OUT}/municipalities.geojson', driver='GeoJSON')
o = adm0[['geometry']].copy(); o['geometry'] = o.geometry.simplify(0.003); o.to_file(f'{OUT}/nepal_outline.geojson', driver='GeoJSON')
json.dump({'pop_total': {Y0: int(h01['pop'].sum()), Y1: int(h21['pop'].sum())}, 'years': [Y0, Y1], 'hex_km2': HEX_KM2}, open(f'{OUT}/totals.json', 'w'))
print('done')
