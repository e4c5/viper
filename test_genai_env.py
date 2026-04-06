from google import genai
import os

os.environ["GOOGLE_API_KEY"] = "fake-google-key"
os.environ.pop("GEMINI_API_KEY", None)
try:
    client = genai.Client()
    print("GenAI picks up GOOGLE_API_KEY?", client.api_key)
except Exception as e:
    print("Failed with GOOGLE_API_KEY:", repr(e))

os.environ.pop("GOOGLE_API_KEY", None)
os.environ["GEMINI_API_KEY"] = "fake-gemini-key"
try:
    client = genai.Client()
    print("GenAI picks up GEMINI_API_KEY?", client.api_key)
except Exception as e:
    print("Failed with GEMINI_API_KEY:", repr(e))
