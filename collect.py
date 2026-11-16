"""Read-only quota collector. Standard library only; never reads auth files."""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    """Accept this project's minimal schema or a compatible multi-host snapshot."""
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
    quota_path = Path(options.get('quota_file', '~/.config/ai-usage-widget/quotas.json')).expanduser()
    claude_cli = options.get('claude_cli', False)
    if type(claude_cli) is not bool:
        raise ValueError('claude_cli must be true or false')
    if claude_cli:
        executable = find_claude(options.get('claude_executable'))
        if executable:
            state_path = Path(options.get('claude_state', quota_path.parent/'.claude-quota-state')).expanduser()
            refresh_claude(quota_path, state_path, executable)
    result = {'claude': {'weekly': None}, 'codex': {'weekly': choose(candidates)}}
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
    options = {k:source[k] for k in ('codex_home','quota_file','claude_cli','claude_executable','claude_state') if k in source}
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


def find_claude(configured=None):
    if configured is not None:
        if not isinstance(configured, str) or not configured.strip():
            raise ValueError('claude_executable must be a path string')
        path = Path(configured).expanduser()
        return path if path.is_file() else None
    candidates = []
    if os.name == 'nt':
        appdata = Path(os.environ.get('APPDATA', Path.home()/'AppData/Roaming'))
        candidates.extend((appdata/'npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe',
                           Path.home()/'.local/bin/claude.exe'))
    found = shutil.which('claude')
    if found:
        candidates.append(Path(found))
    return next((path for path in candidates if path.is_file()), None)


def claude_reset_time(value, zone, now, minutes):
    try:
        timezone = ZoneInfo(zone)
    except ZoneInfoNotFoundError:
        # Python on Windows may not ship the IANA database. Preserve the original
        # widget's Phoenix behavior; other zones fail closed until tzdata exists.
        if zone != 'America/Phoenix':
            raise ValueError('Install the Python tzdata package for this timezone')
        timezone = dt.timezone(dt.timedelta(hours=-7))
    local = dt.datetime.fromtimestamp(now, timezone)
    values = []
    for year in (local.year, local.year+1):
        fmt = '%b %d, ' + ('%I:%M%p' if ':' in value else '%I%p') + ' %Y'
        try:
            parsed = dt.datetime.strptime(value+' '+str(year), fmt)
        except ValueError:
            continue
        values.append(parsed.replace(tzinfo=timezone).timestamp())
    resets = [reset for reset in values if 0 < reset-now <= minutes*60+60]
    if len(resets) != 1:
        raise ValueError('Claude reset is expired, ambiguous or outside its window')
    return int(resets[0])


def parse_claude_usage(text, now, previous=None):
    if not isinstance(text, str) or len(text) > 32768:
        raise ValueError('Bounded Claude /usage text required')
    observed = dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()
    result = {'source':'Claude CLI /usage (zero inference; displayed reset rounded to minutes)',
              'observed_at':observed,'observation_kind':'native_cli'}
    for label,key,minutes in (('Current week (all models)','weekly',10080),
                              ('Current session','five_hour',300)):
        sessions = [line for line in text.splitlines() if line.startswith('Current session:')]
        if key == 'five_hour' and sessions == ['Current session: 0% used']:
            result[key] = {'state':'not_started','used_percent':0,'observed_at':observed,
                           'source':result['source']}
            continue
        if key == 'five_hour' and not sessions:
            result[key] = {'state':'not_displayed','observed_at':observed,'source':result['source']}
            continue
        pattern = (re.escape(label) + r': (\d+(?:\.\d+)?)% used [\u00b7\u2022\ufffd] resets '
                   r'([A-Z][a-z]{2} \d{1,2}, \d{1,2}(?::\d{2})?[ap]m) \(([^)]+)\)')
        matches = re.findall(pattern,text)
        if len(matches) != 1:
            raise ValueError('Claude /usage display schema changed: '+key)
        percent = float(matches[0][0])
        if not math.isfinite(percent) or not 0 <= percent <= 100:
            raise ValueError('Invalid Claude percentage')
        reset = claude_reset_time(matches[0][1],matches[0][2],now,minutes)
        old = (previous or {}) if key == 'weekly' else (previous or {}).get('five_hour',{})
        anchor = old.get('resets_at')
        if isinstance(anchor,(int,float)) and now < anchor <= now+minutes*60 and abs(anchor-reset) <= 60:
            reset = anchor
        value = {'resets_at':reset,'used_percent':percent,'observed_at':observed,
                 'source':result['source'],'reset_precision_seconds':60}
        if key == 'weekly':
            result.update(value)
        else:
            result[key] = value
    return result


