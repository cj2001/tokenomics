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

### The two LLM approaches

One quick note before I get into the chunking strategies.  The CORDs ship as Senzing-format JSONL, which is great for Senzing because that's literally what it eats.  For the LLM, though, JSON is wildly token-inefficient.  Every field name gets repeated on every record, every quote and brace and colon eats tokens, and you end up paying to serialize syntax instead of data.  So before any of this hit the LLM, I converted the input from JSONL to CSV.  Same fields, same values, but the column headers appear once at the top instead of once per record, and the structural overhead drops dramatically.  In my testing this cut input tokens by roughly 30% compared to sending the raw JSONL through, which directly translates into faster runs and lower bills.  Senzing got the original JSONL because that's its native format.  The LLM got the CSV-converted version of the exact same records.

The next problem I encountered was that I couldn't send all of the data to the LLM at once because of the limits imposed by Anthropic.  First, you need to think about the context window.  Claude Opus 4.8, used in this post, can take a lot of tokens as input in one shot, but not thousands of records plus a prompt.  Then there is the rate-limit and per-request token-cap.  Anthropic enforces celinings on these so even if you can fit all of your data within the context window, you are not allowed to fire it all at once at the API.  So I needed to break the data into cheunks.  For this article, I trief two different approaches for the chunking.  And, not surprisingly, how you build the chunks turns out to matter a lot.

The first approach, which I named the **batched fast** approach, fills its chunks by taking records from each data source in roughly the order they came in and rotating between sources.  The goal is throughput.  Which specific records end up together in a chunk is essentially arbitrary, dictated more by file ordering than by content.

The **blocked** approach builds its chunks deliberately.  Records are grouped by the first few letters of the last name (I used four), so that anyone who might be a duplicate of someone else (every "GRIGGS," every "COHEN," every "ASHWORTH") ends up in the same chunk before being sent to the model.  The specific key doesn't matter — it's just a proxy to get sane block sizes.  Note that there are a ton of different ways that this could be done and I just created a real simple one.  I will say more about this below.

This distinction matters because I theorized that the LLM can only merge two records when it sees them side by side in the same chunk.  If two duplicates land in different chunks, the fast approach might not have the chance to spot them.  The blocked approach is engineered so that they almost always do.

Now let's talk about the same concept from the ER side of the house.  The thing every ER person treats as table stakes is blocking (AKA candidate generation).  Here's the problem it solves.  You can't compare every record to every other record at scale.  That's the O(n²) wall that has always defined ER, and it's brutal.  So every real ER pipeline starts by narrowing the field down to just the records actually worth comparing.  You can do it a bunch of ways...typed keys, blocking on a single field, semantic or vector search, whatever fits your data.  Senzing does it with principled candidate keys.

For the LLM, the chunking IS the blocking.  Think about it...deciding which records should be sent to the LLM at the same time, such as in the blocked approach I described above, is the exact same decision as deciding which records get compared, because the model can only ever merge two records it actually sees together.  Same move, different name.

The bottom line here is that both the LLM approach and the Senzing approach require blocking first.  And it is important to know that good blocking isn't free, which is crucial at scale.  Block on surname and the bucket for common names starts to balloon as your corpus grows.  Every SMITH, every GARCIA...those dense regions of identity space spit out blocks of thousands, not dozens.  So the records the model has to chew through per block keep getting bigger over time, and that's for a reason that has nothing to do with how good the model is.  It is just a fact of life: some individual blocks can get really big and this could confuse or even hit the rate limits on the LLM.  And then the interest question is what happens to those big blocks once you have surfaced them.

And critically, both LLM approaches saw the exact same input data as Senzing did at each sample size.  Same 500 records.  Same 2,500.  Same 5,000.  Same 10,000.  Apples-to-apples, all the way down.

## What the Numbers Said: Does the Car Move?

Here's the headline figure.  Four panels...time, cost, total tokens used, and number of records merged.  All on log-log scales because the dynamic range gets wild fast.

Let me walk through what jumped out.

<img src="data/figures/timing_figure.png" alt="Time Required to Do ER" width="600">

| Records | Senzing | LLM batched fast | LLM blocked |
|---:|---:|---:|---:|
| 500 | 2.99 s | 6.1 s | 111 s |
| 2,500 | 20.77 s | 29 s | 93 s |
| 5,000 | 31.96 s | 130 s | 207 s |
| 10,000 | 69.43 s | 371 s | 501 s |
| 92,175 (full) | 131.47 s | (extrapolated, hours) | (extrapolated, hours) |

Senzing on the full 92,175-record dataset finishes in just over two minutes.  Two minutes for the whole thing: load the data, resolve the entities, and write the results to PostgreSQL!  Meanwhile the LLM, at 10,000 records (which is about 11% of the data) is taking over six minutes in the fast variant and over eight in the blocked variant.

Let me be straight about the extrapolation.  I'm not handing you a magic equation.  There are a ton of different equations you could use to fit this data.  The smaller record counts clearly sit on a different slope, and you shouldn't treat any fitted curve as gospel.

But here's the thing...you don't actually need the curve.  You just need the mechanism, and once you see it, you'll get why the slope HAS to steepen.

