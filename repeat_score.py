import json, urllib.request, collections
scores = []
for i in range(101):
    d = json.load(urllib.request.urlopen("http://127.0.0.1:5000/health"))
    if i:                      # discard the first run
        scores.append(d["score"])
print("distinct:", len(set(scores)), "min:", min(scores), "max:", max(scores))
print(collections.Counter(scores).most_common(5))