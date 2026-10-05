#!/usr/bin/env python3
"""OCI publication works with host metadata and inaccessible worktree Git (#426)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import re

ROOT = Path(__file__).resolve().parents[1]
KEYS = ('KROPS_OCI_GIT_SHA', 'KROPS_OCI_GIT_REF', 'KROPS_OCI_SOURCE_URL')


def run(args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=False)


def main():
    config = (ROOT / 'mise.local-host.toml').read_text()
    section = config.split('[tasks.oci-push]', 1)[1].split('[tasks.kubeconfigs]', 1)[0]
    task = {'run': section.split("run = '''", 1)[1].split("'''", 1)[0]}
    directory = re.search(r'^dir = "(.*)"$', section, re.M)
    if directory:
        task['dir'] = directory.group(1)
    with tempfile.TemporaryDirectory(prefix='oci worktree ') as temporary:
        base = Path(temporary).resolve()
        repo = base / 'main repo'
        repo.mkdir()
        def git(*args, cwd=repo):
            result = run(['git', *args], cwd)
            assert result.returncode == 0, result.stderr
            return result.stdout.strip()
        git('init', '-b', 'main')
        git('config', 'user.email', 'test@example.invalid')
        git('config', 'user.name', 'Test')
        (repo / 'seed').write_text('seed')
        git('add', 'seed')
        git('commit', '-m', 'seed')
        git('remote', 'add', 'origin', 'https://example.invalid/krops.git')
        checkout = base / 'linked checkout'
        git('worktree', 'add', '-b', 'worktree-test', str(checkout))
        (checkout / 'scripts').mkdir()
        shutil.copy(ROOT / 'scripts/toolbox-run.sh', checkout / 'scripts/toolbox-run.sh')
        for path in ('mgmt/local-host', 'workload/local-host'):
            (checkout / path).mkdir(parents=True)
            (checkout / path / 'marker').write_text(path)
        binary = base / 'bin'
        binary.mkdir()
        log = base / 'log.json'
        def stub(name, body):
            file = binary / name
            file.write_text('#!/usr/bin/env python3\n' + body)
            file.chmod(0o755)
        stub('docker', "import os,sys,json\nif sys.argv[1]=='run':\n json.dump({'env':{k:os.environ.get(k) for k in " + repr(KEYS) + "},'args':sys.argv[1:]},open(os.environ['OCI_TEST_LOG'],'w'))\n")
        stub('curl', 'pass\n')
        stub('flux', "import sys,os,json\nfrom pathlib import Path\na=sys.argv[1:]\np=Path(next(x[7:] for x in a if x.startswith('--path=')))\nassert sorted(str(f.relative_to(p)) for f in p.rglob('*') if f.is_file())==['mgmt/local-host/marker','workload/local-host/marker']\nassert (p/'mgmt/local-host/marker').read_text()=='mgmt/local-host'\nassert (p/'workload/local-host/marker').read_text()=='workload/local-host'\njson.dump(a,open(os.environ['OCI_TEST_LOG'],'w'))\n")
        env = {k: v for k, v in os.environ.items() if k not in KEYS}
        env.update(PATH=f'{binary}:{env["PATH"]}', CONTAINER_ENGINE='docker', OCI_TEST_LOG=str(log))
        sha = git('rev-parse', 'HEAD', cwd=checkout)
        for ref in ('worktree-test', 'detached'):
            if ref == 'detached':
                git('checkout', '--detach', cwd=checkout)
            # Stale process and .env values must be replaced with current host facts.
            stale = dict(env, **dict.fromkeys(KEYS, 'stale'))
            (checkout / '.env').write_text('\n'.join(f'{k}=stale-env' for k in KEYS))
            result = run(['bash', 'scripts/toolbox-run.sh', 'bootstrap', 'local-host'], checkout, stale)
            assert result.returncode == 0, result.stderr
            metadata = json.loads(log.read_text())
            expected = dict(zip(KEYS, (sha, ref, 'https://example.invalid/krops.git')))
            assert metadata['env'] == expected, metadata
            assert all(k in metadata['args'] for k in KEYS), metadata
            # Simulate a container mount whose .git file points outside its filesystem.
            gitfile = checkout / '.git'
            original = gitfile.read_text()
            gitfile.write_text('gitdir: /inaccessible/host/worktree\n')
            stub('git', "import sys\nprint('Git must not be called',file=sys.stderr)\nsys.exit(91)\n")
            result = run(['bash', '-e', '-c', task['run']], checkout, dict(env, **expected))
            assert result.returncode == 0, result.stderr
            args = json.loads(log.read_text())
            assert f'--revision={ref}@sha1:{sha}' in args, args
            assert '--source=https://example.invalid/krops.git' in args, args
            for supplied in ({}, {KEYS[0]: sha}, dict.fromkeys(KEYS, '')):
                result = run(['bash', '-e', '-c', task['run']], checkout, dict(env, **supplied))
                assert result.returncode != 0, result.stdout
                assert 'ERROR:' in result.stderr, result.stderr
            gitfile.write_text(original)
            (binary / 'git').unlink()
        (checkout / '.env').unlink()
        for origin in (True, False):
            if not origin:
                git('remote', 'remove', 'origin')
            result = run(['bash', '-e', '-c', task['run']], checkout, env)
            assert result.returncode == 0, result.stderr
            args = json.loads(log.read_text())
            source = 'https://example.invalid/krops.git' if origin else f'file://{checkout}'
            assert f'--source={source}' in args, args
            assert f'--revision=detached@sha1:{sha}' in args, args
    assert task.get('dir') == '{{config_root}}', task
    print('OCI worktree host metadata, detached HEAD, native fallback and failure cases OK')


if __name__ == '__main__':
    main()
