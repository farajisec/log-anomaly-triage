#!/usr/bin/env python3
"""Score triage candidates against labeled anomaly windows.

An event is a true positive if it overlaps a labeled anomaly window for the
same service; a labeled window with no overlapping event is a false
negative; an event overlapping no labeled window is a false positive.
Detection latency is how long after a window opened the event was raised.
"""
from __future__ import annotations
import argparse, json
from datetime import datetime

def _t(s): return datetime.fromisoformat(s)

def overlap(a0,a1,b0,b1): return a0 <= b1 and b0 <= a1

def score(candidates, labels):
    wins = [{"service":a["service"],"s":_t(a["start"]),"e":_t(a["end"]),
             "type":a["type"],"hit":False} for a in labels["anomalies"]]
    tp=fp=0; latencies=[]
    for c in candidates:
        cs,ce=_t(c["start"]),_t(c["end"])
        matched=None
        for w in wins:
            if w["service"]==c["service"] and overlap(cs,ce,w["s"],w["e"]):
                matched=w; break
        if matched:
            tp+=1
            if not matched["hit"]:
                matched["hit"]=True
                latencies.append(max(0.0,(cs-matched["s"]).total_seconds()/60))
        else:
            fp+=1
    fn=sum(1 for w in wins if not w["hit"])
    prec=tp/(tp+fp) if (tp+fp) else None
    rec=sum(w["hit"] for w in wins)/len(wins) if wins else None
    f1=(2*prec*rec/(prec+rec) if prec and rec and (prec+rec) else None)
    return {"TP":tp,"FP":fp,"FN":fn,"precision":prec,"recall":rec,"f1":f1,
            "mean_detection_latency_min":(round(sum(latencies)/len(latencies),2)
                                          if latencies else None),
            "windows":len(wins)}

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queue",default="data/triage_queue.json")
    ap.add_argument("--labels",default="data/labels.json")
    ap.add_argument("--out",default="data/eval.json")
    a=ap.parse_args()
    with open(a.queue) as f: q=json.load(f)["candidates"]
    with open(a.labels) as f: labels=json.load(f)
    res=score(q,labels)
    with open(a.out,"w") as f: json.dump(res,f,indent=2)
    def fmt(x): return "n/a" if x is None else (f"{x:.3f}" if isinstance(x,float) else str(x))
    print(f"TP={res['TP']} FP={res['FP']} FN={res['FN']} / {res['windows']} windows")
    print(f"precision={fmt(res['precision'])} recall={fmt(res['recall'])} "
          f"f1={fmt(res['f1'])} mean_latency_min={fmt(res['mean_detection_latency_min'])}")

if __name__=="__main__": main()