def native_claude_usage(executable, folder):
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    sid = str(uuid.uuid4())
    guard = folder/'guard.py'
    guard.write_text(
        "import json,sys,uuid\n"
        "sid=str(uuid.UUID(sys.argv[1]))\n"
        "value=json.loads(sys.stdin.buffer.read(262145))\n"
        "if value.get('session_id') != sid: raise SystemExit(2)\n"
        "print(json.dumps({'continue':False,'stopReason':'Read-only /usage probe; no inference or tools authorized'}))\n",
        encoding='utf8')
    parts = [sys.executable,str(guard),sid]
    command = subprocess.list2cmdline(parts) if os.name == 'nt' else shlex.join(parts)
    settings = folder/'settings.json'
    atomic_json(settings,{'hooks':{event:[{'hooks':[{'type':'command','command':command,
        'timeout':10,'onFailure':'block'}]}] for event in ('UserPromptSubmit','PreToolUse')}})
    output,error = folder/'output.jsonl',folder/'stderr.log'
    args = [str(executable),'-p','--session-id',sid,'--no-session-persistence',
            '--output-format','stream-json','--verbose','--strict-mcp-config',
            '--mcp-config','{"mcpServers":{}}','--tools','','--max-turns','1',
            '--max-budget-usd','0.01','--settings',str(settings),'/usage']
    with output.open('x',encoding='utf8') as stdout,error.open('x',encoding='utf8') as stderr:
        completed = subprocess.run(args,cwd=folder,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,
            timeout=45,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if output.stat().st_size > 262144:
        raise ValueError('Claude quota output exceeds review bound')
    results = []
    for line in output.read_text(encoding='utf8').splitlines():
        row = json.loads(line)
        if row.get('session_id') not in (None,sid):
            raise ValueError('Claude quota identity mismatch')
        if row.get('type') == 'result':
            results.append(row)
    if (completed.returncode or len(results) != 1 or results[0].get('is_error')
            or type(results[0].get('total_cost_usd')) not in (int,float)
            or results[0]['total_cost_usd'] != 0
            or type(results[0].get('num_turns')) is not int or results[0]['num_turns'] != 0):
        raise ValueError('Claude quota collection did not prove zero inference/cost')
    return results[0].get('result')


def _refresh_claude(quota_path,state_root,executable,now=time.time):
    current = now()
    state_path = state_root/'state.json'
    try:
        state = read_json(state_path) if state_path.exists() else {}
    except (OSError,ValueError):
        state = {}
    if not isinstance(state,dict):
        state = {}
    if 0 <= current-state.get('attempted_at',0) < 900:
        return {'status':'throttled'}
    state = {'attempted_at':current,'status':'pending'}
    atomic_json(state_path,state)
    try:
        with tempfile.TemporaryDirectory(prefix='poll-',dir=state_root) as temporary:
            text = native_claude_usage(executable,Path(temporary))
        old = read_json(quota_path) if quota_path.exists() else {}
        if not isinstance(old,dict):
            old = {}
        observation = parse_claude_usage(text,now(),old.get('claude'))
        old['claude'] = observation
        atomic_json(quota_path,old)
        state.update(status='ok',observed_at=observation['observed_at'])
    except Exception as error:
        state.update(status='failed',error_type=type(error).__name__,error_reason=str(error)[:200])
    atomic_json(state_path,state)
    return {key:state[key] for key in ('status','observed_at','error_type') if key in state}


def refresh_claude(quota_path,state_root,executable,now=time.time):
    state_root = Path(state_root)
    state_root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (state_root/'poll.lock').open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            if lock.seek(0,2) == 0:
                lock.write(b'0');lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:
                return {'status':'busy'}
        else:
            import fcntl
            try:
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:
                return {'status':'busy'}
        try:
            return _refresh_claude(Path(quota_path),state_root,executable,now)
        finally:
            if os.name == 'nt':
                lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(lock.fileno(),fcntl.LOCK_UN)


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