Let's start with what blocking does for you.  It's the only reason ER is tractable in the first place...it stops you from comparing all 92,175 records against each other.  Huge win.  But inside a single block, finding every duplicate STILL means weighing every record against every other record in that block.  And that work is quadratic in the block's size.  Double the records in a chunk and you roughly quadruple the comparison work, while only doubling the tokens you're paying for.  This is the part people miss when they get excited about bigger context windows...a bigger window is a bigger box, not a faster engine.  More room to stuff records in doesn't make the comparisons inside any cheaper.

And here's the kicker, the part blocking does NOT rescue you from.  As the corpus grows, the blocks for common names grow right along with it.  Every SMITH, every GARCIA like we discussed before.  So your most expensive blocks get more expensive faster than the dataset as a whole does.

That's the wall the timing curve is climbing.  And it's exactly why "just use a model with a 10M-token window" makes the economics worse, not better...you're buying a bigger box to hold a problem that punishes you for filling it.

And before anyone says you can parallelize the problem to make it faster...you can, but you can't parallelize away the *work*.  The token bill is identical whether you run it serially or fan it across a thousand API keys, and the fleet of computers you'd need to chew through 100M records in any reasonable window is its own punchline.  You would be standing up a whole datacenter to brute-force what one engine does on a single node.  So yes, you can buy back the wall-clock through parallelization, but you can't buy back the token bill, and you probably can't buy that fleet either.

Senzing's curve barely moves while the LLM curves climb the wall.

### Cost

<img src="data/figures/cost_figure.png" alt="Total Cost of ER" width="600">

| Records | LLM batched fast | LLM blocked |
|---:|---:|---:|
| 500 | $0.44 | $0.56 |
| 2,500 | $2.50 | $2.51 |
| 5,000 | $4.93 | $4.98 |
| 10,000 | $10.04 | $10.17 |

Senzing isn't on this chart because Senzing's marginal cost basically isn't there.  You need a license file to run it, so it's not strictly free, but "every run costs you tokens" isn't how it works.  Once you're licensed, the marginal cost of running on more records is just CPU time on a machine you already own.  And Senzing offers a [free non-production evaluation license](https://senzing.com/request-non-prod-license/) you can use to duplicate this experiment yourself.  So for testing, prototyping, and benchmarking like I was doing here, the cost difference is genuinely zero versus ten dollars per 10,000 records.

What my experiments showed was that it cost about ten dollars to process 10,000 records.  This is a pretty steep investment for a really small amount of data!  Extrapolated to the full 92,175 the LLM lands somewhere around $90–$100 per run.  *Per run.*  And sure, you wouldn't re-run the whole corpus constantly when a single new record arrives.  You would resolve it once, persist the results, and manage those new records on arrival.  But that is exactly where "just use an LLM" quietly stops being true, which is the rest of this post.

### Tokens

<img src="data/figures/tokens_figure.png" alt="Number of Tokens Used for ER" width="600">

| Records | LLM batched fast | LLM blocked |
|---:|---:|---:|
| 500 | 86,941 | 93,141 |
| 2,500 | 489,661 | 492,671 |
| 5,000 | 964,027 | 968,657 |
| 10,000 | 1,947,296 | 1,908,223 |

The LLM burned through nearly two million tokens at 10,000 records, with the two variants almost identical.  (Senzing, of course, uses zero tokens because it isn't a language model.)  This figure is mostly here to show the cost numbers above aren't some weird pricing artifact.  The LLM really is doing nearly two million tokens of work to do what Senzing does in a fraction of the time at no marginal cost beyond the license.  None.

And it's a moving target.  These are Opus 4.8 counts, and per Anthropic's own notes the updated tokenizer maps the same input to roughly 1.0–1.35× the tokens that Opus 4.6 used at the same per-token price (independent measurements often land higher).  So "the same job" quietly got more expensive with a version bump, which is its own argument against building your cost model on top of token economics you don't control.



---
## Acknowledgements

I want to thank Paco Nathan, Jeff Butcher, and Brian Macy for their helpful discussions on this topic.

## References

1. Li, Y., Li, J., Suhara, Y., Doan, A., & Tan, W.-C.  *Deep Entity Matching with Pre-Trained Language Models* (Ditto).  arXiv:2004.00584, 2020.  Published in VLDB 2021.  [https://arxiv.org/abs/2004.00584](https://arxiv.org/abs/2004.00584)

2. Peeters, R., Steiner, A., & Bizer, C.  *Entity Matching using Large Language Models*.  arXiv:2310.11244, 2023.  [https://arxiv.org/abs/2310.11244](https://arxiv.org/abs/2310.11244)

3. *Match, Compare, or Select?  An Investigation of Large Language Models for Entity Matching.*  COLING 2025.  arXiv:2405.16884.  [https://aclanthology.org/2025.coling-main.8/](https://aclanthology.org/2025.coling-main.8/)

4. *Structured Multi-Step Reasoning for Entity Matching Using Large Language Model.*  arXiv:2511.22832, 2025.  [https://arxiv.org/abs/2511.22832](https://arxiv.org/abs/2511.22832)

