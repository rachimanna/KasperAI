from config.settings import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GROQ_API_KEY,
    GROQ_MODEL,
    CEREBRAS_API_KEY,
    CEREBRAS_MODEL,
    AI_PROVIDERS,
)

def status(value):
    return "SET" if value else "EMPTY"

print(f"Gemini key: {status(GEMINI_API_KEY)}")
print(f"Gemini model: {GEMINI_MODEL or 'EMPTY'}")
print(f"Groq key: {status(GROQ_API_KEY)}")
print(f"Groq model: {GROQ_MODEL or 'EMPTY'}")
print(f"Cerebras key: {status(CEREBRAS_API_KEY)}")
print(f"Cerebras model: {CEREBRAS_MODEL or 'EMPTY'}")
print(f"Provider order: {AI_PROVIDERS}")
