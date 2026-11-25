import json, urllib.request, collections, statistics
rows = []
for i in range(101):
    d = json.load(urllib.request.urlopen("http://127.0.0.1:5000/health"))
    if i:
        rows.append((d["score"], d["h_storage"], d["h_memory"], d["h_compute"], d.get("benchmark_ops")))
scores = [r[0] for r in rows]
print("distinct score:", len(set(scores)), "range:", min(scores), max(scores))
for name, k in (("h_storage",1),("h_memory",2),("h_compute",3)):
    v = [r[k] for r in rows]
    print(name, "distinct:", len(set(v)), "min:", min(v), "max:", max(v))
ops = [r[4] for r in rows if r[4]]
print("benchmark_ops mean:", statistics.mean(ops), "sd:", statistics.pstdev(ops))