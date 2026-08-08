import openai
import httpx

try:
    raise httpx.ConnectError("Failed to connect")
except Exception as e:
    exc1 = e

try:
    raise openai.RateLimitError("Rate limited", response=httpx.Response(429, request=httpx.Request("GET", "http://a")), body=None)
except Exception as e:
    exc2 = e

try:
    print("error=%s" % exc1)
except Exception as e:
    print("exc1 failed:", type(e), e)

try:
    print("error=%s" % exc2)
except Exception as e:
    print("exc2 failed:", type(e), e)

try:
    print("role=%s model=%s error=%s" % ("a", "b", exc2))
except Exception as e:
    print("exc2 multiple failed:", type(e), e)
