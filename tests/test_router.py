from router.ai_router import ask


messages = [
    {
        "role": "user",
        "content": "Ответь одним словом: ПРИВЕТ",
    }
]

result = ask(messages)

print("Router: OK")
print("Provider:", result["provider"])
print("Answer:", result["answer"])

if result["errors"]:
    print("Previous provider errors:")

    for error in result["errors"]:
        print(
            "-",
            error["provider"],
            ":",
            error["error"][:300],
        )
