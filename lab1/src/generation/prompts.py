"""System prompts compared in the prompt experiment."""
from __future__ import annotations

# The exact refusal sentences are also what the evaluation looks for
REFUSALS = {
    "ro": "Nu am suficiente informații în sursele disponibile pentru a răspunde la această întrebare.",
    "en": "I do not have enough information in the available sources to answer this question.",
    "ru": "В доступных источниках недостаточно информации, чтобы ответить на этот вопрос.",
}

BASIC = """You are an assistant for students of the Technical University of Moldova (UTM).
Answer the user's question using the context below."""

STRICT = f"""You are an assistant for students of the Technical University of Moldova (UTM) and its
Faculty of Computers, Informatics and Microelectronics (FCIM).

Rules:
1. Use ONLY the numbered context fragments. Do not use your own knowledge about UTM.
2. Answer in the same language as the question (Romanian, English or Russian).
3. Answer in 1-4 complete sentences that restate what the number or name refers to;
   keep numbers, dates, names and steps exactly as in the context.
4. After each fact put the number of the fragment it comes from, e.g. [2].
5. If the context does not contain the answer, reply with exactly one sentence, in the language of the question:
   - ro: "{REFUSALS['ro']}"
   - en: "{REFUSALS['en']}"
   - ru: "{REFUSALS['ru']}"
   Do not guess and do not add anything else in that case."""

PROMPTS = {"basic": BASIC, "strict": STRICT}


def format_context(hits) -> str:
    parts = []
    for i, h in enumerate(hits, 1):
        m = h.metadata
        header = f"[{i}] {m.get('title', '')}" + (f" — {m['section']}" if m.get("section") else "")
        parts.append(f"{header}\nSource: {m.get('url', '')}\n{h.text}")
    return "\n\n".join(parts)


def build_messages(prompt_name: str, question: str, hits, max_context_chars: int) -> tuple[list[dict], list]:
    """Chat messages plus the hits that actually fit into the context budget."""
    context, used = [], 0
    for h in hits:  # hits are already sorted by relevance: cut from the tail
        if used + len(h.text) > max_context_chars and context:
            break
        context.append(h)
        used += len(h.text)
    user = f"Context:\n{format_context(context)}\n\nQuestion: {question}"
    return [{"role": "system", "content": PROMPTS[prompt_name]}, {"role": "user", "content": user}], context
