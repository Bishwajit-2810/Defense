from defense.services.api.routers.analysis import _result_to_html

res = {
    "post_id": "123",
    "text_sentiment": None,
    "image_sentiment": None,
    "emotion": None,
    "confidence": None,
    "comment_analysis": None
}

try:
    _result_to_html(res)
    print("Success")
except Exception as e:
    import traceback
    traceback.print_exc()
