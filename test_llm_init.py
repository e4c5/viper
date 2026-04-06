import os
from code_review.models import get_configured_model

def main():
    model = get_configured_model()
    print("Model instance:", model)
    print("Type:", type(model))
    if hasattr(model, 'model'):
        print("Model name:", model.model)
    if hasattr(model, 'api_key'):
        print("API Key set:", model.api_key is not None)
        print("API Key value snippet:", model.api_key[:5] + "..." if model.api_key else "None")
    print("GOOGLE_API_KEY in env:", "GOOGLE_API_KEY" in os.environ)

if __name__ == "__main__":
    main()
