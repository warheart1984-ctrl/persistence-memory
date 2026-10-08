import importlib.util, sys, time, threading, json
spec=importlib.util.spec_from_file_location("c","scripts/chaos/cl_chaos_100x.py"); c=importlib.util.module_from_spec(spec); sys.modules["c"]=c; spec.loader.exec_module(c)
stack=c.load_stack("/tmp/jarvis-chaos100x"); key=open(stack["secrets_dir"]+"/api-key").read().strip()
stats=c.Stats(); cl=c.Client(stack["url"],key,stats)
rec=cl.post("/api/jarvis/memory",{"content":"partition experiment","source_agent":"x","session_id":"s","type":"decision","evidence":[{"kind":"user-request","ref":"x"}]}).json["memory"]["id"]
for i in range(30): cl.get(f"/api/jarvis/memory/{rec}")   # warm the pool so connections are established and idle
log=[]; stop=threading.Event(); t0=time.time()
def loop(kind,path,key_=True):
    while not stop.is_set():
        a=time.time(); r=cl.get(path,timeout=90,key=key_); b=time.time(); log.append((kind,round(a-t0,1),round(b-t0,1),r.status))
ths=[threading.Thread(target=loop,args=("read",f"/api/jarvis/memory/{rec}")) for _ in range(3)]+[threading.Thread(target=loop,args=("ready","/ready",False))]
[t.start() for t in ths]; time.sleep(2)
import subprocess
net,db=stack["network"],stack["containers"]["db"]
tcut=time.time()-t0; subprocess.run(["docker","network","disconnect",net,db],check=True)
HOLD=int(sys.argv[1]); time.sleep(HOLD); theal=time.time()-t0
subprocess.run(["docker","network","connect","--alias","db",net,db],check=True)
time.sleep(25); stop.set(); [t.join(100) for t in ths]
print("cut at",round(tcut,1),"healed at",round(theal,1))
for row in sorted(log,key=lambda r:r[1]):
    if row[2]>tcut+0.2 or row[3]!=200: print(row)
