/**
 * rubric-config.ts
 *
 * ============================================================================
 * FINAL STUDY VALUES. Every value in this module is now the finalized output of
 * the content_quality study, NOT a placeholder. It is aligned to:
 *   - configs/config.yaml            judge models, rubric, scale, threshold
 *   - configs/prices.yaml            per-model input/output prices
 *   - src/rubric.py                  the seven dimensions, help, and anchors
 *   - prompts/judge/<model>/<v>.txt  each model's selected best prompt version
 *   - outputs/best_prompt_versions.json  which version won per model
 *   - outputs/dimension_reduction.json    refined dimension set and fitted weights
 *   - outputs/composite_results.json      the composite is the "fitted" type
 *
 * What is final here:
 *   - MODELS: exactly the four judges the study ran (Poe bot names).
 *   - MODEL_SYSTEM_PROMPTS: each model's selected best prompt version, embedded
 *     verbatim (gpt-5.2 v1, claude-haiku-4.5 v3, gemini-2.5-flash v3,
 *     perplexity-sonar v2), reconstructed with the study's own prompt renderer.
 *   - COMPOSITE_WEIGHTS / COMPOSITE_INTERCEPT: the study's FITTED weights over
 *     the REFINED dimension set {neutrality, verifiability, coverage,
 *     readability}. Dropped dimensions carry weight 0.
 *   - GOOD_THRESHOLD: analysis.good_threshold (6.0), applied to the DIRECT
 *     overall score (that is the value the study thresholds for its good/bad
 *     precision, recall, and F1). See DECISION_FIELD below.
 *   - PRICE_TABLE: the four models' verified input/output prices.
 * ============================================================================
 *
 * This module is the ONLY place the live demo encodes study knowledge. The
 * Worker (worker.ts) reads everything from here so that re-pointing the demo at
 * a different study revision means editing this one file.
 */

// The four judge models the live demo exposes. Exactly the study's judge.models
// list in configs/config.yaml, using the Poe bot names the study called.
export const MODELS = [
  "gpt-5.2",
  "claude-haiku-4.5",
  "gemini-2.5-flash",
  "perplexity-sonar",
] as const;

export type ModelName = (typeof MODELS)[number];

// Rubric scale. Seeded from configs/config.yaml (rubric.scale_min / scale_max).
export const SCALE_MIN = 1;
export const SCALE_MAX = 10;

// The seven candidate writing-quality dimensions, seeded from src/rubric.py.
// "overall" is collected as a direct holistic judgment, NOT an average of these.
export const DIMENSIONS = [
  "clarity",
  "neutrality",
  "verifiability",
  "coverage",
  "structure",
  "readability",
  "informativeness",
] as const;

export type Dimension = (typeof DIMENSIONS)[number];

export const ALL_FIELDS: readonly string[] = [...DIMENSIONS, "overall"];

// Short per-dimension descriptions, copied from src/rubric.py DIMENSION_HELP.
export const DIMENSION_HELP: Record<Dimension, string> = {
  clarity: "Is the passage clear and easy to understand on a first read?",
  neutrality:
    "Is the tone neutral and impartial, free of bias, promotion, or editorializing?",
  verifiability:
    "Do the claims appear sourced or attributable rather than unsupported assertions?",
  coverage:
    "Does the passage cover its topic adequately for its scope, without obvious gaps?",
  structure:
    "Is it well organized and coherent, with ideas that flow logically?",
  readability:
    "Is the language fluent, grammatical, and in an appropriate encyclopedic register?",
  informativeness:
    "Does it convey substantive, useful information efficiently rather than padding or triviality?",
};

// Per-dimension endpoint anchors, copied from src/rubric.py DIMENSION_ENDPOINTS.
export const DIMENSION_ENDPOINTS: Record<Dimension, string> = {
  clarity:
    "1 = confusing or impenetrable, 10 = immediately clear and easy to follow.",
  neutrality:
    "1 = heavily biased or promotional, 10 = strictly neutral and impartial.",
  verifiability:
    "1 = unsupported or unverifiable claims, 10 = claims clearly attributable to sources or concrete evidence.",
  coverage:
    "1 = superficial or fragmentary, 10 = thorough and well rounded for its scope.",
  structure: "1 = disjointed or incoherent, 10 = tightly organized and coherent.",
  readability:
    "1 = clumsy, ungrammatical, or awkward prose, 10 = fluent, polished, encyclopedic prose.",
  informativeness:
    "1 = vacuous or trivial, 10 = dense with relevant, useful information.",
};

