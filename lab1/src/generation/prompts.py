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

# v2 fixes the failures found when analysing the strict prompt's answers:
# - 3B models drift into the language of the context, so the answer language is stated
#   explicitly at the end of the user message (see build_messages), not only in the rules;
# - they copied article numbers ("[85]") as citations, so the valid range is spelled out;
# - the site contradicts itself (two phone numbers for the dean), so conflicts must be reported;
# - qwen appended the refusal sentence after a correct answer.
STRICT_V2 = """You are an assistant for students of the Technical University of Moldova (UTM) and its
Faculty of Computers, Informatics and Microelectronics (FCIM).

Rules:
1. Use ONLY the numbered context fragments. Do not use your own knowledge about UTM.
2. Answer in 1-4 complete sentences that restate what the number or name refers to;
   keep numbers, dates, names and steps exactly as in the context.
3. After each fact put the number of the fragment it comes from, e.g. [2]. Citation numbers are ONLY
   the fragment numbers given in the context; never use article, paragraph or page numbers as citations.
4. If two fragments give different values for the same fact (e.g. two phone numbers), give both values,
   each with its own citation, and say that the sources differ.
5. Only if the context does not contain the answer, reply with exactly this sentence and nothing else:
   "{refusal}"
   If you can answer, never add this sentence."""

# v3 adds two rules found by asking list questions in the web interface:
# - "programe de doctorat" was answered with the FCIM faculty's list presented as the whole university's;
# - "программы магистратуры" got only part of a list, without saying the list was incomplete.
STRICT_V3 = STRICT_V2 + """
6. Each fragment says whose website it comes from. Fragments from the FCIM faculty website describe only that
   faculty: when you use them, say so (e.g. "at FCIM ..."), never present them as covering the whole university.
7. When asked to list items (programmes, documents, steps), list every item the fragments contain; if the
   fragments show only part of a longer list, say that the full list is in the cited source."""

SITE_LABELS = {"utm": "UTM university website (utm.md)", "fcim": "FCIM faculty website (fcim.utm.md) — FCIM only"}

TRANSLATE = """Translate the following answer into {language}. Keep every number, name, e-mail address and
citation such as [1] exactly as it is. Output only the translation.

{answer}"""

PROMPTS = {"basic": BASIC, "strict": STRICT, "strict_v2": STRICT_V2, "strict_v3": STRICT_V3}

LANGUAGE_NAMES = {"ro": "Romanian", "en": "English", "ru": "Russian"}


def format_context(hits, site_labels: bool = False) -> str:
    parts = []
    for i, h in enumerate(hits, 1):
        m = h.metadata
        header = f"[{i}] {m.get('title', '')}" + (f" — {m['section']}" if m.get("section") else "")
        source = f"Source: {m.get('url', '')}"
        if site_labels and m.get("source") in SITE_LABELS:
            source += f"\nWebsite: {SITE_LABELS[m['source']]}"
        parts.append(f"{header}\n{source}\n{h.text}")
    return "\n\n".join(parts)


def build_messages(prompt_name: str, question: str, hits, max_context_chars: int,
                   answer_lang: str = "en") -> tuple[list[dict], list]:
    """Chat messages plus the hits that actually fit into the context budget."""
    context, used = [], 0
    for h in hits:  # hits are already sorted by relevance: cut from the tail
        if used + len(h.text) > max_context_chars and context:
            break
        context.append(h)
        used += len(h.text)
    user = f"Context:\n{format_context(context, site_labels=prompt_name == 'strict_v3')}\n\nQuestion: {question}"
    system = PROMPTS[prompt_name]
    if prompt_name in ("strict_v2", "strict_v3"):
        system = system.format(refusal=REFUSALS[answer_lang])
        # the last line before generation is what a small model follows most reliably, so it carries
        # the answer language (putting the refusal sentence there made the model refuse too often)
        user += f"\n\nCite only fragments [1]-[{len(context)}]. Answer in {LANGUAGE_NAMES[answer_lang]}."
    return [{"role": "system", "content": system}, {"role": "user", "content": user}], context
