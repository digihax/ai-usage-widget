"""Read-only quota collector. Standard library only; never reads auth files."""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

MAX_JSON = 8 * 1024 * 1024


def stamp(value):
    if not isinstance(value, str):
        raise ValueError('Missing observation timestamp')
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return parsed.timestamp()


def valid(value, now=None):
    now = time.time() if now is None else now
    try:
        used, reset = value['used_percent'], value['resets_at']
        observed = stamp(value['observed_at'])
        if type(used) not in (int, float) or type(reset) not in (int, float):
            return None
        if not math.isfinite(used) or not math.isfinite(reset):
            return None
        if not (0 <= used <= 100 and now < reset and reset-604800 <= observed < reset and observed <= now+30):
            return None
        return {'used_percent': used, 'resets_at': reset, 'observed_at': value['observed_at'], 'window_minutes':10080}
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def choose(items, now=None):
    values = [v for item in items if (v := valid(item, now))]
    return max(values, key=lambda v: stamp(v['observed_at']), default=None)


def read_json(path):
    path = Path(path).expanduser()
    if path.stat().st_size > MAX_JSON:
        raise ValueError('Snapshot exceeds size limit')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def normalize(data):
    """Accept this project's minimal schema or an existing SessionKeeper snapshot."""
    if not isinstance(data, dict):
        raise ValueError('Snapshot must be an object')
    candidates = {'claude': [], 'codex': []}
    snapshots = [data]
    if isinstance(data.get('hosts'), dict):
        snapshots = [h.get('snapshot', {}) for h in data['hosts'].values() if isinstance(h, dict)]
    for snap in snapshots:
        if not isinstance(snap, dict):
            continue
        windows = snap.get('windows', snap)
        if not isinstance(windows, dict):
            raise ValueError('Windows must be an object')
        for tool in candidates:
            win = windows.get(tool) or {}
            if not isinstance(win, dict):
                continue
            candidates[tool].append(win)
            if isinstance(win.get('weekly'), dict):
                candidates[tool].append(win['weekly'])
            allowances = win.get('allowances') or {}
            if not isinstance(allowances, dict):
                raise ValueError('Allowances must be an object')
            allowance = allowances.get('weekly')
            if isinstance(allowance, dict) and allowance.get('window_minutes') == 10080:
                candidates[tool].append(allowance)
    return {tool: {'weekly': choose(items)} for tool, items in candidates.items()}


def local_probe(options):
    candidates = []
    home = Path(options.get('codex_home', os.environ.get('CODEX_HOME', '~/.codex'))).expanduser()
    # Read only numeric token_count events; no prompts, paths or auth values leave this function.
    paths = list((home/'sessions').rglob('*.jsonl')) if (home/'sessions').exists() else []
    paths.sort(key=lambda p:p.stat().st_mtime, reverse=True)
    for path in paths[:100]:
        try:
            with path.open('rb') as f:
                # Quotas are recent; bound each transcript read to its last 2 MiB.
                size = path.stat().st_size
                f.seek(max(0, size-2*1024*1024))
                if size > 2*1024*1024:
                    f.readline()
                for raw in f:
                    if len(raw) > 262144:
                        continue
                    try:
                        row = json.loads(raw)
                        payload = row.get('payload') or {}
                        if row.get('type') != 'event_msg' or payload.get('type') != 'token_count':
                            continue
                        limits = payload.get('rate_limits') or {}
                        for key in ('primary','secondary'):
                            quota = limits.get(key) or {}
                            if quota.get('window_minutes') == 10080:
                                candidates.append(dict(quota,observed_at=row.get('timestamp')))
                    except (ValueError, TypeError, AttributeError):
                        continue
        except OSError:
            continue
    result = {'claude': {'weekly': None}, 'codex': {'weekly': choose(candidates)}}
    quota_path = Path(options.get('quota_file', '~/.config/ai-usage-widget/quotas.json')).expanduser()
    if quota_path.exists():
        cached = normalize(read_json(quota_path))
        for tool in result:
            result[tool]['weekly'] = choose([result[tool]['weekly'],cached[tool]['weekly']])
    return result


def probe_source(source):
    kind = source.get('type','local')
    if kind == 'file':
        return normalize(read_json(source['path']))
    if kind == 'local':
        return local_probe(source)
    if kind != 'ssh':
        raise ValueError('Unknown source type')
    host = source['host']
    if not isinstance(host,str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*',host):
        raise ValueError('Use a trusted SSH config alias or user@hostname')
    options = {k:source[k] for k in ('codex_home','quota_file') if k in source}
    script = Path(__file__).read_text(encoding='utf8')
    # POSIX remote shell; no config values are interpolated into executable shell text.
    remote = 'python3 - --probe --options ' + shlex.quote(json.dumps(options))
    output = subprocess.run(['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
                             '-o','ConnectTimeout=10',host,remote],input=script,text=True,
                            capture_output=True,timeout=45,check=True,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if len(output.stdout)>MAX_JSON:
        raise ValueError('Remote snapshot exceeds size limit')
    return normalize(json.loads(output.stdout))


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:
            json.dump(data,f,indent=2,allow_nan=False)
            f.write('\n')
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def collect(config, previous=None):
    sources = config.get('sources')
    if not isinstance(sources,list) or not 1<=len(sources)<=16:
        raise ValueError('Configure between one and sixteen sources')
    names = [s.get('name') for s in sources]
    if any(not isinstance(n,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',n) for n in names) or len(set(names))!=len(names):
        raise ValueError('Sources need unique simple names')
    hosts = {}
    for source in sources:
        name = source['name']
        try:
            hosts[name] = {'ok':True,'snapshot':{'windows':probe_source(source)}}
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            old = ((previous or {}).get('hosts') or {}).get(name,{})
            # Revalidate retained numeric readings; preserve timestamps so stale data stays visible as stale.
            hosts[name] = {'ok':False,'error':'Source unavailable; check configuration and SSH access',
                           'snapshot':{'windows':normalize(old.get('snapshot',{}))}}
    return {'generated_at':dt.datetime.now(dt.timezone.utc).isoformat(),'hosts':hosts}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path(__file__).with_name('config.json'))
    parser.add_argument('--output',type=Path,default=Path(__file__).with_name('usage-data.json'))
    parser.add_argument('--watch',action='store_true')
    parser.add_argument('--probe',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--options',default='{}',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.probe:
        print(json.dumps(local_probe(json.loads(args.options)),allow_nan=False))
        return
    config=read_json(args.config)
    interval=config.get('refresh_seconds',60)
    if type(interval) is not int or not 30<=interval<=3600:
        parser.error('refresh_seconds must be an integer from 30 to 3600')
    while True:
        try:
            previous=read_json(args.output) if args.output.exists() else None
        except (OSError,ValueError):
            previous=None
        result=collect(config,previous)
        atomic_json(args.output,result)
        print('Collected %d/%d sources' % (sum(h['ok'] for h in result['hosts'].values()),len(result['hosts'])),flush=True)
        if not args.watch:
            return
        time.sleep(interval)


if __name__=='__main__':
    main()
