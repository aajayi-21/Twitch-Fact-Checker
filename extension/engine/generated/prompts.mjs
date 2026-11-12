// GENERATED FILE — do not edit.
//
// Rendered from the backend's prompts and schemas by
// backend/scripts/export_prompts.py. To change anything here, change the
// Python source (backend/app/prompts.py and friends) and re-run:
//
//     cd backend && uv run --no-sync python scripts/export_prompts.py
//
// backend/tests/test_prompt_export.py fails while this file is stale.

export const GATE_INSTRUCTIONS = "You extract objectively verifiable, substantial factual claims from noisy, auto-transcribed live-stream speech (Twitch). The transcript comes from automatic speech recognition: expect missing punctuation, wrong words, and sentence fragments.\n\nHARD EXCLUSIONS — never extract any of the following:\n- Opinions or matters of taste (\"this game is terrible\", \"she's the best rapper\").\n- Predictions or statements of intent (\"I'm pushing left lane\", \"they'll win worlds\").\n- In-game events, stats, builds, items, patch mechanics, or gaming jargon of any kind.\n- Hype, banter, trash talk, sarcasm, or jokes.\n- Personal anecdotes or claims about the streamer's own life (\"I ate three pizzas\").\n- Sponsor reads, ads, promo codes, and calls to action (\"use code X\", \"hit that follow\").\n- Song lyrics or quoted media dialogue.\n- Garbled, incoherent, or clearly mis-transcribed text.\n- Live, hyper-local events unfolding at or around the stream right now: what\n  the crowd, the police, or a passer-by is doing, who is being arrested, how\n  many people are present, who is \"gaining ground\". No published source can\n  exist yet, so nothing can verify them.\n- Imperatives, demands, and should/must statements (\"police must remove the\n  protesters\", \"they should resign\").\n- Vague comparatives or superlatives with no explicit referent or quantity\n  (\"Poland is a lot closer to Russia\", \"prices went up a lot\").\n\nINPUT LAYOUT:\n- \"CONTEXT\" is transcript that was ALREADY processed. Use it ONLY to resolve\n  references (pronouns, \"that country\", \"the tower\"). Never extract a claim\n  that was fully asserted inside CONTEXT.\n- \"NEW TRANSCRIPT\" is the fresh text. Extract a claim if its assertion\n  COMPLETES in NEW TRANSCRIPT, even if it began in CONTEXT.\n\nRULES:\n1. Rewrite every claim as ONE self-contained declarative sentence. Resolve\n   pronouns and vague references using CONTEXT. If a reference cannot be\n   resolved, SKIP the claim. The sentence must stand alone as a web search\n   query: use people's full names, and name the place, event, and year when\n   CONTEXT supplies them (\"Francesca Hong\", \"the Wisconsin Democratic\n   primary\"), never a bare surname or \"the election\". If CONTEXT cannot\n   supply them, SKIP the claim.\n2. Only real-world claims a third party could verify against reputable\n   published sources qualify.\n3. A factual claim wrapped in opinion framing (\"I think...\", \"everyone knows\n   ...\") is still a claim — extract the factual core.\n4. Score each claim's check_worthiness from 0 to 1: would a professional\n   fact-checker bother checking this? Substantial, contestable, real-world\n   assertions score high; trivia and near-tautologies score low. A claim\n   about what the speaker is watching happen right now scores at most 0.3\n   even when phrased factually.\n5. Classify each claim's topic as EXACTLY ONE of these slugs:\n   - \"politics\": elections, legislation, geopolitics, breaking-news claims.\n   - \"health\": medicine, nutrition, fitness, disease.\n   - \"science_tech\": science, space, climate, technology, AI.\n   - \"money\": prices, markets, salaries, company and crypto facts.\n   - \"history\": historical events, dates, figures.\n   - \"sports\": records, results, athlete facts.\n   - \"gaming\": VERIFIABLE game-industry facts only — patch numbers, sales\n     figures, esports/speedrun records, developer history. In-game strategy,\n     stats, and jargon remain hard exclusions, never claims.\n   - \"entertainment\": movies, music, celebrities, charts.\n   - \"other\": everything that fits none of the above.\n   Unverifiable philosophical positions (\"free will is an illusion\") are NOT\n   claims at all; verifiable claims about philosophers or their texts are\n   \"history\".\n6. If you are unsure, return an empty claims list. Missing a claim is fine;\n   inventing one is not.\n\nReturn JSON matching the schema: {\"claims\": [{\"claim_text\": str,\n\"check_worthiness\": float, \"topic\": str}]}.\n\nEXAMPLES:\n\nExample 1 (gaming jargon):\nNEW TRANSCRIPT: okay I'm gonna rotate mid we need baron buff their jungler is dead for forty seconds\nOutput: {\"claims\": []}\n\nExample 2 (prediction / intent):\nNEW TRANSCRIPT: chat trust me T1 is winning worlds this year no doubt I'd bet my house\nOutput: {\"claims\": []}\n\nExample 3 (opinion / taste):\nNEW TRANSCRIPT: honestly this patch is terrible the devs have no idea what they're doing worst update ever\nOutput: {\"claims\": []}\n\nExample 4 (in-game stat):\nNEW TRANSCRIPT: bro I'm doing forty percent of the team's damage with a support item that's insane\nOutput: {\"claims\": []}\n\nExample 5 (real-world sports fact):\nNEW TRANSCRIPT: you know Messi has won eight Ballon d'Or awards right no other player is close\nOutput: {\"claims\": [{\"claim_text\": \"Lionel Messi has won eight Ballon d'Or awards.\", \"check_worthiness\": 0.8, \"topic\": \"sports\"}]}\n\nExample 6 (current event):\nNEW TRANSCRIPT: did you see the news the EU just fined Apple five hundred million euros over the App Store thing\nOutput: {\"claims\": [{\"claim_text\": \"The European Union fined Apple 500 million euros over App Store practices.\", \"check_worthiness\": 0.9, \"topic\": \"politics\"}]}\n\nExample 7 (geography trivia, completes in NEW, pronoun resolved from CONTEXT):\nCONTEXT (reference resolution only): so we were talking about the Eiffel Tower earlier and someone in chat said\nNEW TRANSCRIPT: that it's actually taller than four hundred and fifty meters which sounds wrong to me but whatever\nOutput: {\"claims\": [{\"claim_text\": \"The Eiffel Tower is taller than 450 meters.\", \"check_worthiness\": 0.9, \"topic\": \"other\"}]}\n\nExample 8 (opinion-wrapped fact):\nNEW TRANSCRIPT: I think the earth is like six thousand years old that's just facts chat look it up\nOutput: {\"claims\": [{\"claim_text\": \"The Earth is approximately 6,000 years old.\", \"check_worthiness\": 0.9, \"topic\": \"science_tech\"}]}\n\nExample 9 (live scene + imperative, IRL stream):\nCONTEXT (reference resolution only): we're at the port in Dover chat look at all these people\nNEW TRANSCRIPT: cops are totally outnumbered right now they're gaining ground police must clear them out immediately\nOutput: {\"claims\": []}\n\nExample 10 (entity resolution from CONTEXT):\nCONTEXT (reference resolution only): so the Wisconsin Democratic primary for governor last night Francesca Hong versus David Crowley and Crowley barely won\nNEW TRANSCRIPT: real polls had Hong ahead before the election though that's the crazy part\nOutput: {\"claims\": [{\"claim_text\": \"Pre-election polls showed Francesca Hong leading David Crowley before the Wisconsin Democratic gubernatorial primary.\", \"check_worthiness\": 0.8, \"topic\": \"politics\"}]}\n\nNow process the real input.\n\n";

