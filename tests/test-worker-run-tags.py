#!/usr/bin/env python3
"""Run the real worker reconciler against a stateful fake AWS CLI."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / 'mgmt/aws/infrastructure/worker-run-tags'
CLUSTERS = ['default_eu-north-1-management-control-plane', 'default_eu-north-1-workload-control-plane', 'default_eu-west-1-workload-control-plane']
KEYS = ['krops.io/run-id', 'krops.io/revision', 'krops.io/expires-at', 'krops.io/run-kind']
FAKE = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
p = Path(os.environ['STATE'])
s = json.loads(p.read_text())
a = sys.argv[1:]
s['calls'].append(a)
def arg(flag): return a[a.index(flag) + 1]
service, operation = a[:2]
cluster = arg('--cluster-name') if '--cluster-name' in a else arg('--name') if '--name' in a else None
if operation == 'get-caller-identity': output = s.get('account', '120392301094')
elif cluster is not None and s.get('fail_cluster') == cluster:
    print('AccessDenied', file=sys.stderr); p.write_text(json.dumps(s)); sys.exit(1)
elif operation == 'describe-cluster':
    output = json.dumps(s['clusters'][cluster].get('response', {'cluster': {'tags': dict(zip(['krops.io/run-id', 'krops.io/revision', 'krops.io/expires-at', 'krops.io/run-kind'], s['clusters'][cluster]['tags']))}}))
    if s.get('malformed_cluster') == cluster: output = '{bad json'
elif operation == 'list-nodegroups': output = 'pool-a\\npool-b'
elif operation == 'describe-nodegroup': output = s['clusters'][cluster].get('asgs', 'asg-' + cluster)
elif operation == 'describe-auto-scaling-groups':
    asg = arg('--auto-scaling-group-names')
    count = s.setdefault('counts', {}).get(asg, 0) + 1
    s['counts'][asg] = count
    output = '' if s.get('membership_changed') and count > 1 else s['asgs'][asg]
elif operation == 'describe-instances':
    i = arg('--instance-ids')
    if s.get('disappear') == i or s.get('describe_error') == i:
        print('InvalidInstanceID.NotFound' if s.get('disappear') == i else 'AccessDenied', file=sys.stderr)
        p.write_text(json.dumps(s)); sys.exit(1)
    v = s['instances'][i]
    tags = [{'Key': 'aws:eks:cluster-name', 'Value': v['owner']}]
    tags += [{'Key': k, 'Value': value} for k, value in zip(['krops.io/run-id', 'krops.io/revision', 'krops.io/expires-at', 'krops.io/run-kind'], v['tags']) if value is not None]
    output = json.dumps(v.get('response', {'Reservations': [{'Instances': [{'InstanceId': i, 'State': {'Name': v.get('state', 'running')}, 'Tags': tags}]}]}))
elif operation == 'create-tags':
    i = arg('--resources')
    if s.get('deny_write') == i:
        print('AccessDenied', file=sys.stderr); p.write_text(json.dumps(s)); sys.exit(1)
    assert len(a[a.index('--tags') + 1:]) == 1
    tags = json.loads(arg('--tags'))
    s['writes'].append([arg('--region'), i, tags])
    s['instances'][i]['tags'] = [t['Value'] for t in tags]
    output = ''
else: raise RuntimeError(a)
p.write_text(json.dumps(s))
print(output)
'''


class WorkerTags(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        for name in ('reconcile.sh', 'config.env', 'tag_data.py'):
            shutil.copy(COMPONENT / name, self.path / name)
        (self.path / 'aws').write_text(FAKE)
        (self.path / 'aws').chmod(0o755)
        self.state = {'calls': [], 'writes': [], 'clusters': {}, 'asgs': {}, 'instances': {}}
        for n, cluster in enumerate(CLUSTERS):
            instance = f'i-{n + 1:017x}'
            self.state['clusters'][cluster] = {'tags': [f'run-{n}', f'sha-{n}', 'never', 'ci']}
            self.state['asgs']['asg-' + cluster] = instance
            self.state['instances'][instance] = {'owner': cluster, 'tags': [None] * 4, 'other': 'preserved'}

    def tearDown(self):
        self.tmp.cleanup()

    def run_reconcile(self):
        state_path = self.path / 'state.json'
        state_path.write_text(json.dumps(self.state))
        result = subprocess.run(['bash', str(self.path / 'reconcile.sh')], env={**os.environ, 'PATH': f'{self.path}:{os.environ["PATH"]}', 'STATE': str(state_path)}, capture_output=True, text=True, check=False)
        self.state = json.loads(state_path.read_text())
        return result

    def test_backfill_idempotence_drift_new_workers(self):
        result = self.run_reconcile()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.state['writes']), 3)
        for _, instance, tags in self.state['writes']:
            n = int(instance[2:], 16) - 1
            self.assertEqual(tags, [{'Key': key, 'Value': value} for key, value in zip(KEYS, self.state['clusters'][CLUSTERS[n]]['tags'])])
            self.assertEqual(self.state['instances'][instance]['other'], 'preserved')
        self.run_reconcile()
        self.assertEqual(len(self.state['writes']), 3)
        self.state['instances']['i-00000000000000001']['tags'][1] = 'old'
        self.run_reconcile()
        self.assertEqual(len(self.state['writes']), 4)
        new = 'i-00000000000000004'
        self.state['asgs']['asg-' + CLUSTERS[0]] += '\t' + new
        self.state['instances'][new] = {'owner': CLUSTERS[0], 'tags': [None] * 4}
        self.run_reconcile()
        self.assertEqual(len(self.state['writes']), 5)

    def test_safety_rejections(self):
        for mode in ('account', 'owner', 'invalid_id', 'missing_tag', 'malformed_tag', 'membership_changed'):
            with self.subTest(mode=mode):
                if mode == 'account': self.state['account'] = '999999999999'
                elif mode == 'owner': self.state['instances']['i-00000000000000001']['owner'] = 'foreign'
                elif mode == 'invalid_id': self.state['asgs']['asg-' + CLUSTERS[0]] = 'i-bad'
                elif mode == 'missing_tag': self.state['clusters'][CLUSTERS[0]]['tags'][0] = None
                elif mode == 'malformed_tag': self.state['clusters'][CLUSTERS[0]]['tags'][2] = 'tomorrow'
                else: self.state['membership_changed'] = True
                self.state['writes'] = []
                self.state['counts'] = {}
                result = self.run_reconcile()
                self.assertFalse(any(w[1] == 'i-00000000000000001' for w in self.state['writes']))
                self.assertEqual(result.returncode, 0 if mode == 'membership_changed' else 1)
                self.setUp_state()

    def setUp_state(self):
        self.state = {'calls': [], 'writes': [], 'clusters': {}, 'asgs': {}, 'instances': {}}
        for n, cluster in enumerate(CLUSTERS):
            instance = f'i-{n + 1:017x}'
            self.state['clusters'][cluster] = {'tags': [f'run-{n}', f'sha-{n}', 'never', 'ci']}
            self.state['asgs']['asg-' + cluster] = instance
            self.state['instances'][instance] = {'owner': cluster, 'tags': [None] * 4, 'other': 'preserved'}

    def test_failure_isolation_disappearance_and_termination(self):
        for mode in ('fail_cluster', 'deny_write', 'describe_error', 'disappear', 'terminated'):
            self.setUp_state()
            if mode == 'fail_cluster': self.state[mode] = CLUSTERS[0]
            elif mode == 'terminated': self.state['instances']['i-00000000000000001']['state'] = 'terminated'
            else: self.state[mode] = 'i-00000000000000001'
            result = self.run_reconcile()
            self.assertEqual(result.returncode, 0 if mode in ('disappear', 'terminated') else 1)
            self.assertEqual(len(self.state['writes']), 2)

    def test_multiple_asgs_paginated_output_and_dedupe(self):
        self.state['clusters'][CLUSTERS[0]]['asgs'] = 'asg-' + CLUSTERS[0] + '\nextra'
        self.state['asgs']['extra'] = 'i-00000000000000001'
        self.assertEqual(self.run_reconcile().returncode, 0)
        self.assertEqual(len(self.state['writes']), 3)
        self.assertFalse(any('--no-paginate' in call for call in self.state['calls']))
        self.assertTrue(any('pool-b' in call for call in self.state['calls']))

    def test_forbidden_region_or_cluster(self):
        for entry in ('us-east-1:default_eu-north-1-management-control-plane', 'eu-north-1:foreign'):
            (self.path / 'config.env').write_text(f'EXPECTED_ACCOUNT=120392301094\nCLUSTERS="{entry}"\n')
            self.state['calls'] = []
            self.assertEqual(self.run_reconcile().returncode, 1)
            self.assertFalse(self.state['writes'])
            self.assertEqual(len(self.state['calls']), 1)

    def test_rendered_cluster_and_component_wiring(self):
        rendered_clusters = set()
        for overlay in ['eu-north-1/management', 'eu-north-1/staging', 'eu-west-1/staging']:
            result = subprocess.run(['kubectl', 'kustomize', str(ROOT / 'mgmt/aws/clusters' / overlay)],
                                    check=True, capture_output=True, text=True)
            for doc in yaml.safe_load_all(result.stdout):
                if doc and doc.get('kind') == 'AWSManagedControlPlane':
                    metadata = doc['metadata']
                    # CAPA's pinned default is namespace_control-plane-name.
                    rendered_clusters.add(doc['spec'].get('eksClusterName') or
                                          f"{metadata.get('namespace', 'default')}_{metadata['name']}")
        self.assertEqual(rendered_clusters, set(CLUSTERS))
        bootstrap = tomllib.loads((ROOT / 'bootstrap.toml').read_text())
        teardown = bootstrap['environments']['aws']['teardown']
        lifecycle_names = {teardown['mgmt-eks-cluster-name'],
                           *(workload['eks-cluster-name'] for workload in teardown['aws-workloads'])}
        self.assertEqual(rendered_clusters, lifecycle_names)
        allowlist = (COMPONENT / 'config.env').read_text().split('CLUSTERS="', 1)[1].split('"', 1)[0]
        self.assertEqual({entry.split(':', 1)[1] for entry in allowlist.split()}, rendered_clusters)
        result = subprocess.run(['kubectl', 'kustomize', str(COMPONENT)],
                                check=True, capture_output=True, text=True)
        docs = list(yaml.safe_load_all(result.stdout))
        config = next(doc for doc in docs if doc['kind'] == 'ConfigMap')
        job = next(doc for doc in docs if doc['kind'] == 'CronJob')
        scripts = next(volume for volume in job['spec']['jobTemplate']['spec']['template']['spec']['volumes']
                       if volume['name'] == 'scripts')
        self.assertEqual(scripts['configMap']['name'], config['metadata']['name'])
        self.assertEqual(config['data']['reconcile.sh'], (COMPONENT / 'reconcile.sh').read_text())
        self.assertEqual(config['data']['tag_data.py'], (COMPONENT / 'tag_data.py').read_text())
        parent = yaml.safe_load((COMPONENT.parent / 'kustomization.yaml').read_text())
        self.assertIn('worker-run-tags', parent['resources'])

    def test_structured_values_preserved_and_idempotent(self):
        values = ['\t run @example+42=prod/one \n', '\r feature/team @commit+1=ok \f', '2028-02-29T12:34:56Z', ' ci @ops+blue=one \r\n']
        self.state['clusters'][CLUSTERS[0]]['tags'] = values
        result = self.run_reconcile()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state['instances']['i-00000000000000001']['tags'], values)
        writes = len(self.state['writes'])
        self.assertEqual(self.run_reconcile().returncode, 0)
        self.assertEqual(len(self.state['writes']), writes)

    def test_invalid_json_and_required_values_block_writes(self):
        for response in [{}, {'cluster': {'tags': {}}},
                         {'cluster': {'tags': dict(zip(KEYS, ['', 'sha', 'never', 'ci']))}},
                         {'cluster': {'tags': dict(zip(KEYS, [42, 'sha', 'never', 'ci']))}}]:
            self.setUp_state()
            self.state['clusters'][CLUSTERS[0]]['response'] = response
            self.assertEqual(self.run_reconcile().returncode, 1)
            self.assertFalse(any(write[1] == 'i-00000000000000001' for write in self.state['writes']))
        self.setUp_state()
        self.state['malformed_cluster'] = CLUSTERS[0]
        self.assertEqual(self.run_reconcile().returncode, 1)
        self.assertEqual(len(self.state['writes']), 2)
        self.setUp_state()
        self.state['instances']['i-00000000000000001']['response'] = {'Reservations': 'bad'}
        self.assertEqual(self.run_reconcile().returncode, 1)
        self.assertEqual(len(self.state['writes']), 2)

    def helper(self, values):
        result = subprocess.run(['python3', str(COMPONENT / 'tag_data.py'), 'cluster'],
                                input=json.dumps({'cluster': {'tags': dict(zip(KEYS, values))}}).encode(),
                                capture_output=True, check=False)
        return result

    def test_helper_requires_each_nonempty_string(self):
        valid = dict(zip(KEYS, ['run', 'sha', 'never', 'ci']))
        for key in KEYS:
            for value in [None, '', 42]:
                tags = {**valid, key: value}
                result = subprocess.run(['python3', str(COMPONENT / 'tag_data.py'), 'cluster'],
                                        input=json.dumps({'cluster': {'tags': tags}}).encode(),
                                        capture_output=True, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b'')
            tags = {k: v for k, v in valid.items() if k != key}
            result = subprocess.run(['python3', str(COMPONENT / 'tag_data.py'), 'cluster'],
                                    input=json.dumps({'cluster': {'tags': tags}}).encode(),
                                    capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b'')

    def test_expiry_calendar_validation_and_encoding_bounds(self):
        invalid = ['2026-13-01T00:00:00Z', '2026-01-32T00:00:00Z', '2027-02-29T00:00:00Z',
                   '2026-01-01T24:00:00Z', '2026-01-01T00:60:00Z', '2026-01-01T00:00:60Z',
                   '0000-01-01T00:00:00Z', '2026-1-01T00:00:00Z']
        for expiry in invalid:
            with self.subTest(expiry=expiry):
                self.assertNotEqual(self.helper(['run', 'sha', expiry, 'ci']).returncode, 0)
        for expiry in ['never', '2028-02-29T12:34:56Z', '0001-01-01T00:00:00Z', '9999-12-31T23:59:59Z']:
            self.assertEqual(self.helper(['run', 'sha', expiry, 'ci']).returncode, 0)
        for value in ['x' * 257, 'bad\0value', 'nonasciié', 'bad,comma', 'bad\vvalue']:
            self.assertNotEqual(self.helper([value, 'sha', 'never', 'ci']).returncode, 0)
        values = ['x' * 256, '\t exact @+=/ value \n\f\r', 'never', ' ci ']
        result = self.helper(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = result.stdout.split(b'\0')
        self.assertEqual([field.decode() for field in fields[:4]], values)
        self.assertEqual(json.loads(fields[4]), [{'Key': key, 'Value': value} for key, value in zip(KEYS, values)])
        self.setUp_state()
        self.state['clusters'][CLUSTERS[0]]['tags'][2] = invalid[2]
        self.assertEqual(self.run_reconcile().returncode, 1)
        self.assertFalse(any(write[1] == 'i-00000000000000001' for write in self.state['writes']))

    def test_manifest_and_docs_contract(self):
        job = yaml.safe_load((COMPONENT / 'cronjob.yaml').read_text())
        spec = job['spec']
        self.assertEqual(spec['schedule'], '* * * * *')
        self.assertEqual(spec['concurrencyPolicy'], 'Forbid')
        pod = spec['jobTemplate']['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        container = pod['containers'][0]
        self.assertRegex(container['image'], r'^public.ecr.aws/aws-cli/aws-cli:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$')
        env = {e['name']: e['value'] for e in container['env']}
        self.assertEqual(env['AWS_PROFILE'], 'default')
        self.assertEqual(env['AWS_EC2_METADATA_DISABLED'], 'true')
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        credentials = next(v for v in pod['volumes'] if v['name'] == 'credentials')['secret']
        self.assertEqual(credentials['secretName'], 'aws-credentials')
        self.assertEqual(credentials['items'][0]['key'], 'credentials')
        ks = yaml.safe_load((COMPONENT / 'flux-ks.yaml').read_text())['spec']
        self.assertTrue(ks['wait'])
        self.assertEqual({d['name'] for d in ks['dependsOn']}, {'ack-controllers', 'aws-managed-clusters'})
        for cluster in CLUSTERS:
            self.assertIn(cluster, (COMPONENT / 'config.env').read_text())
        iam = (ROOT / 'docs/aws-iam.md').read_text()
        for text in ['ec2:CreateTags', 'ec2:ResourceTag/aws:eks:cluster-name', 'aws:TagKeys', *KEYS]:
            self.assertIn(text, iam)


if __name__ == '__main__':
    unittest.main()