/**
 * COMPOSITE: FINAL. The study's composite is the "fitted" type
 * (outputs/composite_results.json) over the REFINED dimension set
 * {neutrality, verifiability, coverage, readability}
 * (outputs/dimension_reduction.json -> refined_rubric). The other three
 * dimensions (clarity, structure, informativeness) were dropped and carry
 * weight 0 here. The composite is a linear regression fit against the human
 * ground-truth overall:
 *
 *   composite = COMPOSITE_INTERCEPT + sum_d ( COMPOSITE_WEIGHTS[d] * score[d] )
 *
 * where the sum runs over COMPOSITE_DIMENSIONS. It is NOT a normalized weighted
 * mean: the intercept and the un-normalized weights are exactly the study's
 * fitted coefficients, so this reproduces the study's refined fitted composite.
 * The result is clamped to the SCALE_MIN..SCALE_MAX scale for display.
 */
export const COMPOSITE_DIMENSIONS: readonly Dimension[] = [
  "neutrality",
  "verifiability",
  "coverage",
  "readability",
];

// Dimensions dropped from the refined set. Kept for documentation only.
export const COMPOSITE_DROPPED_DIMENSIONS: readonly Dimension[] = [
  "clarity",
  "structure",
  "informativeness",
];

// refined_rubric.fitted_weights from outputs/dimension_reduction.json. Dropped
// dimensions are 0 so they never enter the composite.
export const COMPOSITE_WEIGHTS: Record<Dimension, number> = {
  clarity: 0,
  neutrality: 0.0495,
  verifiability: 0.277,
  coverage: 0.4139,
  structure: 0,
  readability: 0.3138,
  informativeness: 0,
};

// refined_rubric.fitted_intercept from outputs/dimension_reduction.json.
export const COMPOSITE_INTERCEPT = -0.106;

/**
 * Refined fitted composite. Reproduces the study's refined_rubric fitted
 * prediction, then clamps to the rubric scale for presentation.
 */
export function compositeScore(scores: Record<string, number>): number {
  let v = COMPOSITE_INTERCEPT;
  for (const d of COMPOSITE_DIMENSIONS) {
    v += COMPOSITE_WEIGHTS[d] * scores[d];
  }
  return Math.max(SCALE_MIN, Math.min(SCALE_MAX, v));
}

/**
 * GOOD_THRESHOLD: FINAL. analysis.good_threshold (6.0) from configs/config.yaml.
 * A passage is labeled "good" when the value named by DECISION_FIELD is at or
 * above this threshold, on the SCALE_MIN..SCALE_MAX scale.
 */
export const GOOD_THRESHOLD = 6.0;

/**
 * DECISION_FIELD: FINAL. Which score drives the good/bad decision. The study
 * thresholds the DIRECT overall score for its secondary good/bad metric
 * (precision, recall, F1 in src/metrics.py prf1, where the judge value is the
 * overall column). So the demo's decision is driven by the direct overall
 * score, not the composite. The composite is still returned alongside it for
 * transparency.
 */
export const DECISION_FIELD = "overall" as const;

// Judge generation settings, seeded from configs/config.yaml api.* block.
export const TEMPERATURE = 0;
export const MAX_OUTPUT_TOKENS = 2000;

// The cheap model used for the JSON-repair fallback, seeded from
// configs/config.yaml repair.model.
export const REPAIR_MODEL = "claude-haiku-4.5";

/**
 * MODEL_SYSTEM_PROMPTS: FINAL per-model best judge prompt. Each model uses the
 * winning prompt version recorded in outputs/best_prompt_versions.json:
 *   gpt-5.2           v1   (the minimal base prompt, no corrections)
 *   claude-haiku-4.5  v3   (base + "too_generous" + "substance")
 *   gemini-2.5-flash  v3   (base + "length_under" + "too_generous")
 *   perplexity-sonar  v2   (base + "length_under")
 *
 * The study built each version by appending diagnosis-driven corrective lines
 * to a shared base prompt (src/prompt_tuning.py render_prompt). The exact same
 * base text, corrective library, and renderer are reproduced below so the demo
 * sends byte-for-byte what the study sent for each model's best version, plus
 * the rubric and anchors from buildUserPrompt.
 */
const JUDGE_BASE =
  "You are evaluating the writing quality of an encyclopedic passage (a paragraph\n" +
  "from Wikipedia).\n\n" +
  "Read the passage, then rate it on each rubric dimension and give an overall\n" +
  "score. Return your scores as JSON.";

