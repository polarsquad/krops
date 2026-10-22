#!/usr/bin/env python3
"""Exercise shell run metadata through fake Kubernetes/Helm commands."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
KEYS = ['KROPS_RUN_ID', 'KROPS_REVISION', 'KROPS_EXPIRES_AT', 'KROPS_RUN_KIND']


class RunTags(unittest.TestCase):
    def run_shell(self, script, env=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            fake = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['LOG'], 'a') as f:
    f.write(json.dumps([os.path.basename(sys.argv[0]), *args]) + '\\n')
if 'get' in args:
    key = args[-1].split('.data.')[-1].rstrip('}')
    if os.environ.get('FAIL_READ') == key: sys.exit(1)
    print(json.loads(os.environ['VALUES']).get(key, ''), end='')
elif 'apply' in args:
    sys.stdin.read()
    if os.environ.get('FAIL_APPLY'): sys.exit(1)
elif 'create' in args: print('{}')
'''
            for cmd in ('kubectl', 'helm'):
                (path / cmd).write_text(fake)
                (path / cmd).chmod(0o755)
            (path / 'date').write_text("""#!/usr/bin/env python3
from datetime import datetime, timezone
import os, sys
args = sys.argv[1:]
if args == ['-u', '+%s']:
    print(os.environ.get('FAKE_NOW', '1700000000'))
else:
    seconds = int(args[2].lstrip('@')) if args[1] in ('-d', '-r') else 1700000000
    print(datetime.fromtimestamp(seconds, timezone.utc).strftime(args[-1].lstrip('+')))
""")
            (path / 'date').chmod(0o755)
            values = dict(zip(KEYS, ['run exact', 'sha', 'never', 'ci']))
            proc_env = {k: v for k, v in os.environ.items() if not k.startswith('KROPS_')}
            proc_env.update(PATH=f'{tmp}:{os.environ["PATH"]}', LOG=f'{tmp}/log', VALUES=json.dumps(values))
            proc_env.update(env or {})
            result = subprocess.run(['bash', '-c', f'set -euo pipefail; source "{ROOT}/bootstrap-common.sh"; PROFILE=local-host; trap seed_cleanup EXIT; {script}'], env=proc_env, capture_output=True, text=True, check=False)
            events = [json.loads(line) for line in (path / 'log').read_text().splitlines()] if (path / 'log').exists() else []
            return result, events

    def test_ttl(self):
        for ttl, seconds in [('3600', 3600), ('90s', 90), ('30m', 1800), ('2h', 7200),
                             ('  0090s  ', 90), ('0', 0), ('', 86400), ('   ', 86400),
                             (None, 86400), ('none', None)]:
            with self.subTest(ttl=ttl):
                env = {} if ttl is None else {'KROPS_RUN_TTL': ttl}
                result, _ = self.run_shell('resolve_run_tags; echo "$_KROPS_EXPIRES_AT"', env)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = 'never' if seconds is None else datetime.fromtimestamp(1700000000 + seconds, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                self.assertEqual(result.stdout.strip(), expected)
        for ttl in ['-1', '1.5h', '1h30m', '+1', 'h', '999999999999999999999h', 'x[$(touch /tmp/bad)]']:
            with self.subTest(ttl=ttl):
                result, events = self.run_shell('resolve_run_tags; prepare_flux; apply_run_tags; activate_flux', {'KROPS_RUN_TTL': ttl})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(events, [])

    def test_bootstrap_sequence(self):
        source = (ROOT / 'bootstrap.sh').read_text()
        start = source.index('resolve_run_tags\n')
        sequence = source[start:source.index('# ── Step 5:', start)]
        result, events = self.run_shell(sequence)
        self.assertEqual(result.returncode, 0, result.stderr)
        operator = next(i for i, e in enumerate(events) if 'flux-operator' in e)
        metadata = next(i for i, e in enumerate(events) if 'krops-run' in e)
        activation = next(i for i, e in enumerate(events) if 'flux' in e and e[0] == 'helm')
        self.assertLess(operator, metadata)
        self.assertLess(metadata, activation)
        result, events = self.run_shell(sequence, {'FAIL_APPLY': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any('flux' in e and e[0] == 'helm' for e in events))

    def test_pivot_copy_and_failures(self):
        source = (ROOT / 'pivot.sh').read_text()
        start = source.index('  read_source_run_tags\n')
        sequence = source[start:source.index('  echo ">>> Kustomizations', start)]
        sequence = 'MGMT_KUBECONFIG=target; ' + sequence
        result, events = self.run_shell(sequence)
        self.assertEqual(result.returncode, 0, result.stderr)
        reads = [e for e in events if 'get' in e]
        self.assertEqual(len(reads), 4)
        self.assertTrue(all(e[1:3] == ['--context', 'kind-mgmt'] for e in reads))
        create = next(e for e in events if 'krops-run' in e and 'create' in e)
        self.assertIn('--from-literal=KROPS_RUN_ID=run exact', create)
        self.assertEqual(create[1:3], ['--kubeconfig', 'target'])
        for key in KEYS:
            for failure in ({'FAIL_READ': key}, {'VALUES': json.dumps({k: 'x' for k in KEYS if k != key})}):
                result, events = self.run_shell(sequence, failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(e[0] == 'helm' for e in events))

    def test_ttl_clock_overflow(self):
        result, _ = self.run_shell('resolve_run_tags', {'FAKE_NOW': '253402300800', 'KROPS_RUN_TTL': '0h'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('clock overflow', result.stderr)

    def test_activate_requires_prepare(self):
        # SEED_REGISTRY_CONFIG not set at all
        result, _ = self.run_shell('unset SEED_REGISTRY_CONFIG; activate_flux')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('activate_flux called before prepare_flux', result.stderr)
        # SEED_REGISTRY_CONFIG set but empty
        result, _ = self.run_shell('SEED_REGISTRY_CONFIG=""; activate_flux')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('activate_flux called before prepare_flux', result.stderr)


if __name__ == '__main__':
    unittest.main()
