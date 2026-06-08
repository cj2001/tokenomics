# I Pitted an LLM Against Senzing for Agentic Entity Resolution.  And Whew.

## Tl;Dr

*** Reword this paragraph

And the per-pass cost is steep before you even get to that.  Senzing resolved the full 92,175-record dataset in about two minutes at zero marginal cost.  The LLM needed 7+ minutes on 10,000 records — roughly 11% of the data — at about $10 a run, and that's using the best model available, because every cheaper option did worse.  Scale to the 100M-record datasets people actually run in production and a single pass runs into six figures of API spend.  You can parallelize the wall-clock down, but you can't parallelize away the token bill, or the size of the fleet you'd need to do it fast.

## Introduction

Okay so here's the deal.  I've been spending a lot of time lately poking at where LLMs actually earn their keep and where they're just expensive vibes in a trenchcoat.  Entity resolution (ER) felt like a fair fight on paper...take a pile of records about people, figure out which ones refer to the same human, merge accordingly.  LLMs are supposedly great at fuzzy text matching.  Senzing is a purpose-built ER engine that's been doing this for years.  Let the bake-off begin.

Of course, others have tried entity resolution with LLMs before and gotten mixed results.  However, they notably relied heavily on customizing the data, the prompt, or both [\[1-4\]](#references).  What's different about this experiment is that I didn't reshape the data or hand-engineer a prompt to the benchmark...the LLM got the same JSONL records Senzing got (just converted to CSV for token efficiency).  That makes this less "how high can the LLM score with the right prompt" and more "how does a general-purpose LLM compare to a purpose-built engine when you drop them into the same pipeline slot."

I set out to compare an LLM against Senzing on entity resolution (ER).  I was originally thinking that accuracy is the obvious thing to fight over, and there's a real fight there.  However, as I discovered through experimentation, it is a complicated and nuanced discussion that deserves its own blog post after the initial discussions on how to actually _do_ ER with an LLM are done.  So for this post I wanted to focus just on that initial part: the mechanics of using an LLM for ER.  Even more than that though, the surprise is that you don't need the accuracy argument at all.  What I found by actually doing ER with an LLM in terms of cost and time was so convincing that accuracy didn't even matter.  The cost was so prohibitive and the time required was not feasible for true production systems.  And those margins only widen as the data grows for reasons that cannot be fixed by just using a better or bigger LLM.  

Let's start with some of the basics.  ER, when done right, is not a one-time batch job.  Yes, you begin with doing an initial ingestion and resolution of your existing data.  However, it would be very unusual to just stop there.  New records will keep arriving.  So you need to have a system in place to handle that.  And it would be really inefficient to have a system that had to completely re-run the ER process every time it took in a single new record.  

When you think about how an LLM would be doing an ER job, it is doing it as a batch job.  You hand it records, it hands back groupings that ideally correspond to resolved entities.  But what happens when a new record comes in?  This happens all the time.  Your business gains a new customer, a dataset gets updated, or you bring new data onboard your existing platform.  Any time you get new data, whether it is a single record or a complete dataset, you would have to throw _all_ of the data back at the LLM and re-do the complete ER.  The only way around that while still using an LLM would involve the LLM calling a tool that would go out and query an existing database of entities that had already been resolved.  And if you do that, you are essentially just writing your own ER system and not taking advantage of the flexibility that might be available if an LLM actually could do the job.  So you didn't actually skip building an ER engine.  You built one and dropped your most expensive part (the LLM) into its cheapest job.

If you're building production ER at any meaningful scale, you won't get there with an LLM on its own.  This isn't because LLMs aren't smart enough, but because it's the wrong *tool* for the job.  You need a purpose-built ER engine.  The rest of this post shows the work, and why this is structural, not something next quarter's model patches.

### An Analogy

People buy ER systems for their safety features.  In a way, it is like a car.  People buy cars because, in addition to achieving the outcome of getting you from point A to point B, they provide you with a sturdy metal frame, brakes, a metal exterior around you, seat belts, air bags, etc.  ER systems are pretty similar.  When you buy one you get access to a bunch of safety things like audit trails, deterministic decisions, defensible explanations, reproducible know-your-customer (KYC) / anti-money laundering (AML) / sanctions outcomes.  That's the actual requirement people walk in with: "I need the safest possible vehicle."

So if we take this analogy a bit futher, the LLM-based approach is the car of the future: self-driving demos, touchscreens everywhere, voice interfaces, etc.  The only problem is that the first trip costs more than the car, and to make it street-legal you end up building the chassis, the brakes, and the transmission yourself...at which point the shiny bit you started with is really just a very expensive seat.  The innovations are real.  They just have nothing to do with why anyone walked onto the lot.  Keep that in mind as we start getting into the numbers.  The first half of this post is about what that first trip costs, and the second is about everything you'd have to bolt on to make the thing street-legal at all.

## The Setup

The data came from Senzing's [Las Vegas CORD](https://senzing.com/senzing-ready-data-collections-cord/), which is a free collection of "Senzing-ready" datasets that share a Las Vegas geographic footprint.  CORD stands for Collection Of Relatable Data, and the whole point of these is that the datasets contain overlapping features like names and addresses across different sources, which makes them perfect for testing entity resolution.

I picked two of them...the **Equifax B2BConnect** dataset (firmographic and contact data) and the **US National Provider Index** (a registry of US healthcare providers).  Both CORDs include records for organizations and people, but for this experiment I limited the input to the people-only records from each.  Combined, the two people files come to 92,175 records.  That's not enormous by modern data standards, but it's modestly past the toy-problem threshold.

I ran Senzing on bare metal with PostgreSQL as the backing store, using the Senzing SDK.  For the LLM side, I used Claude Opus 4.8 and prompted it to behave like an ER engine and emit results in the same Senzing JSONL format.  Same input, same expected output, totally different machinery.

I tested both pipelines at four sample sizes of the dataset, distributed statistically between the two as they are in the complete dataset.  The sample sizes were 500 records, 2,500, 5,000, and 10,000.  Going bigger than 10,000 with the LLM started getting silly on cost and time, so for the full 92,175 I extrapolated mathematically and ran Senzing on the actual file for comparison.

### A note on the LLM's output format

It is worth pausing here, because this is where you start to see how much of an ER engine you actually need to mimic when you're playing pretend with an LLM.  I asked Claude to emit Senzing-style JSONL so that I could score both pipelines with the same code.  And it does, sort of.  Each line of LLM output looks like this...

```
{"RESOLVED_ENTITY": {...}, "RELATED_ENTITIES": [...]}
```

with the same top-level field names Senzing uses (`ENTITY_ID`, `ENTITY_NAME`, `RECORD_COUNT`, `RECORDS`, `DATA_SOURCE`, `RECORD_ID`, `MATCH_KEY`, `ERRULE_CODE`).  That was deliberate on my end.  My output-builder was specifically written to mimic the export format so that the same scoring scripts could chew through both files.

But under that thin shell, a real Senzing export carries a lot more freight that the LLM output simply doesn't have.  Things like...

* The `FEATURES` block, with per-entity `NAME`, `ADDRESS`, `PHONE`, `EMAIL`, and `DOB` lists plus usage stats.
* The full original record payload (`JSON_DATA`) for every record in the entity.
* `INTERNAL_ID`, `MATCH_LEVEL`, `MATCH_LEVEL_CODE`, `MATCH_KEY_DETAILS`, and `FEATURE_SCORES` on each record.
* `ECCLASSIFICATIONS`, `ENTITY_NAME_DETAILS`, and `BEST_NAME` on each entity.
* Richer `RELATED_ENTITIES` entries that include `MATCH_LEVEL`, `IS_DISCLOSED`, `IS_AMBIGUOUS`, and a handful of other fields.  The LLM version only writes `ENTITY_ID`, `MATCH_LEVEL_CODE`, and `MATCH_KEY`.

So what the LLM produces is "compatible enough for the scoring scripts" rather than "drop-in interchangeable with a Senzing export."  However, if you feed an LLM-output JSONL into a downstream tool that expects Senzing's full schema (something like `sz_explorer`, or anything that reads the FEATURES or JSON_DATA blocks), it breaks.  This isn't a knock on the LLM exactly.  I didn't ask it to fabricate feature scores or match levels, because those would just be hallucinations dressed up as engineering.  What the LLM _does_ do is match the easy outer layer of the schema cheaply.  But this leaves the evidence and scores that a real ER engine produces, a byproduct of the ER decision making undone.  And that difference between "looks like the output" and "did the work" is an important distinction!

---
## Acknowledgements

I want to thank Paco Nathan, Jeff Butcher, and Brian Macy for their helpful discussions on this topic.

## References

1. Li, Y., Li, J., Suhara, Y., Doan, A., & Tan, W.-C.  *Deep Entity Matching with Pre-Trained Language Models* (Ditto).  arXiv:2004.00584, 2020.  Published in VLDB 2021.  [https://arxiv.org/abs/2004.00584](https://arxiv.org/abs/2004.00584)

2. Peeters, R., Steiner, A., & Bizer, C.  *Entity Matching using Large Language Models*.  arXiv:2310.11244, 2023.  [https://arxiv.org/abs/2310.11244](https://arxiv.org/abs/2310.11244)

3. *Match, Compare, or Select?  An Investigation of Large Language Models for Entity Matching.*  COLING 2025.  arXiv:2405.16884.  [https://aclanthology.org/2025.coling-main.8/](https://aclanthology.org/2025.coling-main.8/)

4. *Structured Multi-Step Reasoning for Entity Matching Using Large Language Model.*  arXiv:2511.22832, 2025.  [https://arxiv.org/abs/2511.22832](https://arxiv.org/abs/2511.22832)

