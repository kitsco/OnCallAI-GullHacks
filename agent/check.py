"""Quick check that the agent can reach Gemini and AWS. Run on the server from the repo root:

    python3.11 -m agent.check
"""
from agent.config import GEMINI_API_KEY, GEMINI_MODEL


def main():
    print(f"Model: {GEMINI_MODEL}")
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY is missing from .env")
        return
    print(f"Key: set ({len(GEMINI_API_KEY)} characters)")
    from google import genai

    client = genai.Client(api_key=GEMINI_API_KEY)
    try:
        reply = client.models.generate_content(model=GEMINI_MODEL, contents="Reply with exactly: OnCall AI ready")
        print("Gemini says:", (reply.text or "").strip())
    except Exception as exc:
        print("Gemini call failed:", repr(exc)[:500])
        print("Models your key can use (look for a 'flash' one and set GEMINI_MODEL=... in .env):")
        try:
            for m in client.models.list():
                name = getattr(m, "name", "")
                if "flash" in name or "pro" in name:
                    print("  ", name.replace("models/", ""))
        except Exception as exc2:
            print("  could not list models:", repr(exc2)[:300])

    from agent import evidence
    print("AWS check:", evidence.summarize_metric("payments-api", "LatencyP95")["text"])
    print("Alarms firing:", evidence.firing_alarms() or "none")


if __name__ == "__main__":
    main()
