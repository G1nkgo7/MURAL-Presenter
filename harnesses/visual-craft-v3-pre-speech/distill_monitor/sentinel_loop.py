import json, subprocess, datetime, time, os
BASE=os.environ.get(
    "VISUAL_CRAFT_WORKSPACE",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)
MAN=f"{BASE}/distillation/log/complex_5k_html_0625_1917.manifest.jsonl"
LOG=f"{BASE}/distillation/distill_monitor/sentinel.log"
ROUNDS=30
INTERVAL=300  # 5 min

def pg(pat):
    r=subprocess.run(['pgrep','-f',pat],capture_output=True,text=True)
    return len([x for x in r.stdout.split() if x.strip()])>0

def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def writelog(s):
    with open(LOG,'a') as f: f.write(s+"\n")

def probe():
    main = pg('distill_ppt.py.*complex_5k_html_0625_1917')
    watch = pg('watch.sh.*complex_5k')
    dash = pg('server.py.*--port 8000')
    lines=[]
    with open(MAN) as f:
        for line in f:
            line=line.strip()
            if line: lines.append(line)
    w=lines[-40:]
    comp=rej=err=apifail=0; last_fin=None
    for line in w:
        try: d=json.loads(line)
        except: continue
        st=d.get('status')
        if st=='completed': comp+=1
        elif st=='rejected': rej+=1
        elif st=='error': err+=1
        if 'api_failed' in line: apifail+=1
        f=d.get('finished_at')
        if f: last_fin=f
    cum=set()
    for line in lines:
        try: d=json.loads(line)
        except: continue
        if d.get('status')=='completed': cum.add(d.get('sample_id'))
    cumc=len(cum)
    den=comp+rej+err
    acc=(comp/den*100) if den else 0.0
    return dict(main=main,watch=watch,dash=dash,comp=comp,rej=rej,err=err,
               apifail=apifail,acc=acc,cum=cumc,last_fin=last_fin)

stall=0; prev_cum=None
for rnd in range(2, ROUNDS+1):  # round 1 already done manually
    time.sleep(INTERVAL)
    try:
        p=probe()
    except Exception as e:
        writelog(f"[{now()}] ROUND{rnd} PROBE-ERROR {e}")
        continue
    procflag='OK' if (p['main'] and p['watch'] and p['dash']) else 'PROC-ISSUE'
    line=(f"[{now()}] R{rnd} main={'Y' if p['main'] else 'N'} watch={'Y' if p['watch'] else 'N'} "
          f"dash={'Y' if p['dash'] else 'N'} | last40: comp={p['comp']} rej={p['rej']} err={p['err']} "
          f"api_failed={p['apifail']} acc={p['acc']:.0f}% | cum_completed={p['cum']} "
          f"| last_finished={p['last_fin']} | {procflag}")
    writelog(line)

    # stall detection
    if prev_cum is not None and p['cum']<=prev_cum:
        stall+=1
    else:
        stall=0
    prev_cum=p['cum']

    # anomaly markers
    if not p['main']:
        writelog(f"[{now()}] ⚠️ R{rnd} MAIN SCHEDULER DOWN — production stopped, escalate to lead")
    if p['apifail']>=5:
        writelog(f"[{now()}] ⚠️ R{rnd} api_failed={p['apifail']} (>=5) — possible key/gateway issue, escalate")
    den=p['comp']+p['rej']+p['err']
    if den>0 and p['acc']<60:
        writelog(f"[{now()}] ⚠️ R{rnd} accept rate {p['acc']:.0f}% (<60%) — quality drop, escalate")
    if stall>=3:
        writelog(f"[{now()}] ⚠️ R{rnd} cum_completed stalled {stall} rounds at {p['cum']} — possible stuck/FUSE saturation, escalate")

    # lightweight maintenance: revive watcher if dead
    if not p['watch']:
        try:
            subprocess.Popen(
                "cd "+BASE+"/distillation && nohup ./distill_monitor/watch.sh complex_5k_html_0625_1917 8 7200 "
                "> distill_monitor/complex_5k_html_0625_1917.watch.out 2>&1 &",
                shell=True)
            time.sleep(3)
            revived=pg('watch.sh.*complex_5k')
            writelog(f"[{now()}] R{rnd} MAINTENANCE: watcher was dead, restarted (now {'alive' if revived else 'STILL DOWN'})")
        except Exception as e:
            writelog(f"[{now()}] R{rnd} MAINTENANCE: watcher restart FAILED {e}")

writelog(f"[{now()}] SENTINEL LOOP COMPLETE ({ROUNDS} rounds)")
