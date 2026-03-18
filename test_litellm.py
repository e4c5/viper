import litellm

# litellm.suppress_debug_info = True

messages = [{"role": "user", "content": "hello"}]
for _ in range(2):
    response = litellm.completion(
        model="openrouter/google/gemini-2.5-flash",
        messages=messages,
        api_key="sk-or-fake",
        mock_response="hi" # Use mock to avoid API call
    )
    print("Done call")
