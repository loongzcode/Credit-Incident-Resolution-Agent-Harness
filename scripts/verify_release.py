"""Runs release checks against one clean commit; produces results, never edits them."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from credit_harness.production.schema import HEAD


ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(['git',*args],cwd=ROOT,text=True).strip()


def summary(path):
    root = ET.parse(path).getroot()
    suites = [root] if root.tag=='testsuite' else root.findall('testsuite')
    totals = {k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
    totals['passed'] = totals['tests']-totals['failures']-totals['errors']-totals['skipped']
    return totals


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--workers',default='4')
    args = parser.parse_args()
    if git('status','--porcelain'):
        raise SystemExit('Commit all source changes before release verification')
    if not os.environ.get('TEST_POSTGRES_URL'):
        raise SystemExit('Dedicated TEST_POSTGRES_URL required; cannot silently skip PostgreSQL')
    sha = git('rev-parse','HEAD'); out = ROOT/'.local/release'/sha; out.mkdir(parents=True,exist_ok=True)
    npm = shutil.which('npm.cmd' if os.name=='nt' else 'npm')
    py = sys.executable
    commands = [
        ('static',[py,'-m','scripts.static_checks'],ROOT,None),
        ('lint',[py,'-m','scripts.lint'],ROOT,None),
        ('sqlite',[py,'-m','pytest','-n',args.workers,'-q','--basetemp='+str(out/'sqlite-tmp'),'--junitxml='+str(out/'sqlite.xml')],ROOT,'sqlite.xml'),
        ('postgresql',[py,'-m','pytest','-n',args.workers,'-q','--postgres','--basetemp='+str(out/'pg-tmp'),'--junitxml='+str(out/'postgresql.xml')],ROOT,'postgresql.xml'),
        ('frontend',[npm,'test','--','--reporter=junit','--outputFile='+str(out/'frontend.xml')],ROOT/'frontend','frontend.xml'),
        ('build',[npm,'run','build'],ROOT/'frontend',None),
    ]
    report = dict(commit_sha=sha,migration_revision=HEAD,started_at=datetime.now(timezone.utc).isoformat(),checks={})
    for name,command,cwd,xml in commands:
        if git('rev-parse','HEAD') != sha or git('status','--porcelain'):
            report['source_changed']=True; break
        print('RUNNING '+name,flush=True)
        log = out/(name+'.log')
        with log.open('w',encoding='utf-8') as f:
            result = subprocess.run(command,cwd=cwd,stdout=f,stderr=subprocess.STDOUT,timeout=3600)
        check = dict(exit_code=result.returncode,log_sha256=hashlib.sha256(log.read_bytes()).hexdigest())
        if xml and (out/xml).exists(): check.update(summary(out/xml))
        report['checks'][name] = check
        print(name+': '+json.dumps(check),flush=True)
    report['same_final_sha'] = git('rev-parse','HEAD')==sha and not git('status','--porcelain')
    report['passed'] = report['same_final_sha'] and len(report['checks'])==len(commands) and all(c['exit_code']==0 for c in report['checks'].values())
    report['timestamp'] = datetime.now(timezone.utc).isoformat()
    (out/'release-verification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(str(out/'release-verification.json'),flush=True)
    raise SystemExit(0 if report['passed'] else 1)


if __name__=='__main__': main()
