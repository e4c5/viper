import os
from code_review.models import get_configured_model

def main():
    model = get_configured_model()
    print("Model:", model)
    try:
        response = model.generate("hello hello can you reply with a single word yes")
        print("Response:", response.text)
    except Exception as e:
        print("Error:", repr(e))

if __name__ == "__main__":
    main()
