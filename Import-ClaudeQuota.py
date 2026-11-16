"""Import a reading you obtained from Claude's usage display. No model calls."""
import argparse
import datetime as dt
from pathlib import Path
from collect import atomic_json, read_json, valid

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--used',type=float,required=True,help='Weekly percent used, not percent remaining')
p.add_argument('--reset',required=True,help='ISO reset timestamp including timezone, e.g. 2027-01-07T18:00:00-07:00')
p.add_argument('--observed-at',required=True,help='ISO timestamp when you read the percentage, with timezone')
p.add_argument('--output',type=Path,default=Path.home()/'.config/ai-usage-widget/quotas.json')
a=p.parse_args()
try:
    reset=dt.datetime.fromisoformat(a.reset.replace('Z','+00:00'))
    if reset.tzinfo is None:
        raise ValueError('Reset must include timezone')
    value=valid({'used_percent':a.used,'resets_at':reset.timestamp(),'observed_at':a.observed_at})
    if value is None:
        raise ValueError('Reading must be valid, unexpired and inside its weekly window')
except ValueError as e:
    p.error(str(e))
data=read_json(a.output) if a.output.exists() else {}
data['claude']={'weekly':value}
atomic_json(a.output,data)
print('Saved weekly Claude reading; it will become stale until updated.')
