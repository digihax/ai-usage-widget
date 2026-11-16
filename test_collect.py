import datetime as dt
import json
from pathlib import Path
import tempfile
import time
import unittest
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

if __name__=='__main__':unittest.main()
