"""Strict bounded canonicalization for private Phone context uploads."""
import datetime as dt
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from context_store import StoreError, identifier, instant

UNITS={'heart_rate':'count/min','resting_heart_rate':'count/min','hrv_sdnn':'ms','steps':'count','sleep':'min','workout':'min'}

def require(ok,code='invalid_context'):
    if not ok:raise StoreError(code)
def integer(v):return type(v) is int and 0<=v<=2**53-1
def number(v):return type(v) in (int,float) and math.isfinite(v)
def text(v,limit):return isinstance(v,str) and 0<len(v)<=limit and all(ord(c)>=32 for c in v)
def timestamp(v):return instant(v).isoformat(timespec='microseconds')
def interval(row):
    start=timestamp(row.get('start_at'));end=timestamp(row.get('end_at'))
    require(start<=end,'invalid_interval');return start,end

def validate_context(v):
    require(isinstance(v,dict) and set(v)=={'version','request_id','observed_at','timezone','function_flags','collection_state','health','health_deleted','places'})
    require(type(v['version']) is int and v['version']==3 and identifier(v['request_id']))
    require(text(v['timezone'],128),'invalid_timezone')
    try:ZoneInfo(v['timezone'])
    except (ZoneInfoNotFoundError,ValueError):raise StoreError('invalid_timezone')
    flags=v['function_flags'];require(isinstance(flags,dict) and set(flags)=={'health_enabled','location_enabled','share_derived_with_ai','control_revision'})
    require(all(type(flags[k]) is bool for k in ('health_enabled','location_enabled','share_derived_with_ai')) and integer(flags['control_revision']))
    state=v['collection_state'];require(isinstance(state,dict) and set(state)=={'health','location'})
    require(state['health'] in ('not_queried','available','no_data','unsupported','error') and state['location'] in ('not_queried','available','no_data','denied','restricted','unsupported','error'))
    require(isinstance(v['health'],list) and len(v['health'])<=512 and isinstance(v['health_deleted'],list) and len(v['health_deleted'])<=512 and isinstance(v['places'],list) and len(v['places'])<=128)
    health=[];seen=set()
    for row in v['health']:
        require(isinstance(row,dict) and set(row).issubset({'id','source','revision','kind','start_at','end_at','value','unit','category','activity','motion_context'}) and {'id','source','revision','kind','start_at','end_at','value','unit'}<=set(row))
        require(identifier(row['id']) and row['id'] not in seen);seen.add(row['id'])
        require(text(row['source'],256) and integer(row['revision']) and isinstance(row['kind'],str) and row['kind'] in UNITS and row['unit']==UNITS[row['kind']])
        require(number(row['value']) and row['value']>=0)
        start,end=interval(row)
        if row['kind'] in ('sleep','workout'):require(abs((instant(end)-instant(start)).total_seconds()/60-row['value'])<=.01,'duration_mismatch')
        if 'category' in row:require(row['kind']=='sleep' and type(row['category']) is int and 0<=row['category']<=64)
        if 'activity' in row:require(row['kind']=='workout' and text(row['activity'],128))
        if 'motion_context' in row:require(row['kind'] in ('heart_rate','resting_heart_rate') and row['motion_context'] in ('sedentary','active','unknown'))
        health.append(dict(row,start_at=start,end_at=end,deleted=False))
    require(all(identifier(x) for x in v['health_deleted']) and len(set(v['health_deleted']))==len(v['health_deleted']))
    places=[];seen=set()
    for row in v['places']:
        require(isinstance(row,dict) and set(row).issubset({'id','start_at','end_at','latitude','longitude','horizontal_accuracy_m','kind','alias'}) and {'id','start_at','end_at','latitude','longitude','horizontal_accuracy_m','kind'}<=set(row))
        require(identifier(row['id']) and row['id'] not in seen);seen.add(row['id']);start,end=interval(row)
        require(number(row['latitude']) and -90<=row['latitude']<=90 and number(row['longitude']) and -180<=row['longitude']<=180 and number(row['horizontal_accuracy_m']) and 0<=row['horizontal_accuracy_m']<=100000)
        require(row['kind'] in ('visit','coarse','sample'))
        if 'alias' in row:require(text(row['alias'],128))
        places.append(dict(row,start_at=start,end_at=end))
    return dict(v,observed_at=timestamp(v['observed_at']),function_flags=dict(flags),collection_state=dict(state),health=health,places=places)
