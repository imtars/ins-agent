# Insur-QA 本地 holdout 检索消融

查询数：256；完整去重语料：21953 passages。

| Pipeline | Recall@1 | Recall@5 | Recall@10 | MRR@10 | Hit@10 |
| --- | ---: | ---: | ---: | ---: | ---: |
| dense | 0.0781 | 0.2158 | 0.3047 | 0.1384 | 0.3125 |
| sparse | 0.0742 | 0.2461 | 0.3525 | 0.1485 | 0.3555 |
| hybrid | 0.0820 | 0.2383 | 0.3604 | 0.1534 | 0.3672 |
| hybrid_rerank | 0.1172 | 0.2773 | 0.4043 | 0.1947 | 0.4102 |

数字由 `python -m evaluation.rag.run` 实际生成；限制与参数见同名 JSON。
