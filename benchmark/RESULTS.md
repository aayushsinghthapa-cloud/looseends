# Benchmark results

**Task:** Recover hidden see-also links between Kubernetes Enhancement Proposals. 295 declared links across 176 KEPs were hidden; each method ranks 677 candidates per KEP.

| Method | Hits@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|
| text (TF-IDF document search) | 0.336 | 0.600 | 0.699 | 0.563 |
| graph: shared people | 0.117 | 0.375 | 0.532 | 0.331 |
| graph: shared gates + teams | 0.047 | 0.168 | 0.225 | 0.165 |
| graph: proposal mentions | 0.129 | 0.546 | 0.600 | 0.422 |
| graph: all except mentions | 0.142 | 0.430 | 0.590 | 0.382 |
| graph: all signals | 0.320 | 0.659 | 0.756 | 0.593 |
| hybrid: graph + text | 0.429 | 0.787 | 0.886 | 0.691 |

Hits@1 is the share of hidden links ranked first. Recall@k is the share of hidden links found in the top k. MRR is the average of 1 / rank of the first correct answer. Higher is better for all.

Graph weights were fixed before testing: {'mention': 3.0, 'mention_2hop': 0.5, 'people': 1.0, 'gates': 2.0, 'same_team': 0.3, 'shared_teams': 0.2}. The hybrid's text weight was chosen by 2-fold cross-validation (per fold: [16, 32]), so it is never tuned on the KEPs it is scored on.

Reproduce with `python -m looseends.benchmark`.
