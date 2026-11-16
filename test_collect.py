import datetime as dt
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import collect

class Quotas(unittest.TestCase):
    def setUp(self):
        self.now=time.time()
        self.q={'used_percent':42,'resets_at':self.now+3600,'observed_at':dt.datetime.fromtimestamp(self.now,dt.timezone.utc).isoformat()}
    def test_invalid_expired_future_and_boolean(self):
        for change in ({'used_percent':True},{'used_percent':float('nan')},{'used_percent':-1},{'used_percent':101},{'resets_at':self.now-1},{'observed_at':'2020-01-01T00:00:00'},{'observed_at':dt.datetime.fromtimestamp(self.now+120,dt.timezone.utc).isoformat()}):
            with self.subTest(change=change):self.assertIsNone(collect.valid(dict(self.q,**change),self.now))
    def test_same_account_never_sums_sources(self):
        old=dict(self.q,used_percent=25,observed_at=dt.datetime.fromtimestamp(self.now-60,dt.timezone.utc).isoformat())
        self.assertEqual(collect.choose([old,self.q],self.now)['used_percent'],42)
    def test_normalize_strips_private_fields(self):
        data={'hosts':{'private-host':{'snapshot':{'windows':{'claude':self.q},'sessions':[{'prompt':'SECRET'}],'auth':'SECRET'}}}}
        out=collect.normalize(data)
        self.assertEqual(out['claude']['weekly']['used_percent'],42)
        self.assertNotIn('SECRET',json.dumps(out))
        self.assertNotIn('private-host',json.dumps(out))
    def test_codex_home_finds_only_numeric_weekly_events(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'sessions').mkdir()
            event={'timestamp':self.q['observed_at'],'type':'event_msg','payload':{'type':'token_count','rate_limits':{'secondary':dict(self.q,window_minutes=10080)},'prompt':'SECRET'}}
            (root/'sessions/session.jsonl').write_text('bad json\n'+json.dumps(event)+'\n')
            (root/'auth.json').write_text('not json, must never be read')
            out=collect.local_probe({'codex_home':folder,'quota_file':str(root/'missing')})
            self.assertEqual(out['codex']['weekly']['used_percent'],42)
            self.assertNotIn('SECRET',json.dumps(out))
    def test_outage_preserves_timestamp_but_expired_is_removed(self):
        cfg={'sources':[{'name':'one','type':'file','path':'missing'}]}
        prev={'hosts':{'one':{'snapshot':{'windows':{'codex':{'weekly':self.q}}}}}}
        out=collect.collect(cfg,prev)
        self.assertFalse(out['hosts']['one']['ok'])
        self.assertEqual(out['hosts']['one']['snapshot']['windows']['codex']['weekly']['observed_at'],self.q['observed_at'])
        prev['hosts']['one']['snapshot']['windows']['codex']['weekly']['resets_at']=self.now-1
        self.assertIsNone(collect.collect(cfg,prev)['hosts']['one']['snapshot']['windows']['codex']['weekly'])
    def test_ssh_rejects_option_and_shell_injection(self):
        for host in ('-oProxyCommand=bad','host;touch bad','user@host /bad'):
            with self.subTest(host=host),self.assertRaises(ValueError):collect.probe_source({'type':'ssh','host':host})
    def test_ssh_path_is_shell_quoted_and_host_checked(self):
        with patch('collect.subprocess.run') as run:
            run.return_value.stdout=json.dumps({'codex':{'weekly':self.q}})
            collect.probe_source({'type':'ssh','host':'my-host','quota_file':"~/a'; touch bad"})
            args=run.call_args.args[0]
            self.assertIn('StrictHostKeyChecking=yes',args)
            self.assertIn('BatchMode=yes',args)
            self.assertIn('--probe --options',args[-1])
    def test_malformed_source_does_not_block_healthy_source(self):
        bad_values=[[],None,{'windows':[]},{'windows':{'codex':{'allowances':[1]}}}]
        config={'sources':[{'name':'bad','type':'file','path':'bad'},{'name':'good','type':'file','path':'good'}]}
        for bad in bad_values:
            with self.subTest(bad=bad),patch('collect.read_json',side_effect=[bad,{'codex':{'weekly':self.q}}]):
                result=collect.collect(config)
                self.assertFalse(result['hosts']['bad']['ok'])
                self.assertTrue(result['hosts']['good']['ok'])
                self.assertEqual(result['hosts']['good']['snapshot']['windows']['codex']['weekly']['used_percent'],42)
    def test_duplicate_names_rejected(self):
        with self.assertRaises(ValueError):collect.collect({'sources':[{'name':'x'},{'name':'x'}]})
    def test_atomic_output_and_no_prompts(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'snapshot.json'
            collect.atomic_json(p,{'codex':self.q})
            self.assertEqual(collect.read_json(p)['codex']['used_percent'],42)


class ClaudeRefresh(unittest.TestCase):
    NOW = dt.datetime.fromisoformat('2026-10-16T00:35:00+00:00').timestamp()
    TEXT = '''You are currently using your subscription to power your Claude Code usage
Current session: 1% used � resets Oct 15, 5:59pm (America/Phoenix)
Current week (all models): 90% used � resets Oct 18, 6:59am (America/Phoenix)
Current week (Fable): 0% used � resets Oct 18, 7am (America/Phoenix)
'''

    def test_native_usage_parses_weekly_and_session_windows(self):
        value = collect.parse_claude_usage(self.TEXT,self.NOW)
        self.assertEqual(value['used_percent'],90)
        self.assertEqual(value['five_hour']['used_percent'],1)
        self.assertEqual(value['observation_kind'],'native_cli')
        self.assertEqual(collect.parse_claude_usage(self.TEXT.replace('�','\u00b7'),self.NOW)['used_percent'],90)

    def test_refresh_is_throttled_and_preserves_verified_reading(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);quota=root/'quotas.json';state=root/'state'
            with patch('collect.native_claude_usage',return_value=self.TEXT) as native:
                self.assertEqual(collect.refresh_claude(quota,state,'unused',now=lambda:self.NOW)['status'],'ok')
                saved=quota.read_bytes()
                self.assertEqual(collect.refresh_claude(quota,state,'unused',now=lambda:self.NOW+60)['status'],'throttled')
                self.assertEqual(native.call_count,1)
            self.assertEqual(quota.read_bytes(),saved)

    def test_native_command_must_prove_zero_cost_and_turns(self):
        with tempfile.TemporaryDirectory() as folder:
            def run(args,**kwargs):
                self.assertIn('--no-session-persistence',args)
                self.assertEqual(args[args.index('--tools')+1],'')
                sid=args[args.index('--session-id')+1]
                settings=json.loads(Path(args[args.index('--settings')+1]).read_text())
                self.assertEqual(settings['hooks']['UserPromptSubmit'][0]['hooks'][0]['onFailure'],'block')
                kwargs['stdout'].write(json.dumps({'type':'result','session_id':sid,
                    'total_cost_usd':0,'num_turns':0,'result':self.TEXT})+'\n')
                kwargs['stdout'].flush()
                return SimpleNamespace(returncode=0)
            with patch('collect.subprocess.run',side_effect=run):
                poll=Path(folder)/'poll';poll.mkdir()
                self.assertEqual(collect.native_claude_usage('unused',poll),self.TEXT)

    def test_local_probe_refreshes_claude_before_reading_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            quota=Path(folder)/'quotas.json'
            reading={'used_percent':42,'resets_at':time.time()+3600,
                     'observed_at':dt.datetime.now(dt.timezone.utc).isoformat()}
            def refresh(path,state,executable):
                collect.atomic_json(path,{'claude':{'weekly':reading}})
                return {'status':'ok'}
            with (patch('collect.find_claude',return_value=Path('claude')),
                  patch('collect.refresh_claude',side_effect=refresh) as call):
                result=collect.local_probe({'codex_home':folder,'quota_file':str(quota),'claude_cli':True})
            self.assertEqual(result['claude']['weekly']['used_percent'],42)
            call.assert_called_once()

if __name__=='__main__':unittest.main()