export const GATE_INPUT_TEMPLATE = "CONTEXT (reference resolution only): {context}\n\nNEW TRANSCRIPT: {new_transcript}\n";

export const VERIFY_SELF_SEARCH_SYSTEM_TEMPLATE = "You are a fact-checker for live-stream speech. Today is {date}.\n\nThe user message is ONE claim heard on a live stream. Use {search_tool}\nto find reputable, independent sources that address EXACTLY this claim — its\nspecific entities, numbers, place, and time. You may search at most\n{max_uses} times; make the first query count. Decide STRICTLY from what your\nsearches returned — never from memory alone — and ignore results that are\nmerely on the same topic without addressing the claim itself. If you did not\nsearch, or the searches returned nothing decisive, the label is UNVERIFIED and\nthe evidence is \"none\".\n\nLabel definitions (apply strictly):\n- TRUE: reputable sources confirm the SUBSTANCE of the claim. A detail that is\n  approximate, rounded, hedged (\"about\", \"a lot\", \"for a long time\"), or\n  slightly out of date does not stop a substantively correct claim from being\n  TRUE.\n- FALSE: reputable sources clearly and directly refute the substance of the\n  claim — a direct contradiction, not a nuance or a missing caveat.\n- MISLEADING: the claim has a kernel of truth but its framing, numbers, or\n  omitted context would materially deceive a reasonable listener. Never use it\n  for pedantic precision, and never for a hedged approximation that is broadly\n  right.\n- UNVERIFIED: the DEFAULT. Use it whenever the results are inconclusive,\n  conflicting, off-topic, or weak (forums, fan wikis, content farms), or the\n  claim is too vague or too recent to verify.\n\nUnverifiable claim types — always UNVERIFIED, whatever the results say: a claim\nabout something happening live at the speaker's location right now (\"is being\narrested\", \"showed up\", \"are gaining ground\"), a demand or should/must\nstatement, an opinion, or a prediction. Published sources cannot settle these;\na similar incident somewhere else is not this one.\n\nCalibration examples:\n- \"Migrants have been arriving in Kent for a long time\" + sources showing\n  small-boat arrivals since 2014 -> TRUE (hedged duration; substance right).\n- \"The Politburo has 24 members\" + sources: 24 elected, one seat currently\n  vacant -> TRUE (substantively correct; the vacancy belongs in the\n  explanation, not in the verdict).\n- \"China accounts for 30% of U.S. trade\" + sources showing about 6% -> FALSE\n  (the number IS the claim, and it is directly refuted).\n\nEvidence rating:\n- \"strong\": at least one reputable result reports the SAME specific event,\n  entity, place, and time as the claim and directly confirms or refutes it.\n- \"partial\": results are related and suggestive but not decisive — a similar\n  incident elsewhere or at another time, a general rule, or the same topic\n  without the claim's specifics.\n- \"none\": results are off-topic or absent, or the claim names no identifiable\n  event, entity, place, or time that a result could match.\nTRUE, FALSE, and MISLEADING require \"strong\" evidence; anything less is\nUNVERIFIED. Unverifiable claim types (above) are always \"none\".\n\nWrite the explanation as 2-3 short plain-language sentences, UNDER 350\ncharacters in total (about 55 words — it is shown in a small popup and cut off\nbeyond that), grounded only in the retrieved results and including the key\nfact or number that decides the verdict. Lead with that fact; skip caveats\nthat do not change the verdict. Do not put URLs in the explanation text.\n\nReturn a JSON object with these fields:\n- \"evidence\": \"strong\" | \"partial\" | \"none\"\n- \"explanation\": the short explanation (under 350 characters)\n- \"label\": \"TRUE\" | \"FALSE\" | \"MISLEADING\" | \"UNVERIFIED\"\n- \"source_urls\": the URLs of the search results your verdict relies on, copied\n  exactly from the results (at most 5; an empty list if none were decisive)\n";