// The corrective guidance library, copied verbatim from src/prompt_tuning.py
// CORRECTIONS. Only the keys that appear in the winning versions are needed.
const JUDGE_CORRECTIONS: Record<string, string> = {
  length_under:
    "Do not penalize a thorough passage for its length when the extra detail " +
    "is relevant and genuinely informative to the reader.",
  too_generous:
    "You have been scoring too generously. Be more critical and reserve scores " +
    "of 8 or higher for genuinely excellent passages.",
  substance:
    "Judge substance before style. First decide whether the passage is accurate, " +
    "neutral, and informative, and let that dominate the overall score. Treat " +
    "surface polish as secondary.",
};

// Reproduces src/prompt_tuning.py render_prompt. With no corrections it returns
// the base prompt exactly as v1.txt (which ends in a trailing newline).
function renderJudgePrompt(applied: readonly string[]): string {
  if (applied.length === 0) return JUDGE_BASE + "\n";
  const lines = [
    JUDGE_BASE,
    "",
    "Additional calibration guidance learned from reviewer disagreement:",
  ];
  for (const key of applied) lines.push(`- ${JUDGE_CORRECTIONS[key]}`);
  lines.push("");
  lines.push("Return only the requested JSON object.");
  return lines.join("\n");
}

// The selected best prompt version per model (from best_prompt_versions.json).
export const MODEL_BEST_VERSION: Record<ModelName, string> = {
  "gpt-5.2": "v1",
  "claude-haiku-4.5": "v3",
  "gemini-2.5-flash": "v3",
  "perplexity-sonar": "v2",
};

export const MODEL_SYSTEM_PROMPTS: Record<ModelName, string> = {
  "gpt-5.2": renderJudgePrompt([]),
  "claude-haiku-4.5": renderJudgePrompt(["too_generous", "substance"]),
  "gemini-2.5-flash": renderJudgePrompt(["length_under", "too_generous"]),
  "perplexity-sonar": renderJudgePrompt(["length_under"]),
};

// ---------------------------------------------------------------------------
// Prompt construction. Mirrors src/rubric.py build_user_prompt so the live
// demo asks each judge for the same JSON shape the study uses.
// ---------------------------------------------------------------------------

function rubricBlock(): string {
  const lines = ["Score these dimensions of the passage plus an overall score:"];
  for (const d of DIMENSIONS) {
    lines.push(`  - ${d}: ${DIMENSION_HELP[d]}`);
  }
  lines.push(
    "  - overall: your holistic judgment of the passage's writing quality, " +
      "decided directly and NOT as the average of the dimensions above.",
  );
  return lines.join("\n");
}

function anchorsBlock(): string {
  const lines = [
    `Every score is an integer from ${SCALE_MIN} to ${SCALE_MAX} on this scale:`,
    `  ${SCALE_MIN} = unusable: incoherent, empty, or clearly not encyclopedic writing.`,
    "  4 = poor to fair: readable but with real problems in clarity, neutrality, sourcing, or coverage.",
    "  7 = good: clear, neutral, and informative encyclopedic writing with only minor shortcomings.",
    `  ${SCALE_MAX} = exceptional: clear, neutral, well sourced, comprehensive, and polished.`,
    "Per-dimension meaning of the endpoints:",
  ];
  for (const d of DIMENSIONS) {
    lines.push(`  - ${d}: ${DIMENSION_ENDPOINTS[d]}`);
  }
  return lines.join("\n");
}

export function jsonInstruction(): string {
  const example: Record<string, unknown> = {};
  for (const d of DIMENSIONS) {
    example[d] = { reason: "<short reason>", score: "<integer 1 to 10>" };
  }
  example.overall = { reason: "<short reason>", score: "<integer 1 to 10>" };
  const shape = JSON.stringify(example, null, 2);
  return (
    "Respond with a single JSON object and nothing else. For every dimension " +
    'and for overall, write a brief reason FIRST and then the integer score, in ' +
    'that order, as an object with keys "reason" then "score". Use this exact ' +
    "shape:\n" +
    shape
  );
}

export function buildUserPrompt(passage: string): string {
  return (
    `PASSAGE TO EVALUATE:\n${passage}` +
    "\n\n" +
    rubricBlock() +
    "\n\n" +
    anchorsBlock() +
    "\n\n" +
    jsonInstruction()
  );
}

export function buildSystemPrompt(model: ModelName): string {
  return MODEL_SYSTEM_PROMPTS[model] ?? renderJudgePrompt([]);
}

