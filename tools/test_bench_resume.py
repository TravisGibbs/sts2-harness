"""Offline regressions for resuming benchmark experiments without destroying evidence."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
from bench import archive_recording, suite_recorded_cost, validate_resume_config
import bench

class ResumeTests(unittest.TestCase):
    def config(self):
        return dict(character='ironclad', ascension=0, seeds=['BENCH01'], attempts=1,
                    limits={'max_requests':600,'max_cost':.75,'max_floor':None},
                    prompt_fingerprint='abc', models=[{'model':'local:test','effort':'default'}])

    def test_legacy_tools_config_can_resume(self):
        old=self.config(); new=self.config()
        new['models'][0].update(protocol='tools',prompt_fingerprint='abc')
        new['parallel']=6
        validate_resume_config(old,new)

    def test_changed_experiment_rejected(self):
        for key,value in [('character','silent'),('ascension',1),('seeds',['BENCH02']),
                          ('limits',{}),('prompt_fingerprint','changed'),('attempts',2)]:
            with self.subTest(key=key):
                old=self.config(); new=self.config(); new[key]=value
                with self.assertRaisesRegex(ValueError,'new --suite'):
                    validate_resume_config(old,new)

    def test_harness_version_change_rejected(self):
        old=self.config(); new=self.config()
        old['harness_version']='0.2'; new['harness_version']='0.3'
        with self.assertRaisesRegex(ValueError,'harness_version'):
            validate_resume_config(old,new)
        # Suites recorded before the version was stored can still resume.
        legacy=self.config(); validate_resume_config(legacy,new)

    def test_protocol_and_per_model_prompt_rejected(self):
        for extra in ({'protocol':'text'},{'prompt_fingerprint':'other'}):
            old=self.config(); new=self.config(); new['models'][0].update(extra)
            with self.assertRaises(ValueError): validate_resume_config(old,new)

    def test_archive_preserves_bytes_and_total_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); runs=root/'runs'; runs.mkdir()
            path=runs/'model__seed.jsonl'
            original='\n'.join(json.dumps(x) for x in [
                {'type':'step','cost':.125},{'type':'step','cost':.2},
                {'type':'run_end','cost':.325,'outcome':'api_error'}])+'\n'
            path.write_text(original,encoding='utf-8')
            self.assertAlmostEqual(suite_recorded_cost(root),.325)
            archived=archive_recording(path)
            self.assertFalse(path.exists())
            self.assertEqual(archived.read_text(encoding='utf-8'),original)
            self.assertIsNone(archive_recording(path))
            path.write_text(json.dumps({'type':'step','cost':.05})+'\n',encoding='utf-8')
            self.assertAlmostEqual(suite_recorded_cost(root),.375)
            second=archive_recording(path)
            self.assertNotEqual(archived,second)
            self.assertTrue(archived.exists())
            self.assertAlmostEqual(suite_recorded_cost(root),.375)

    def test_partial_line_does_not_hide_prior_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'runs').mkdir()
            (root/'runs'/'partial.jsonl').write_text('{"type":"step","cost":0.4}\n{"type":',encoding='utf-8')
            self.assertAlmostEqual(suite_recorded_cost(root),.4)

    def test_documented_adjustments_count_without_fake_recordings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            adjustments=root/'cost_adjustments.json'
            adjustments.write_text(json.dumps([{'amount_usd':.482,'source':'original log'}]),encoding='utf-8')
            self.assertAlmostEqual(suite_recorded_cost(root),.482)
            for invalid in [-1,float('nan'),float('inf')]:
                adjustments.write_text(json.dumps([{'amount_usd':invalid}]),encoding='utf-8')
                with self.assertRaises(ValueError): suite_recorded_cost(root)

    def test_unexpected_error_counts_paid_requests_before_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'runs' / 'partial.jsonl'
            path.parent.mkdir()
            args = SimpleNamespace(attempts=1, suite='test', character='ironclad', ascension=0,
                                   max_cost=1, max_requests=10, max_floor=None, verbose=False, budget=.1)
            suite = bench.Suite(args, 1, 0)
            job = bench.Job(1, 1, 'A', 'local:test', 'default', 'tools', 'local:test', path, ())
            def crash(*args, **kwargs):
                path.write_text(json.dumps({'type': 'step', 'cost': .25}) + '\n', encoding='utf-8')
                raise ValueError('unexpected provider payload')
            with patch.object(bench, 'ensure_game', return_value=False), \
                 patch.object(bench, 'run_once', side_effect=crash), patch.object(bench, 'say'):
                with self.assertRaisesRegex(ValueError, 'unexpected provider payload'):
                    suite.play(job, Mock(), None)
                self.assertAlmostEqual(suite.spent, .25)
                self.assertFalse(suite.budget_left())
            self.assertTrue(path.exists())  # The diagnostic recording survives too.

if __name__=='__main__': unittest.main()