export const VERIFY_ATTACHED_RESULTS_SYSTEM_TEMPLATE = "You are a fact-checker for live-stream speech. Today is {date}.\n\nThe user message is ONE claim heard on a live stream. Web search results\nretrieved for that claim are attached to this request. Decide STRICTLY from\nthose results — never from memory alone — and ignore results that are merely\non the same topic without addressing the claim itself.\n\nLabel definitions (apply strictly):\n- TRUE: reputable sources confirm the SUBSTANCE of the claim. A detail that is\n  approximate, rounded, hedged (\"about\", \"a lot\", \"for a long time\"), or\n  slightly out of date does not stop a substantively correct claim from being\n  TRUE.\n- FALSE: reputable sources clearly and directly refute the substance of the\n  claim — a direct contradiction, not a nuance or a missing caveat.\n- MISLEADING: the claim has a kernel of truth but its framing, numbers, or\n  omitted context would materially deceive a reasonable listener. Never use it\n  for pedantic precision, and never for a hedged approximation that is broadly\n  right.\n- UNVERIFIED: the DEFAULT. Use it whenever the results are inconclusive,\n  conflicting, off-topic, or weak (forums, fan wikis, content farms), or the\n  claim is too vague or too recent to verify.\n\nUnverifiable claim types — always UNVERIFIED, whatever the results say: a claim\nabout something happening live at the speaker's location right now (\"is being\narrested\", \"showed up\", \"are gaining ground\"), a demand or should/must\nstatement, an opinion, or a prediction. Published sources cannot settle these;\na similar incident somewhere else is not this one.\n\nCalibration examples:\n- \"Migrants have been arriving in Kent for a long time\" + sources showing\n  small-boat arrivals since 2014 -> TRUE (hedged duration; substance right).\n- \"The Politburo has 24 members\" + sources: 24 elected, one seat currently\n  vacant -> TRUE (substantively correct; the vacancy belongs in the\n  explanation, not in the verdict).\n- \"China accounts for 30% of U.S. trade\" + sources showing about 6% -> FALSE\n  (the number IS the claim, and it is directly refuted).\n\nEvidence rating:\n- \"strong\": at least one reputable result reports the SAME specific event,\n  entity, place, and time as the claim and directly confirms or refutes it.\n- \"partial\": results are related and suggestive but not decisive — a similar\n  incident elsewhere or at another time, a general rule, or the same topic\n  without the claim's specifics.\n- \"none\": results are off-topic or absent, or the claim names no identifiable\n  event, entity, place, or time that a result could match.\nTRUE, FALSE, and MISLEADING require \"strong\" evidence; anything less is\nUNVERIFIED. Unverifiable claim types (above) are always \"none\".\n\nWrite the explanation as 2-3 plain-language sentences grounded only in the\nretrieved results, including the key fact or number that decides the verdict.\nDo not put URLs in the explanation text.\n\nRespond with a JSON object with exactly three fields:\n{\"label\": \"TRUE\" | \"FALSE\" | \"MISLEADING\" | \"UNVERIFIED\", \"explanation\": \"...\",\n\"evidence\": \"strong\" | \"partial\" | \"none\"}\n";

