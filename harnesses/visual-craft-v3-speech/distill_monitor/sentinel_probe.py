import json, subprocess, datetime, sys, os
BASE=os.environ.get(
    "VISUAL_CRAFT_WORKSPACE",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)
MAN=f"{BASE}/distillation/log/complex_5k_html_0625_1917.manifest.jsonl"
LOG=f"{BASE}/distillation/distill_monitor/sentinel.log"

def pg(pat):
    r=subprocess.run(['pgrep','-f',pat],capture_output=True,text=True)
    return len([x for x in r.stdout.split() if x.strip()])>0

main = pg('distill_ppt.py.*complex_5k_html_0625_1917')
watch = pg('watch.sh.*complex_5k')
dash = pg('server.py.*--port 8000')

# last 40 window
lines=[]
with open(MAN) as f:
    for line in f:
        line=line.strip()
        if line: lines.append(line)
w=lines[-40:]
comp=rej=err=apifail=0; last_fin=None
for line in w:
    d=json.loads(line)
    st=d.get('status')
    if st=='completed': comp+=1
    elif st=='rejected': rej+=1
    elif st=='error': err+=1
    if 'api_failed' in line: apifail+=1
    f=d.get('finished_at')
    if f: last_fin=f

# cumulative unique completed
cum=set()
for line in lines:
    d=json.loads(line)
    if d.get('status')=='completed': cum.add(d.get('sample_id'))
cumc=len(cum)

den=comp+rej+err
acc = (comp/den*100) if den else 0.0
ts=datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
procflag = 'OK' if (main and watch and dash) else 'PROC-ISSUE'
line=(f"[{ts}] main={'Y' if main else 'N'} watch={'Y' if watch else 'N'} dash={'Y' if dash else 'N'} "
      f"| last40: comp={comp} rej={rej} err={err} api_failed={apifail} acc={acc:.0f}% "
      f"| cum_completed={cumc} | last_finished={last_fin} | {procflag}")
print(line)
with open(LOG,'a') as f:
    f.write(line+"\n")
# emit machine-readable for the driver
print(f"PARSE main={int(main)} watch={int(watch)} dash={int(dash)} apifail={apifail} acc={acc:.1f} cum={cumc}")
