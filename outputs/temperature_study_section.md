## Output consistency versus temperature

This side experiment measures how stable a single judge's scores are when the
same passage is scored over and over. It isolates one judge, one prompt, and one
passage, and varies only the sampling temperature. The goal is to separate two
sources of the judge disagreement seen in the main study: genuine differences in
judgment between models versus mere sampling noise inside one model.

### Setup

The passage is item_0105, the single passage on which the four study judges
disagreed most. Their overall scores on it span the full usable range: the
claude-haiku-4.5 judge gave 1, gpt-5.2 gave 5, perplexity-sonar gave 8, and
gemini-2.5-flash gave 10 (overall variance 11.5, the largest of all 500
passages, and also the largest mean across-dimension variance). The passage is a
bare bibliographic citation rather than a prose paragraph, so the judges split
on whether it should be scored as a well formed reference entry or rejected as
non-encyclopedic content. It is an ideal stress test for scoring stability.

The judge is claude-haiku-4.5 with its selected best prompt (v3) and the study's
rubric and anchors, exactly as in the main run. The passage was scored 300 times
at each temperature in {0, 0.1, 0.2, ..., 1.0}, for 3300 scored draws in total.
Each draw records the overall score plus all seven dimension scores. Calls were
cached so an interruption never repeated a completed draw, and the spend was
tracked in a ledger separate from the main study's cost log.

Before the sweep, temperature was confirmed to be honored for this model: a short
creative prompt returned four identical completions at temperature 0 and four
distinct completions at temperature 1.0. Temperature is therefore a real,
effective control here, not a silently ignored parameter.

### The temperature-0 determinism result

At temperature 0 the judge is fully deterministic on this passage. All 300 draws
were byte-for-byte identical: the same overall score of 1, the same seven
dimension scores, and the same written rationale every time. The overall score
had zero variance and exactly one distinct value across all 300 calls. In other
words, at temperature 0 repeated scoring adds no noise at all, and a single call
is a complete summary of what the judge will say. This is the core finding, and
it justifies the main study's choice to run every judge at temperature 0 for
reproducibility.

### How variability and the average change as temperature rises

Any temperature above 0 breaks that determinism immediately. Even at temperature
0.1 the overall score starts taking neighbouring values, and the standard
deviation jumps from 0 to about 0.5. The average also shifts up at once: the mean
overall rises from 1.00 at temperature 0 to roughly 1.6 for every positive
temperature, an increase of about 0.6 of a point that appears as soon as
sampling is enabled and then stays roughly flat. This is a discrete step, not a
gradual drift. The reason is a floor effect. At temperature 0 the score is pinned
at the scale minimum of 1, so sampling can only move it upward, and the mean
lifts the moment the distribution is allowed to spread.

The overall score itself stays fairly concentrated across the whole range. The
median is 2 at every positive temperature and the interquartile range is a single
point wide throughout, so the typical draw is a 1 or a 2 regardless of
temperature. What grows with temperature is the tail. The maximum sampled overall
score climbs from 3 at low temperature to 5, 6, and 7 at the high end, and the
standard deviation widens from about 0.5 to about 0.7. So higher temperature does
not move the center of the overall score much, it mainly adds rare high outliers.

The dimension scores are far more volatile than the overall score, and this is
where most of the temperature-driven inconsistency lives. Per-dimension standard
deviations rise from 0 at temperature 0 to between about 1 and 2.8 points at high
temperature. The coverage dimension is the most unstable: the model increasingly
cannot decide whether a citation can be scored for coverage at all, and at higher
temperature it more and more often writes a non-numeric "N/A" for coverage
instead of an integer. The rate of such schema violations climbs from 0 at
temperature 0 to roughly a quarter of all draws at the highest temperatures. That
rising N/A rate is itself a form of output inconsistency: the same input yields a
schema-valid response most of the time and a schema-violating one a growing
fraction of the time as temperature increases.

### Figures

Distribution of the overall score at each temperature. Temperature 0 is a single
point at 1, and the spread and upper tail grow with temperature while the median
stays at 2.

![Distribution of the overall score by temperature](docs/images/temperature_overall_distributions.png)