// System prompt for the JSON-repair fallback call. Copied from src/repair.py.
export const REPAIR_SYSTEM =
  "You convert another model's reply into a single valid JSON object that " +
  "matches a required schema. Output only the JSON object and nothing else. " +
  "Preserve the original scores and reasons wherever they appear in the reply. " +
  "If a required field is genuinely missing, infer the most reasonable value " +
  "from the text. Every score must be an integer from 1 to 10.";

// ---------------------------------------------------------------------------
// Price table and cost cap. FINAL, mirrored from configs/prices.yaml (the study
// source of truth). Units are US dollars per 1,000,000 tokens.
//
// IMPORTANT: Poe bills API usage in its own compute points, not in per-token
// dollars. The values below are the study's documented public list-price
// approximations, used only for bounding demo spend and for the cost estimate.
//
// From configs/prices.yaml (last reviewed 2026-07):
//   gpt-5.2            1.25 / 10.00
//   claude-haiku-4.5   1.00 /  5.00   (also the JSON-repair model)
//   gemini-2.5-flash   0.30 /  2.50
//   perplexity-sonar   1.00 /  1.00
// The study's default price (1.00 / 3.00) is the fallback for any other model.
// ---------------------------------------------------------------------------
export interface Price {
  input: number; // dollars per 1,000,000 input tokens
  output: number; // dollars per 1,000,000 output tokens
}

export const DEFAULT_PRICE: Price = { input: 1.0, output: 3.0 };

export const PRICE_TABLE: Record<string, Price> = {
  "gpt-5.2": { input: 1.25, output: 10.0 },
  // claude-haiku-4.5 is both a judge and the cheap JSON-repair model.
  "claude-haiku-4.5": { input: 1.0, output: 5.0 },
  "gemini-2.5-flash": { input: 0.3, output: 2.5 },
  "perplexity-sonar": { input: 1.0, output: 1.0 },
};

export function priceFor(model: string): Price {
  return PRICE_TABLE[model] ?? DEFAULT_PRICE;
}

/**
 * Compute the dollar cost of one call:
 *   cost = input_tokens * input_price + output_tokens * output_price
 * with prices expressed per 1,000,000 tokens.
 */
export function costUsd(model: string, inputTokens: number, outputTokens: number): number {
  const p = priceFor(model);
  return (inputTokens / 1_000_000) * p.input + (outputTokens / 1_000_000) * p.output;
}

// Rough characters-per-token ratio, copied from src/tokens.py, used only when
// the Poe response does not carry a usage block.
export const CHARS_PER_TOKEN = 4.0;

export function estimateTokens(text: string): number {
  if (!text) return 0;
  return Math.max(1, Math.round(text.length / CHARS_PER_TOKEN));
}

// ---------------------------------------------------------------------------
// Limits. All are overridable per deployment via wrangler vars (see worker.ts
// readLimits). These defaults are the safety net.
// ---------------------------------------------------------------------------
export const LIMIT_DEFAULTS = {
  // Max characters of pasted content accepted by /evaluate. Study passages are
  // capped at 2000 chars (dataset.max_chars) so this leaves generous headroom.
  MAX_INPUT_CHARS: 8000,
  // Per-access-token daily quota of /evaluate calls.
  PER_TOKEN_DAILY_QUOTA: 10,
  // Hard global daily cap on total judge calls across ALL tokens. The ultimate
  // backstop so total spend is bounded no matter what.
  GLOBAL_DAILY_CALL_CAP: 200,
  // Per-IP daily cap on /evaluate calls. Deters someone cycling tokens or
  // emails from a single IP. Counted per client IP per calendar day.
  EVAL_IP_DAILY_CAP: 3,
  // Per-user cost cap over a rolling CALENDAR MONTH, in US dollars. Default 5.00.
  MONTHLY_COST_CAP_USD: 5.0,
  // How many access requests a single IP may make per day (request-access).
  IP_DAILY_REQUEST_CAP: 20,
  // Cooldown, in hours, before the same email can be issued another token.
  EMAIL_COOLDOWN_HOURS: 24,
  // Default per-day quota stamped onto a freshly minted token.
  DEFAULT_TOKEN_QUOTA_PER_DAY: 10,
};

// SUPPORT_URL: deployment value, not a study value. The operator fills this in
// with their own support or donation page. Shown in the friendly "monthly cost
// cap reached" response. Overridable per deployment with the SUPPORT_URL var.
export const SUPPORT_URL = "https://example.com/support";

// The warm, collective capped message. Phrased as "we", never as the user
// having hit their own limit.
export const CAPPED_MESSAGE =
  "We have reached our token limit for now. If you find this useful, please " +
  "consider supporting us so we can keep it running.";
