"""Read-only historical odds comparison; never emits consensus predictions.

Features: margin-normalized home/draw/away and exact FT over 2.5 probability.
Fixed neighborhood: all coordinates within 5pp, closest 300, minimum 100.
Rates and Wilson intervals describe historical neighbors, not calibrated forecasts.
"""
import csv
import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np

ARCHIVE = Path(__file__).parent / 'data/iddaa_arsiv_YEDEK.csv'
MIN_N = 100
MAX_N = 300
RADIUS = .05
MAX_AGE = 900


def features(odds):
    try:
        values = [float(o) for o in odds]
        if len(values) != 5 or any(not math.isfinite(o) or o <= 1 for o in values):
            return None
        p = [1/o for o in values]
        return np.array([v/sum(p[:3]) for v in p[:3]] + [p[3]/sum(p[3:])])
    except (TypeError, ValueError, OverflowError):
        return None


@lru_cache(maxsize=2)
def load_archive(path, modified):
    rows, seen, rejected = [], set(), 0
    with open(path, encoding='utf-8-sig', newline='') as handle:
        for r in csv.DictReader(handle):
            try:
                date = datetime.strptime(r['Date'], '%d-%m-%Y').replace(tzinfo=timezone.utc)
                key = (date, r['Mac'].strip().casefold())
                x = features([r[k] for k in ('Open_H','Open_D','Open_A','Open_O25','Open_U25')])
                h, a = float(r['FTHG']), float(r['FTAG'])
                if x is None or any(not math.isfinite(v) or v < 0 or not v.is_integer() for v in (h,a)) or key in seen:
                    rejected += 1
                    continue
                seen.add(key)
                rows.append(dict(date=date.strftime('%Y-%m-%d'), timestamp=date.timestamp(),
                                 match=r['Mac'], score=f'{int(h)}–{int(a)}',
                                 odds=[float(r[k]) for k in ('Open_H','Open_D','Open_A','Open_O25','Open_U25')],
                                 x=x, y=[int(h>0 and a>0), int(h+a>2)]))
            except (ValueError, KeyError, TypeError):
                rejected += 1
    rows.sort(key=lambda r: (r['date'], r['match']))
    return rows, np.array([r['x'] for r in rows]).reshape(-1,4), np.array([r['y'] for r in rows]).reshape(-1,2), rejected


def interval(wins, n):
    p, z = wins/n, 1.96
    den = 1+z*z/n
    center = (p+z*z/(2*n))/den
    half = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [round(100*(center-half),1), round(100*(center+half),1)]


def compare(x, rows, X, Y):
    distances = np.max(np.abs(X-x), axis=1) if len(X) else np.array([])
    idx = np.flatnonzero(distances <= RADIUS)
    idx = idx[np.argsort(distances[idx], kind='stable')[:MAX_N]]
    result = dict(samples=len(idx), sufficient=len(idx)>=MIN_N, kg=None, over25=None, examples=[])
    if len(idx) < MIN_N:
        return result
    for col, name in enumerate(('kg','over25')):
        wins = int(Y[idx,col].sum())
        result[name] = dict(rate=round(100*wins/len(idx),1), wins=wins, interval=interval(wins,len(idx)))
    result['max_distance_pp'] = round(float(distances[idx].max())*100,2)
    result['examples'] = [{k:rows[i][k] for k in ('date','match','score','odds')} for i in idx[:8]]
    return result


def snapshot_path(db_path):
    return Path(db_path).parent / 'iddaa_current_snapshot.json'


def save_snapshot(db_path, events, fetched_at):
    """Atomic independent file, no lock or writes to the live SQLite database."""
    now = time.time()
    if not isinstance(fetched_at, (int,float)) or not math.isfinite(fetched_at) or not 0 <= now-fetched_at <= MAX_AGE:
        raise ValueError('Invalid snapshot timestamp')
    path = snapshot_path(db_path)
    body = json.dumps(dict(fetched_at=fetched_at,events=events), ensure_ascii=False, allow_nan=False)
    name = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False, encoding='utf-8') as f:
            name = f.name
            f.write(body)
        os.replace(name,path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def dashboard(db_path, now=None):
    now = time.time() if now is None else now
    rows,X,Y,rejected = load_archive(str(ARCHIVE), ARCHIVE.stat().st_mtime_ns)
    # Exclude any archive record on/after the current UTC date (no future leakage).
    cutoff = datetime.fromtimestamp(now,timezone.utc).strftime('%Y-%m-%d')
    n = sum(r['date'] < cutoff for r in rows)
    rows,X,Y = rows[:n],X[:n],Y[:n]
    result = dict(archive_count=n,rejected=rejected,archive_start=rows[0]['date'] if rows else None,
                  archive_end=rows[-1]['date'] if rows else None, fresh=False,fetched_at=None,matches=[],
                  missing_odds=0,invalid_kickoff=0,min_samples=MIN_N)
    try:
        snapshot = json.loads(snapshot_path(db_path).read_text())
    except FileNotFoundError:
        return result
    fetched = float(snapshot['fetched_at'])
    result['fetched_at'] = fetched
    result['fresh'] = math.isfinite(fetched) and 0<=now-fetched<=MAX_AGE
    if not result['fresh']:
        return result
    seen = set()
    for e in snapshot['events']:
        try:
            kickoff = float(e.get('kickoff'))
            if not math.isfinite(kickoff):
                raise ValueError()
        except (TypeError,ValueError):
            result['invalid_kickoff'] += 1
            continue
        if kickoff <= now or str(e.get('event_id')) in seen:
            continue
        seen.add(str(e.get('event_id')))
        odds = [e.get(k) for k in ('odd_1','odd_x','odd_2','over25_odd','under25_odd')]
        x = features(odds)
        if x is None:
            result['missing_odds'] += 1
            continue
        item = dict(event_id=e['event_id'],home=e.get('home',''),away=e.get('away',''),kickoff=kickoff,
                    odds=odds,market_over25=round(float(x[3])*100,1),**compare(x,rows,X,Y))
        result['matches'].append(item)
    result['matches'].sort(key=lambda r:r['kickoff'])
    return result


def audit():
    rows,X,Y,rejected=load_archive(str(ARCHIVE),ARCHIVE.stat().st_mtime_ns)
    split=rows[len(rows)//2]['date']
    n=sum(r['date']<split for r in rows)
    predicted, actual, market = [],[],[]
    for i in range(n,len(rows)):
        r=compare(X[i],rows[:n],X[:n],Y[:n])
        if r['sufficient']:
            predicted.append([r['kg']['rate']/100,r['over25']['rate']/100]); actual.append(Y[i]); market.append(X[i,3])
    p,y=np.array(predicted),np.array(actual)
    report=dict(archive_count=len(rows),rejected=rejected,split_date=split,train=n,test=len(rows)-n,covered=len(p),
                mode='historical_comparison_only',radius_pp=5,min_samples=MIN_N,max_samples=MAX_N,metrics={})
    for j,key in enumerate(('kg','over25')):
        report['metrics'][key]=dict(brier=float(np.mean((p[:,j]-y[:,j])**2)),
                                    baseline_brier=float(np.mean((Y[:n,j].mean()-y[:,j])**2)),
                                    correlation=float(np.corrcoef(p[:,j],y[:,j])[0,1]))
    report['metrics']['over25']['market_brier']=float(np.mean((np.array(market)-y[:,1])**2))
    return report


if __name__ == '__main__':
    print(json.dumps(audit(),ensure_ascii=False,indent=2))