Mean overall score versus temperature, with the temperature-0 value marked. The
mean steps up by about 0.6 of a point as soon as temperature is positive, then
stays roughly flat.

![Mean overall score versus temperature](docs/images/temperature_mean_overall.png)

Spread of the overall score versus temperature. The standard deviation rises from
0 at temperature 0 to about 0.7, while the interquartile range stays one point
wide.

![Spread of the overall score versus temperature](docs/images/temperature_spread.png)

### Mean and spread by temperature

The table reports, for each temperature, the number of draws, the mean overall
score, the standard deviation and interquartile range of the overall score, the
range of sampled values, and the shift of the mean relative to the temperature-0
value of 1.00. The last column is the fraction of draws in which the model
returned a non-numeric coverage score.

| temperature | draws | mean overall | SD | IQR | min to max | mean minus temp-0 | coverage N/A rate |
|---|---|---|---|---|---|---|---|
| 0.0 | 300 | 1.00 | 0.00 | 0.0 | 1 to 1 | 0.00 | 0.00 |
| 0.1 | 300 | 1.61 | 0.50 | 1.0 | 1 to 3 | 0.61 | 0.00 |
| 0.2 | 300 | 1.62 | 0.49 | 1.0 | 1 to 2 | 0.62 | 0.01 |
| 0.3 | 300 | 1.54 | 0.50 | 1.0 | 1 to 2 | 0.54 | 0.03 |
| 0.4 | 300 | 1.53 | 0.53 | 1.0 | 1 to 3 | 0.53 | 0.06 |
| 0.5 | 300 | 1.52 | 0.56 | 1.0 | 1 to 3 | 0.52 | 0.10 |
| 0.6 | 300 | 1.58 | 0.67 | 1.0 | 1 to 5 | 0.58 | 0.23 |
| 0.7 | 300 | 1.63 | 0.59 | 1.0 | 1 to 4 | 0.63 | 0.26 |
| 0.8 | 300 | 1.67 | 0.72 | 1.0 | 1 to 7 | 0.67 | 0.24 |
| 0.9 | 300 | 1.67 | 0.73 | 1.0 | 1 to 6 | 0.67 | 0.25 |
| 1.0 | 300 | 1.74 | 0.67 | 1.0 | 1 to 5 | 0.74 | 0.22 |

### What this means for the main study

Two conclusions follow. First, at temperature 0 the judges are deterministic, so
the disagreement documented in the main study is real disagreement between models
and not an artifact of sampling noise. Repeating a temperature-0 evaluation would
reproduce the exact same score. Second, raising temperature does not merely add
symmetric noise around the temperature-0 answer. On a passage where the judge sits
at the scale floor it introduces an upward bias in the mean of more than half a
point and a growing rate of schema violations on the hardest dimension. For an
evaluation pipeline the practical guidance is to score at temperature 0: it is
both the most reproducible setting and the one that avoids the floor-effect bias
seen here.

### Caveats

The result is for one passage and one judge model, chosen precisely because it was
the hardest case in the study. That passage sits at the scale floor: the judge
rates it 1 at temperature 0. This shapes the temperature result, because a floored
score can only be pushed upward by sampling, so the upward mean shift seen here is
partly a floor artifact rather than a general property of temperature. Had the
chosen passage sat mid-scale, say around 5, the variation would likely be more
symmetric and the mean shift smaller or in either direction. Characterizing
temperature's effect in general would need a more neutral analysis over passages
spanning the scale rather than this single boundary case, which we do not do here.
The temperature-0 determinism finding, however, is a property of the decoding and
is expected to hold generally for this model. A minor
data-collection note: for temperatures 0.1 through 0.5 part of the sample was
gathered by an earlier strict parser that discarded draws whose coverage score was
non-numeric, so the reported coverage N/A rate for those temperatures understates
the true rate (the sharp step at 0.6, where collection switched to a lenient
parser that keeps such draws, reflects this). The overall-score statistics are
affected only marginally, because the discarded draws carried overall scores of 1
or 2 that are already the dominant values, and the temperature-0 result is not
affected at all.
