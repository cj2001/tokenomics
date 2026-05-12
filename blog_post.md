# I Pitted an LLM Against Senzing for Agentic Entity Resolution.  And Whew.

## Bottom Line Up Front

Senzing beat Claude Opus 4.7 on every metric that matters for entity resolution (time, cost, and accuracy), and the gap widened as the dataset grew.  Senzing resolved a full 92,175-record dataset in about two minutes at zero marginal cost; the LLM needed 7+ minutes on just 10,000 records, cost roughly $10 per run, and topped out at an F1 of 0.88 against Senzing's results, dropping as low as 0.29 by 10,000 records.  If you are building a production ER pipeline at any meaningful scale, you will not get there with  AI on its own.  You need to use a purpose-built ER engine.  The rest of this post shows the work.

## Introduction

Okay so here's the deal.  I've been spending a lot of time lately poking at where LLMs actually earn their keep and where they're just expensive vibes in a trenchcoat.  Entity resolution (ER) felt like a fair fight on paper...take a pile of records about people, figure out which ones refer to the same human, merge accordingly.  LLMs are supposedly great at fuzzy text matching.  Senzing is a purpose-built ER engine that's been doing this for years.  Let the bake-off begin.

Of course, others have tried entity resolution with LLMs before and gotten mixed results.  However, they notably relied heavily on customizing the data, the prompt, or both [\[1-4\]](#references).  What's different about this experiment is that I didn't reshape the data or hand-engineer a prompt to the benchmark...the LLM got the same JSONL records Senzing got (just converted to CSV for token efficiency).  That makes this less "how high can the LLM score with the right prompt" and more "how does a general-purpose LLM compare to a purpose-built engine when you drop them into the same pipeline slot."

I went in genuinely curious.  I'll tell you up front that I expected the LLM to lose on cost and maybe win on flexibility, or maybe trade blows depending on the dataset size.  That is...not what happened.  What actually happened is that Senzing won across pretty much every metric that matters, and the gap got wider as the data got bigger.  This post is me showing my work.  And, of course, you can check out all of the code I used to run this experiment in [this GitHub repo](https://github.com/cj2001/tokenomics).

## The Setup

The data came from Senzing's [Las Vegas CORD](https://senzing.com/senzing-ready-data-collections-cord/), which is a free collection of "Senzing-ready" datasets that share a Las Vegas geographic footprint.  CORD stands for Collection Of Relatable Data, and the whole point of these is that the datasets contain overlapping features like names and addresses across different sources, which makes them perfect for testing entity resolution.

I picked two of them...the **Equifax B2BConnect** dataset (firmographic and contact data) and the **US National Provider Index** (a registry of US healthcare providers).  Both CORDs include records for organizations and people, but for this experiment I limited the input to the people-only records from each.  Combined, the two people files come to 92,175 records.  That's not enormous by modern data standards, but it's modestly past the toy-problem threshold.

I ran Senzing on bare metal with PostgreSQL as the backing store, using the Senzing SDK.  For the LLM side, I used Claude Opus 4.7 and prompted it to behave like an ER engine and emit results in the same Senzing JSONL format.  Same input, same expected output, totally different machinery.

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
* Richer `RELATED_ENTITIES` entries that include `MATCH_LEVEL`, `IS_DISCLOSED`, `IS_AMBIGUOUS`, and friends.  The LLM version only writes `ENTITY_ID`, `MATCH_LEVEL_CODE`, and `MATCH_KEY`.

So what the LLM produces is "compatible enough for the scoring scripts" rather than "drop-in interchangeable with a Senzing export."  If you tried to feed an LLM-output JSONL into a downstream tool that expected Senzing's full schema (something like `sz_explorer`, or anything that reads the FEATURES or JSON_DATA blocks), it would break.  This isn't a knock on the LLM exactly...I didn't ask it to fabricate feature scores or match levels, because those might just be hallucinations dressed up as engineering.  But it's worth flagging that the shape of the output is matching the easy outer layer, and the parts that real downstream tooling actually depends on aren't there.

### The two LLM approaches

One quick note before I get into the chunking strategies.  The CORDs ship as Senzing-format JSONL, which is great for Senzing because that's literally what it eats.  For the LLM, though, JSON is wildly token-inefficient.  Every field name gets repeated on every record, every quote and brace and colon eats tokens, and you end up paying to serialize syntax instead of data.  So before any of this hit the LLM, I converted the input from JSONL to CSV.  Same fields, same values, but the column headers appear once at the top instead of once per record, and the structural overhead drops dramatically.  In my testing this cut input tokens by roughly 30% compared to sending the raw JSONL through, which directly translates into faster runs and lower bills.  Senzing got the original JSONL because that's its native format.  The LLM got the CSV-converted version of the exact same records.

Both LLM approaches send records to the model in chunks, because no single chunk can hold the whole dataset at once.  This is partly a context-window limit (Claude Opus 4.7 can take a lot in one shot, but not "92,175 person records plus a system prompt plus instructions" worth) and partly a rate-limit and per-request token-cap issue.  Anthropic enforces tokens-per-minute and tokens-per-request ceilings, and even when you have headroom on the context window itself, you're not allowed to fire it all at once at the API.  So you chunk.  How you build the chunks turns out to matter a lot.

The thing I named the **batched fast** approach fills its chunks by taking records from each data source in roughly the order they came in and rotating between sources.  The goal is throughput.  Which specific records end up together in a chunk is essentially arbitrary, dictated more by file ordering than by content.

The **blocked** approach builds its chunks deliberately.  Records are grouped by the first few letters of the last name (I used four), so that anyone who might be a duplicate of someone else (every "GRIGGS," every "COHEN," every "ASHWORTH") ends up in the same chunk before being sent to the model.

This distinction matters because I theorized that the LLM can only merge two records when it sees them side by side in the same chunk.  If two duplicates land in different chunks, the fast approach might not the chance to spot them.  The blocked approach is engineered so that they almost always do.

And critically, both approaches saw the exact same input data as Senzing did at each sample size.  Same 500 records.  Same 2,500.  Same 5,000.  Same 10,000.  Apples-to-apples, all the way down.

## What the Numbers Said

Here's the headline figure.  Four panels...time, cost, total tokens used, and number of records merged.  All on log-log scales because the dynamic range gets wild fast.

Let me walk through what jumped out.

### Time

<img src="data/figures/timing_figure.png" alt="Time Required to Do ER" width="600">

| Records | Senzing | LLM batched fast | LLM blocked |
|---:|---:|---:|---:|
| 500 | 2.99 s | 5.5 s | 135.2 s |
| 2,500 | 20.77 s | 31.0 s | 99.2 s |
| 5,000 | 31.96 s | 86.9 s | 284.7 s |
| 10,000 | 69.43 s | 434.3 s | 512.3 s |
| 92,175 (full) | 131.47 s | (extrapolated, hours) | (extrapolated, hours) |

Senzing on the full 92,175-record dataset finishes in just over two minutes.  Two minutes for the whole thing, meaning two minutes to load the data, resolve the entities, and write the results to the PostGreSQL database.  The LLM at 10,000 records (about 11% of the data) is taking over seven minutes in the fast variant and over eight in the blocked variant.  Extrapolated out to the full dataset, you're looking at hours...if it would even complete at all. 

This wasn't even close.  Senzing's curve barely moves while the LLM curves climb the wall.

**A quick notes on the fits used in this plot and subsequent ones:**  I am not making any claims on what is the correct type of equation to fit the data.  In log-log plots the power law can make sense and I use that equation type frequently for the fits in this post.  However, in some cases like this one it is instructive to break out of that because the data in the smaller record numbers seems to be of a different slope on a log-log plot.  So don't take the provided equations as gospel.  I am just using them to illustrate a reasonable fit where we actually have measurements and then to provide you an idea of what would happen if we extrapolate all the way out to 100M records.

### Cost

<img src="data/figures/cost_figure.png" alt="Total Cost of ER" width="600">

| Records | LLM batched fast | LLM blocked |
|---:|---:|---:|
| 500 | $0.44 | $0.66 |
| 2,500 | $2.50 | $2.52 |
| 5,000 | $4.79 | $5.16 |
| 10,000 | $10.25 | $10.14 |

Senzing isn't on this chart because Senzing's marginal cost is not really there.  You do need a license file to run Senzing, so it's not strictly free, but Anthropic-style "every run costs you tokens" pricing isn't how it works.  Once you're licensed, the marginal cost of running it on more records is just CPU time on a machine you already own.  And critically, Senzing offers a [free non-production evaluation license](https://senzing.com/request-non-prod-license/), which you can get to duplicate this experiment in your own.  So for testing, prototyping, and benchmarking like I was doing here, the cost difference is genuinely zero versus ten dollars per 10,000 records.  Extrapolated to the full 92,175 dataset, the LLM lands somewhere in the ballpark of $90 to $100 per run.  _Per run._  If you're iterating on your pipeline or running this nightly, that's real money.

Ten dollars to process 10,000 records, and roughly half a dollar to process 500, is a steep ask for what amounts to a fairly limited amount of data.  When you frame it as "the cost of resolving one healthcare provider directory," it suddenly looks much less reasonable than the abstract per-token math suggests.

### Tokens

<img src="data/figures/tokens_figure.png" alt="Number of Tokens Used for ER" width="600">

| Records | LLM batched fast | LLM blocked |
|---:|---:|---:|
| 500 | 86,750 | 97,150 |
| 2,500 | 489,744 | 493,064 |
| 5,000 | 856,407 | 975,668 |
| 10,000 | 1,955,461 | 1,908,253 |

The LLM burned through nearly two million tokens at 10,000 records, with the two variants almost identical.  Token usage is certainly growing as the input gets larger, which is expected.  One thing worth noting here is that Claude Opus 4.7 uses an updated tokenizer compared to 4.6, and per Anthropic's own descriptions of the new tokenizer, token counts on the same input are expected to run higher than what you would have seen on 4.6.  So if you're doing back-of-the-envelope math from older Claude pricing experiments, expect the numbers on 4.7 to come in noticeably above what 4.6 would have given you.  (Although Anthropic's claim is that this new tokenizer results in much more accuracy for Opus 4.7.)

Senzing of course uses zero tokens because it isn't a language model.  This panel is mostly here to show that the cost numbers above aren't some weird pricing artifact...the LLM really is doing nearly two million tokens of work to do what Senzing does in a fraction of the time at no marginal cost beyond the license fee (i.e. free for a non-prod trial license).

### Records Merged

<img src="data/figures/merged_figure.png" alt="Number of Records Merged" width="600">

| Records | Senzing | LLM batched fast | LLM blocked |
|---:|---:|---:|---:|
| 500 | 1 | 1 | 1 |
| 2,500 | 8 | 13 | 10 |
| 5,000 | 28 | 1 | 36 |
| 10,000 | 89 | 31 | 101 |
| 92,175 | 5,931 | (n/a) | (n/a) |

There are some strange things going on here and it is worth talking about.  Look at the fast variant at 5,000 records.  It found one merge.  One.  Senzing found 28.  I want to spend a second on this row, because it tripped me up when I first saw it.  My first reaction was that I'd messed something up.  So I re-ran the 5,000-record fast experiment.  And I got the same result.  So I re-ran it again.  Same result.  I finallyl re-ran it a third time and got the same result.  Three independent runs, all returning exactly one merge.

And what makes it weirder is that the 2,500-record run produced 13 merges and the 10,000-record run produced 31 merges, both reasonable in the neighborhood of what Senzing was doing.  Just this one specific size collapsed to a single merge, reproducibly.

I'm honestly not sure what's going on inside the LLM at exactly 5,000 records that made it behave that way.  I don't have a clean explanation, but the result is reproducible, so I'm reporting it as I observed it.  The takeaway isn't really about that one weird data point anyway.  The takeaway is that the fast approach produces erratic results as the dataset grows, and "erratic" is not what you want from a production pipeline.

By contrast, the blocked variant tracks Senzing more closely.  It actually slightly over-merges relative to Senzing at 10,000 records (101 vs 89), but as you'll see in a second, "more merges" is not the same thing as "correct merges."

## Accuracy is Where Things Get Painful

Counting merges is one thing.  Knowing whether you got them right is another.  I scored both LLM variants against Senzing's results using standard pairwise precision, recall, and F1.

A quick word on what those mean here, because this matters for interpreting the numbers.  In this experiment, I am treating **Senzing's merges as the ground truth**.  That's the yardstick I'm measuring against.  Given that:

* **Precision** answers the question "of the merges the LLM proposed, what fraction match what Senzing found?"  High precision means the LLM isn't making stuff up.
* **Recall** answers the question "of the merges Senzing found, what fraction did the LLM also find?"  High recall means the LLM isn't missing the matches Senzing knows are there.
* **F1** is the harmonic mean of the two.  It rewards being good at both at once and punishes being lopsided.

A perfect score across the board means the LLM agrees with Senzing on every merge.  Lower precision means false positives (the LLM merged things that shouldn't be merged).  Lower recall means false negatives (the LLM missed merges Senzing caught).

### Fast variant pairwise quality

| Records | Precision | Recall | F1 |
|---:|---:|---:|---:|
| 500 | 1.00 | 1.00 | 1.00 |
| 2,500 | 0.62 | 1.00 | 0.76 |
| 5,000 | 0.00 | 0.00 | 0.00 |
| 10,000 | 0.56 | 0.19 | 0.29 |

This shows that the fast variant is sort of fine on _tiny_ datasets and falls apart everywhere else.

At 500 records it nails everything because there's basically only one merge to find and it found it.  At 2,500 records recall is still perfect (the LLM found every merge Senzing found) but precision dropped to 0.62, meaning roughly 38% of the merges it proposed were spurious.  Then 5,000 records is the bizarre row I described above, where the one merge it produced happened to be wrong, dragging F1 to zero.  Because that 5,000-record point is anomalous in a way I can't explain, it's more honest to read the trend by comparing the 2,500 and 10,000 endpoints and treating the middle as a known weird artifact.  Doing that, recall went from 1.00 down to 0.19, meaning the LLM went from finding every merge Senzing found to missing more than 80% of them.  F1 went from 0.76 to 0.29.  Precision drifted around in the 0.5 to 0.6 range across both endpoints, so call that flat-ish at "roughly half of the merges proposed are real."  The headline pattern is that as the data gets bigger, the fast variant's recall is collapsing and its precision isn't getting better fast enough to compensate.

The pattern here is erratic in a way that's hard to forgive.  You can sometimes work around a system that's consistently wrong in a known direction.  You can't really work around a system whose accuracy lurches around in unpredictable ways as the inputs grow.  Imagine trying to build a downstream pipeline on top of this where you have to explain to a stakeholder why the merge counts dropped 90% because somebody added a few hundred more records to the input.  Yeah...no thanks.

### Blocked variant pairwise quality

| Records | Precision | Recall | F1 |
|---:|---:|---:|---:|
| 500 | 1.00 | 1.00 | 1.00 |
| 2,500 | 0.73 | 1.00 | 0.84 |
| 5,000 | 0.78 | 1.00 | 0.88 |
| 10,000 | 0.69 | 0.82 | 0.75 |

The blocked approach holds up much more sensibly as the data grows.  As hypothesized, the ER results are slightly better than the fast approach since we are forcing similar names to all be in the same batch.  Recall stays at 1.0 all the way through 5,000 records, meaning the LLM caught every single merge Senzing found at those sizes.  Then at 10,000 records recall slips to 0.82, meaning the LLM started missing roughly one in five real merges.

Precision is more interesting.  It hovers around 0.7 to 0.78 across all the larger sizes and never gets close to perfect.  Translating that...about a quarter to a third of the merges the LLM is proposing are wrong.  And these aren't random errors either.  Looking at the false positives, they tend to be cases where two records share an obvious surface match (same name, same employer) but Senzing has flagged them as different people based on disambiguating signals like a different address or a different date of birth.  The LLM sees the surface similarity, doesn't weight the disambiguating fields the same way, and merges anyway.

What this tells me is that the blocked LLM is operating roughly the way an over-eager human reviewer would.  It catches the obvious stuff at small scale, starts making confident-but-wrong calls as the data grows, and at scale it both adds spurious merges and starts missing real ones.  At 10,000 records you've got a system that's correct on roughly three out of four merges and finds roughly four out of five it should.  That's an F1 of 0.75.  Some applications can tolerate that.  Anything where false positives or false negatives carry real consequences (think things like legal matters, compliance, voter registration, fraud detection, healthcare records, anti-money-laundering, etc.) cannot.

### Where the LLM falls short on recall

The false negatives are interesting too.  At 10,000 records the blocked variant misses things like "Troy Guerrette" vs "Troy Guerette," "Steve Griggs" vs "Stephen Griggs," "Dawn Grey" vs "Dawn Gray."  These are exactly the kinds of fuzzy matches you'd think LLMs would crush, because they're the same kind of fuzzy matches humans easily catch.  It is worth being upfront about one thing here, though.  I didn't add any explicit phonetic matching, nickname expansion, or spelling-variant logic to the LLM pipeline, so the model is leaning entirely on its general reasoning to bridge "Steve" and "Stephen" or "Grey" and "Gray."  Senzing caught these in the same dataset.  The LLM pipeline didn't.  That's the observation.

## The Bigger Story

Here's what the whole experiment told me.

**The LLM scales badly.**  Not just on cost and time, which I expected, but on accuracy, which I did not expect to anywhere near this degree.  Senzing's merge-count line is a clean diagonal all the way out to 92,175 records and roughly 5,931 merged records.  The LLM lines stop at 10,000 records, and the extrapolation is not encouraging given that F1 was already trending downward in the blocked variant and bouncing chaotically in the fast variant.

**The fast variant is essentially unusable above a few thousand records.**  Whatever's happening internally, the result is that I had to re-run the 5,000-record case three times to convince myself the single-merge result was real and not a glitch.  When you're hitting "rerun the experiment several times at significant cost to make sure I didn't break it" territory, you've left production-grade behavior behind.

**The blocked variant is better but still not in Senzing's neighborhood.**  Better blocking gets you to F1 around 0.75 to 0.88, which is fine for some use cases but not for anything where the merge quality has real downstream consequences.  And you're paying $10 per 10,000 records to get there, while a system you already own does it correctly in seconds.

**The cost-per-correct-merge is brutal.**  At 10,000 records, the blocked LLM cost about $10.14 and produced roughly 70 true-positive merges (out of 101 total).  That's roughly $0.14 per correct merge.  Senzing produced 89 correct merges in 69 seconds at zero marginal cost.  Multiply that out to production scale and the math gets uglier the bigger the dataset gets.

## What I'd Tell Someone Considering This

If you're building an ER pipeline and you're seriously considering "just use an LLM," I'd push back.  LLMs are great for a lot of things, but entity resolution at any meaningful scale is not currently one of them.  The fast approach falls apart, the blocked approach is slightly better but expensive and slower than a purpose-built engine, and neither one matches the accuracy that Senzing pulls off without breaking a sweat.

If you're prototyping on a small dataset (say, under 1,000 records) and you want a quick-and-dirty merge pass, sure, an LLM might be fun.  But the moment you're talking about production data, daily runs, audit requirements, or anything where someone's going to ask "are you absolutely sure these are actually the same person?"...the calculus changes fast.

I came into this experiment willing to be impressed by the LLM.  I came out of it impressed by Senzing instead.  The thing about purpose-built tools is that the years of engineering go somewhere, and in this case they go into being faster, cheaper, and more accurate than the shiny AI alternative.  

## Acknowledgements

I want to thank Paco Nathan, Jeff Butcher, and Brian Macy for their helpful discussions on this topic.

## References

1. Li, Y., Li, J., Suhara, Y., Doan, A., & Tan, W.-C.  *Deep Entity Matching with Pre-Trained Language Models* (Ditto).  arXiv:2004.00584, 2020.  Published in VLDB 2021.  [https://arxiv.org/abs/2004.00584](https://arxiv.org/abs/2004.00584)

2. Peeters, R., Steiner, A., & Bizer, C.  *Entity Matching using Large Language Models*.  arXiv:2310.11244, 2023.  [https://arxiv.org/abs/2310.11244](https://arxiv.org/abs/2310.11244)

3. *Match, Compare, or Select?  An Investigation of Large Language Models for Entity Matching.*  COLING 2025.  arXiv:2405.16884.  [https://aclanthology.org/2025.coling-main.8/](https://aclanthology.org/2025.coling-main.8/)

4. *Structured Multi-Step Reasoning for Entity Matching Using Large Language Model.*  arXiv:2511.22832, 2025.  [https://arxiv.org/abs/2511.22832](https://arxiv.org/abs/2511.22832)