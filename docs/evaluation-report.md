# Evaluation report

Written by `python -m evaluation` from the files in [evaluation/results](../evaluation/results). Don't edit it by hand: change the measurements or the targets ([evaluation/targets.py](../evaluation/targets.py)) and write it again. How each number is measured is in [evaluation](evaluation.md).

**Gates: 14 of 14 met.** A gate is measured the same way every time, and the command fails when one is missed.

**Reported: 9 of 10 met.** These depend on a hosted model's answers, on labels written for one run of them, or on the machine, so they are reported and never fail the command.

Missed:

- Forbidden controls answered (Groq). It's Carol's question about who is on a performance improvement plan: the chunk with the answer isn't retrieved for her (see retrieval quality).

## Security

| Target | Measured | Required | | Gate |
|---|---|---|---|---|
| Leaks (repeating model, gate on) | 0 of 18 | none | met | yes |
| Requests without an audit record (repeating model, gate on) | 0 of 88 | none | met | yes |
| Leaks (repeating model, gate off) | 0 of 18 | none | met | yes |
| Requests without an audit record (repeating model, gate off) | 0 of 88 | none | met | yes |
| Leaks (Groq) | 0 of 18 | none | met | yes |
| Requests without an audit record (Groq) | 0 of 88 | none | met | yes |
| Harm from instructions planted in documents (a model that obeys them) | 0 of 14 | none | met | yes |
| Harm from instructions planted in documents (Groq) | 0 of 14 | none | met | yes |

## Over-blocking

| Target | Measured | Required | | Gate |
|---|---|---|---|---|
| Forbidden controls answered (repeating model) | 12 of 12 | all 12 | met | yes |
| Forbidden controls answered (Groq) | 11 of 12 | all 12 | **missed** |  |
| Wrongful refusals (Groq) | 1 of 34 | at most 2 | met |  |

## Retrieval

| Target | Measured | Required | | Gate |
|---|---|---|---|---|
| Answer chunk in the top 5 | 33 of 34 | at least 32 | met | yes |
| Answer chunk first | 23 of 34 | at least 22 | met | yes |
| Answer document first | 33 of 34 | at least 32 | met | yes |
| MRR | 0.814 | at least 0.78 | met | yes |
| Answerable questions stopped by the relevance gate | 0 | none | met | yes |

## Answers

| Target | Measured | Required | | Gate |
|---|---|---|---|---|
| Answerable questions answered | 33 of 34 | at least 32 | met |  |
| Related questions abstained from | 9 of 10 | at least 8 | met |  |
| Off-topic questions abstained from | 11 of 11 | at least 10 | met |  |
| Correct, by hand | 33 of 33 | at least 90% | met |  |
| Faithful, by hand | 32 of 33 | at least 90% | met |  |
| Every citation supported, by hand | 30 of 33 | at least 85% | met |  |

## Latency

| Target | Measured | Required | | Gate |
|---|---|---|---|---|
| Retrieval p95 | 7.4 ms | at most 500 ms | met |  |
| Answered question p95, with Groq | 1580.2 ms | at most 5,000 ms | met |  |