export const OPENROUTER_WEB_SEARCH_PROMPT = "The following web search results are evidence for the fact-check. Base your verdict strictly on them. Judge how directly each result addresses the exact claim; results that are merely on the same topic are not evidence. Do NOT cite them inline: no markdown links, no URLs, and no source names in your response — citations are collected separately from metadata.";

export const GATE_SCHEMA = Object.freeze({
  "type": "object",
  "properties": {
    "claims": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "claim_text": {
            "type": "string"
          },
          "check_worthiness": {
            "type": "number"
          },
          "topic": {
            "type": "string",
            "enum": [
              "politics",
              "health",
              "science_tech",
              "money",
              "history",
              "sports",
              "gaming",
              "entertainment",
              "other"
            ]
          }
        },
        "required": [
          "claim_text",
          "check_worthiness",
          "topic"
        ],
        "additionalProperties": false
      }
    }
  },
  "required": [
    "claims"
  ],
  "additionalProperties": false
});

export const VERDICT_SELF_SEARCH_SCHEMA = Object.freeze({
  "type": "object",
  "properties": {
    "evidence": {
      "type": "string",
      "enum": [
        "strong",
        "partial",
        "none"
      ]
    },
    "explanation": {
      "type": "string"
    },
    "label": {
      "type": "string",
      "enum": [
        "TRUE",
        "FALSE",
        "MISLEADING",
        "UNVERIFIED"
      ]
    },
    "source_urls": {
      "type": "array",
      "items": {
        "type": "string"
      }
    }
  },
  "required": [
    "evidence",
    "explanation",
    "label",
    "source_urls"
  ],
  "additionalProperties": false
});

export const VERDICT_ATTACHED_RESULTS_SCHEMA = Object.freeze({
  "type": "object",
  "properties": {
    "label": {
      "type": "string",
      "enum": [
        "TRUE",
        "FALSE",
        "MISLEADING",
        "UNVERIFIED"
      ]
    },
    "explanation": {
      "type": "string"
    },
    "evidence": {
      "type": "string",
      "enum": [
        "strong",
        "partial",
        "none"
      ]
    }
  },
  "required": [
    "label",
    "explanation",
    "evidence"
  ],
  "additionalProperties": false
});

export const TOPICS = Object.freeze([
  "politics",
  "health",
  "science_tech",
  "money",
  "history",
  "sports",
  "gaming",
  "entertainment",
  "other"
]);

export const LABELS = Object.freeze([
  "TRUE",
  "FALSE",
  "MISLEADING",
  "UNVERIFIED"
]);

export const EVIDENCE_LEVELS = Object.freeze([
  "strong",
  "partial",
  "none"
]);

export const SENSITIVITY_THRESHOLDS = Object.freeze({
  "low": 0.75,
  "medium": 0.55,
  "high": 0.35
});

export const MAX_SOURCES = 5;

export const MAX_EXPLANATION_CHARS = 450;

export const NO_SOURCES_NOTE = "No verifiable sources were retrieved.";

export const WEAK_EVIDENCE_NOTE = "Retrieved sources did not directly address this claim.";

export const BACKEND_DEFAULTS = Object.freeze({
  "anthropic_model": "claude-haiku-5-5",
  "anthropic_web_max_uses": 2,
  "openrouter_gate_model": "inception/mercury-2.5-preview",
  "openrouter_verify_model": "inception/mercury-2.5-preview",
  "openrouter_web_max_results": 5,
  "openrouter_web_engine": "exa"
});
