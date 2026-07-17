# Quality rubric for encyclopedic passages

Every rater, human persona or model judge, scores a Wikipedia paragraph on the
same seven writing-quality dimensions plus an overall score. Each is an integer
from 1 (worst) to 10 (best). The dimensions are grounded in Wikipedia's own
quality criteria; some overlap is expected, and a later phase prunes redundant
dimensions.

| Dimension | What it measures |
| --- | --- |
| clarity | Is the passage clear and easy to understand on a first read? |
| neutrality | Is the tone neutral and impartial, free of bias or promotion? |
| verifiability | Do claims appear sourced or attributable rather than unsupported? |
| coverage | Does it cover its topic adequately for its scope, without obvious gaps? |
| structure | Is it well organized and coherent, with ideas that flow logically? |
| readability | Is the language fluent, grammatical, and in an encyclopedic register? |
| informativeness | Does it convey substantive, useful information efficiently? |
| overall | Direct holistic judgment of writing quality, decided independently and NOT as the average of the dimensions above. |

The overall score is the target of the study and is collected as a direct,
independent holistic judgment from every rater (human personas and LLM judges
alike), so later analysis can compare it against computed composites of the
seven dimensions. The dimension scores are recorded for that analysis and for
future work.

Output shape (enforced in code) is a single JSON object where every field
carries a short reason written before an integer score, for example:

```
{
  "clarity": {"reason": "...", "score": 7},
  "neutrality": {"reason": "...", "score": 8},
  "verifiability": {"reason": "...", "score": 5},
  "coverage": {"reason": "...", "score": 6},
  "structure": {"reason": "...", "score": 7},
  "readability": {"reason": "...", "score": 8},
  "informativeness": {"reason": "...", "score": 6},
  "overall": {"reason": "...", "score": 7}
}
```
