# Synthetic evaluation summary

Recorded local run: **2026-09-27**, `checked` pipeline. Published as a historical summary; the portfolio release did not rerun the paid model evaluation.

| Case | Expected lines | Exact lines matched | Incorrect extracted lines | Result | Flagged lines |
|---|---:|---:|---:|---|---:|
| Classic digital, 1 page | 30 | 30 | 0 | verified | 0 |
| Modern digital, 1 page | 27 | 27 | 0 | verified | 0 |
| Classic digital, 5 pages | 177 | 177 | 0 | verified | 0 |
| Modern digital, 5 pages | 196 | 196 | 0 | verified | 0 |
| Classic digital, 30 pages | 1,012 | 1,012 | 0 | verified | 0 |
| Modern digital, 30 pages | 1,157 | 1,157 | 0 | verified | 0 |
| Classic scanned, 1 page | 30 | 30 | 0 | verified | 0 |
| Modern scanned, 5 pages | 165 | 165 | 0 | verified | 0 |
| Classic scanned, 30 pages | 1,081 | 1,050 | 31 | needs review | 33 |

Total: **3,844 / 3,875 exact expected lines matched**. Eight complete documents matched all checked header and line fields. No incorrect extracted lines escaped flags on these cases; no incorrect invoice was marked verified.

The last invoice demonstrates why the human review path exists. Two correct lines were also flagged. A zero silent-error observation on nine generated documents does not establish a zero-error rate on unfamiliar layouts, real photographs, other languages or adversarial documents. The set is small and repeated use during development can favor these layouts. Model results may vary between runs.

The answer keys and fixtures are public; full extraction logs and any real uploads are excluded. The matching / grading logic is in `src/synq_paperwork/evals.py`.
