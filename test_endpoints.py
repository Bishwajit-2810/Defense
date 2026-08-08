import urllib.request
import json

try:
    req = urllib.request.urlopen('http://127.0.0.1:8001/v1/config/llm')
    print(req.read().decode())
except Exception as e:
    print(e)

try:
    req = urllib.request.urlopen('http://127.0.0.1:8001/v1/config/nlp')
    print(req.read().decode())
except Exception as e:
    print(e)
