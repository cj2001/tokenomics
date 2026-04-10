# Tokenomics: Comparing Entity Resolution Approaches

## How the Code Works

All of the entity resolution (ER) code lives in the [tokenomics/](tokenomics/) subdirectory. It implements two parallel pipelines that take the same input — JSONL records from multiple data sources — and produce the same Senzing-compatible output format, so the results from each pipeline can be measured against the other using a single reporting script.

The two approaches are:

1. **Senzing-based ER** — uses the Senzing SDK running against a local PostgreSQL backend to resolve entities deterministically using Senzing's rule engine.
2. **LLM-based ER** — sends the same records to Claude and asks the model to behave as an ER engine, returning results in the same schema.

Each approach has a loader/resolver script and a stats/reporting script.

---

### Approach 1: Senzing SDK

This pipeline is implemented in [tokenomics/load_data_local.py](tokenomics/load_data_local.py) and [tokenomics/merge_stats_local.py](tokenomics/merge_stats_local.py). Both scripts use the `senzing-core` package, which talks to a local Senzing engine configured against a Postgres database. The engine configuration is read from the `SENZING_ENGINE_CONFIGURATION_JSON` environment variable that is set by sourcing `senzing/setupEnv` before running the scripts.

#### Loading: `load_data_local.py`

The loader is responsible for getting raw JSONL records into Senzing so that the engine can resolve them. It does the work in three phases.

**1. Detect data sources from the input files.** Senzing requires every record to declare which `DATA_SOURCE` it belongs to (for example `EQUIFAX` or `NPI-PROVIDERS`), and the engine has to know about each source code before it will accept records from it. Rather than hard-coding source names, [detect_data_source()](tokenomics/load_data_local.py#L44) peeks at the first JSON line of each input file and reads the `DATA_SOURCE` field to discover what's there.

**2. Register any missing data sources.** [ensure_data_sources()](tokenomics/load_data_local.py#L59) opens Senzing's config manager, fetches the current default config, lists the data sources already registered in it, and adds any that the input files require. When new sources have to be added, the function exports a new config, registers it with `register_config()`, and atomically swaps it in as the new default with `replace_default_config_id()`. This is the standard Senzing config-update flow and it's important because adding a record to an unknown data source code will fail.

**3. Stream records into the engine.** [load_jsonl_file()](tokenomics/load_data_local.py#L96) reads each input file line by line, parses each line as JSON, and calls `sz_engine.add_record()` with the data source code, the record ID, and the full record body serialized back to JSON. Crucially, **Senzing does its entity resolution incrementally as each record is added** — there is no separate "resolve" step. Each `add_record` call causes the engine to compare the new record against existing entities, decide whether it merges into one of them or starts a new entity, and persist the result. The script tracks per-source counts, errors, and load rate, and prints a summary at the end including engine workload stats from `sz_engine.get_stats()`.

The output of this stage isn't a file — it's the state of the Senzing database itself. Resolved entities live inside Senzing until something asks for them, which is the job of the next script.

#### Reporting: `merge_stats_local.py`

This script reads the resolved entities back out of Senzing and produces an overlap report describing how the resolution turned out. It can also be pointed at a JSONL file via `--file` so it can score the LLM pipeline's output using the exact same logic — this is what makes the two approaches comparable. The script breaks naturally into seven steps, starting with how it bootstraps Senzing and ending with how it formats numbers for the screen.

**Step 1 — Parse arguments and choose a data source.** [parse_args()](tokenomics/merge_stats_local.py#L50) defines a single optional flag, `--file`. The presence of that flag is what selects between the two modes the script supports: live export from Senzing (the default) or replay from a previously written JSONL file. [main()](tokenomics/merge_stats_local.py#L294) is essentially a router on this flag — everything downstream of the iterator is identical regardless of where the entities come from.

**Step 2 — Resolve Senzing engine configuration (Senzing mode only).** When the script needs to talk to Senzing, [get_settings()](tokenomics/merge_stats_local.py#L31) builds the JSON configuration string the engine expects. It tries two sources in order:
1. The `SENZING_ENGINE_CONFIGURATION_JSON` environment variable, which is what `senzing/setupEnv` exports. If present, that string is used as-is.
2. Otherwise, it falls back to the path in `SENZING_CONFIG_FILE`. If that file exists, it is parsed as a `.ini` file with `configparser`. The `cfgp.optionxform = str` line is important — `configparser` lowercases keys by default, but Senzing's option names are case-sensitive, so this disables the lowercasing. The `[section]` blocks of the ini file are then converted into a nested dict (`{section: {key: value, …}, …}`) and `json.dumps`'d into the same format the env var would have provided.
3. If neither is available, the script prints an error pointing the user at `setupEnv` and exits with code 1.

**Step 3 — Initialize Senzing and set up the entity iterator.** Back in `main()`, the Senzing branch creates an `SzAbstractFactoryCore("merge_stats_local", settings)` and calls `create_engine()` on it to get a working engine handle. This is the local-SDK equivalent of opening a database connection — the factory loads the native libraries, reads the config, opens the Postgres connection pool, and hands back an engine object that can issue queries. The script then calls [export_entities(sz_engine)](tokenomics/merge_stats_local.py#L63), which is a generator: nothing actually runs yet. The wall-clock timer (`start = time.time()`) is started right before iteration begins so that the reported duration captures both the export and the in-pass aggregation.

**Step 4 — Stream the resolved entities out of Senzing.** [export_entities()](tokenomics/merge_stats_local.py#L63) is the function that actually pulls data from the engine. Step by step:
1. It picks `flags = SzEngineFlags.SZ_EXPORT_DEFAULT_FLAGS`. This is Senzing's standard "give me everything" bitmask for an entity report — it includes the resolved entity, its records, the match keys and rule codes for each record, and the related-entity block.
2. `handle = sz_engine.export_json_entity_report(flags)` opens a server-side cursor and returns an opaque handle. No data has crossed the wire yet — Senzing is just preparing to stream.
3. Inside a `try` block, the function enters a `while True` loop. Each iteration calls `sz_engine.fetch_next(handle)`, which returns the next entity as a JSON string, or an empty string when the cursor is exhausted. An empty/falsy return value breaks the loop.
4. The returned line is `.strip()`'d and skipped if it ended up empty (defensive against any blank lines).
5. The line is parsed with `json.loads`. A `JSONDecodeError` is silently swallowed via `continue` — the export can occasionally emit framing rows the script doesn't care about, and skipping is safer than crashing the whole report.
6. The parsed dict is filtered: only objects that contain a top-level `"RESOLVED_ENTITY"` key are yielded. This drops any non-entity rows the export may produce.
7. The whole loop is wrapped in `try/finally` so that `sz_engine.close_export_report(handle)` is always called, even if downstream code raises. Leaking export handles inside Senzing is a real problem if the script is re-run repeatedly.

The result is a generator that produces one entity dict per `next()`, never holding more than one entity in memory at a time. This matters because a real Senzing database can have millions of entities, and a non-streaming approach would be impractical.

**Step 5 — Stream entities out of a file (file mode).** The mirror image of `export_entities()` is [entities_from_file()](tokenomics/merge_stats_local.py#L85). It opens the JSONL file, iterates line by line, strips and skips blanks, parses each line as JSON (silently swallowing parse errors with `continue`), and yields any object containing `RESOLVED_ENTITY`. By having both the Senzing path and the file path produce the same kind of generator with the same shape of yielded dicts, the rest of the script literally cannot tell which one it is reading from. This is what lets the LLM output be scored with the exact same code that scores Senzing.

**Step 6 — Build statistics in a single pass.** [build_stats(entity_iter)](tokenomics/merge_stats_local.py#L100) is where every metric the report shows gets computed. It is deliberately written as a single sweep over the iterator so that nothing has to be re-walked.

It begins by creating eight accumulators:
- `ds_record_count = Counter()` — records per data source.
- `ds_entity_count = Counter()` — entities that contain at least one record from each data source.
- `cross_source_entities = defaultdict(set)` — keyed by `(source_a, source_b)` tuples, holding the set of entity IDs that contain records from both sources.
- `entity_sizes = []` — a flat list of how many records each entity contains.
- `relation_type_counts = Counter()` and `total_relations = 0` — for the related-entities block.
- `match_key_counter = Counter()` and `errule_counter = Counter()` — frequency tables for the match-key and rule-code annotations on each merged record.
- `entity_count = 0` — running total of resolved entities.

Then, for each entity in the iterator, it does the following sub-steps:

1. Pulls the `RESOLVED_ENTITY` block out, grabs the `ENTITY_ID`, grabs the `RECORDS` list, and measures `record_count = len(records)`.
2. Bumps `entity_count += 1` and appends `record_count` to `entity_sizes`. After the loop ends, `entity_sizes` will be a list of integers like `[1, 1, 2, 1, 5, 1, …]` ready for size bucketing.
3. Creates an empty `sources_in_entity = set()` for this entity.
4. Walks each record in the entity's `RECORDS` list:
   - Reads `DATA_SOURCE` (defaulting to `"UNKNOWN"`) and increments `ds_record_count[ds]` by one. Every record is counted once here, so summing this Counter gives the total number of input records.
   - Adds `ds` to `sources_in_entity`. Using a set means each source contributes only once per entity, regardless of how many records from that source are inside.
   - Reads `MATCH_KEY` (e.g. `+NAME+ADDRESS+PHONE`) and `ERRULE_CODE` (e.g. `SAME_A2`). Both are blank strings on the *anchor* record of an entity — the anchor doesn't need a match key because it didn't merge with anything to get there. Only non-empty strings are counted, so anchors and singletons are correctly excluded from the match-key and rule-code histograms.
5. After the per-record loop, it walks `sources_in_entity` and bumps `ds_entity_count[ds] += 1` once per unique source. Important consequence: an entity that contains both `EQUIFAX` and `NPI-PROVIDERS` records is counted in *both* source totals, so summing `ds_entity_count` produces a number larger than `entity_count` whenever cross-source overlap exists. That is intentional — it's what lets the per-source compression ratio be meaningful.
6. To compute cross-source overlap, the function sorts `sources_in_entity` (so pairs come out in deterministic order, e.g. always `("EQUIFAX", "NPI-PROVIDERS")` and never `("NPI-PROVIDERS", "EQUIFAX")`) and then enumerates every unordered pair using a nested `for i / for j in range(i+1, …)` loop. For each pair, the entity's ID is added to `cross_source_entities[(src_a, src_b)]`. Storing a *set* of IDs (not a count) means the same entity cannot be double-counted if processing logic ever revisits it, and it allows the report to print exact shared-entity totals.
7. Finally, it walks `entity.get("RELATED_ENTITIES", [])`. Each related-entity block has a `MATCH_LEVEL_CODE` (with a fallback to the older `MATCH_TYPE` field for compatibility with older Senzing exports). The code is incremented in `relation_type_counts`, and `total_relations` is bumped. Note this counts each *side* of each relationship separately — relationships in Senzing are stored symmetrically (entity A points at B, and B points at A), so the totals roughly double-count actual physical relationships, which is fine for showing relative proportions.

When the loop ends, `build_stats` returns a single dict bundling all eight accumulators so the next step has everything it needs.

**Step 7 — Print the report.** [print_report(stats, duration=None)](tokenomics/merge_stats_local.py#L185) is the formatting layer. It does no math beyond simple percentages and the size-bucket assignment, but the percentages themselves are worth understanding.

1. It computes three top-line numbers: `total_records = sum(ds_record_count.values())`, `total_entities = stats["entity_count"]`, and `merged_records = total_records - total_entities`. The last one deserves a moment: every merge collapses N input records into 1 entity, so it removes `N - 1` from the entity count relative to the record count. Summing those reductions across all merges is exactly `total_records - total_entities`, which is therefore the number of input records that ended up sharing an entity with at least one other record. The "overall merge rate" prints `100 * merged_records / total_records`.
2. **DATA SOURCE SUMMARY.** For each source (sorted alphabetically), it pulls `recs = ds_record_count[ds]` and `ents = ds_entity_count[ds]` and computes a compression ratio of `recs / ents`. A ratio of `1.00x` means no records from that source merged together (every record is its own entity); higher ratios indicate within-source duplication being collapsed.
3. **CROSS-SOURCE OVERLAP.** Only printed if `cross_source_entities` is non-empty. For each `(src_a, src_b)` pair (sorted), it gets `shared = len(cross_source_entities[pair])` and divides by each source's individual entity count to get `pct_a` and `pct_b`. These percentages answer the practical question, "of all the entities I have in source A, what fraction are also represented in source B?", which is the most direct measurement of how much the two data sources actually overlap. If there is only one data source in the data, the section is replaced by a one-line note.
4. **ENTITY SIZE DISTRIBUTION.** The script re-walks `entity_sizes` once, calling [size_bucket(n)](tokenomics/merge_stats_local.py#L161) on each value. `size_bucket()` is a small mapping: `1` is "singletons", `2` is "pairs", then `3-5`, `6-10`, `11-50`, and `51+` (the last is labeled "review" because anything that big is suspicious and worth eyeballing manually for over-merging). Two counters are accumulated: `bucket_counts` (number of entities in each bucket) and `bucket_records` (sum of record counts in each bucket). Iteration order is forced to the constant `BUCKET_ORDER` so the report always reads top-down from singletons to giant clusters.
5. **RELATIONSHIP SUMMARY.** Only printed if `total_relations > 0`. The four short codes (`PM`, `AM`, `DR`, `PR`) are mapped to their human-readable labels via a lookup table, and `most_common()` iterates the relationship counter in descending order with percentages computed against `total_relations`.
6. **TOP MATCH KEYS.** Iterates `match_key_counter.most_common(15)` and shows percentages relative to the sum of all match key occurrences. The match keys (e.g. `+NAME+ADDRESS`, `+NAME+PHONE`, `+NPI`) are the short feature-combination strings that Senzing attaches to a record when it merges, so this section is the closest thing the report has to an answer for *why* the merges happened.
7. **ER RULE USAGE.** Same idea but for the `ERRULE_CODE` counter, with `most_common()` (no limit). The rule codes describe the *strength tier* of each merge (e.g. `SAME_A1` for strong-name + strong-identifier matches), so this section answers *how confidently* the merges were made.
8. If a `duration` was passed in (which `main()` does in both branches), the top-line summary also prints "Report generated in: X.XXs".

The end result is a single command — `python merge_stats_local.py` — that prints a complete picture of how Senzing resolved the loaded data, with everything traceable back to the per-entity numbers that produced it.

---

### Approach 2: LLM-Based ER with Claude

The second pipeline is implemented in [tokenomics/llm_er.py](tokenomics/llm_er.py) and reuses [tokenomics/merge_stats.py](tokenomics/merge_stats.py) for reporting. The goal here is to have a large language model (Claude Opus) play the role of an ER engine and produce output in the *same* shape that Senzing would, so the same stats script can score it. There is no database — the LLM looks at the raw records and decides which ones go together.

#### Resolution: `llm_er.py`

This is the heart of the LLM approach. The script takes two input files (JSONL or CSV from any of the same data sources used by Approach 1), prompts Claude to perform entity resolution on them, and writes a Senzing-compatible JSONL file of resolved entities. It has to solve four interesting problems along the way: framing the task, fitting it into the context window, parsing and validating the model's output, and reconciling results across batches.

**1. Framing the task with a careful system prompt.** The [SYSTEM_PROMPT](tokenomics/llm_er.py#L42) is the most important piece of the script. It instructs the model to act as "an expert entity resolution engine" and walks it through the same conceptual process a real ER system uses: ingest records, compare features (names, addresses, phones, emails, identifiers, employers, gender), assign match-key codes (`+NAME`, `+ADDRESS`, `+PHONE`, etc.), assign ER rule codes (`SAME_A1`, `SAME_A2`, …) that mirror Senzing's strength tiers, and identify related-but-not-same entities using the same `PM` / `AM` / `DR` / `PR` codes Senzing uses. It also nails down a strict JSON output schema with two top-level keys, `MERGED_ENTITIES` and `RELATIONSHIPS`, and tells the model to return *only* groups of 2+ records that resolved together — singletons are explicitly excluded to save output tokens. The prompt ends with an "absolute requirement" that the response start with `{` and end with `}` and contain no commentary, because any preamble or markdown fence wastes output tokens and risks breaking the parser.

**2. Loading and serializing records as CSV.** [load_records()](tokenomics/llm_er.py#L201) accepts either JSONL or CSV input. Once records are in memory they are serialized for the model using [records_to_csv()](tokenomics/llm_er.py#L248), which writes one CSV table per data source with a single header row. Sending data as CSV instead of JSON is a deliberate optimization: a CSV row has no repeated keys, so the per-record token cost is dramatically lower than the equivalent JSON, and that directly reduces both API cost and the chance of overflowing the context window.

**3. Batching to fit a token budget.** Real input files are often too large for a single API call, both because of context-window limits and because the user's per-minute input-token rate limit is finite. [build_batches()](tokenomics/llm_er.py#L291) handles this. It groups records by source, then *interleaves* them across sources so that every batch contains a mix from every input file — this is critical, because cross-source matches are the whole point of the exercise, and a batch containing only one source can't produce them. It uses `tiktoken` (`cl100k_base`) to estimate the token cost of the system prompt, the boilerplate of the user message, and each record's CSV row, and packs records into batches that stay under the configurable `--token-budget` (default 25,000 input tokens per batch, well under typical org rate limits).

**4. Calling the model with retries.** [call_llm_with_retry()](tokenomics/llm_er.py#L363) wraps each request to the Anthropic API with streaming enabled (so partial output isn't lost on parse failures) and exponential backoff on rate-limit errors (`anthropic.RateLimitError`) and 5xx server errors. Per-batch processing happens in [process_single_batch()](tokenomics/llm_er.py#L397), which builds the user message, calls the model, parses the response, and reports per-batch stats (number of merges, number of relationships, input/output tokens). [parse_response()](tokenomics/llm_er.py#L342) is defensive: it strips markdown fences if the model produced any, tries `json.loads()` directly, and otherwise falls back to extracting the substring between the first `{` and the last `}`.

**5. Cross-batch consolidation.** Batching introduces a real correctness problem: a person whose Equifax record lands in batch 1 and whose NPI record lands in batch 4 will never be compared to themselves. To fix this, after every batch has been processed, [consolidate_cross_batch()](tokenomics/llm_er.py#L431) runs a second pass. It assigns each batch-merged entity a temporary ID, sends Claude a compact summary of every merged entity (just the entity name and its constituent `DATA_SOURCE:RECORD_ID` pairs), and asks the model to identify groups of entity IDs that should be combined. A different system prompt, [CONSOLIDATION_SYSTEM_PROMPT](tokenomics/llm_er.py#L151), is used for this pass so the model knows it's being asked to merge entities (not raw records). [apply_cross_batch_merges()](tokenomics/llm_er.py#L492) then takes those merge groups and combines the constituent entities, choosing the first record as the anchor (empty `MATCH_KEY` and `ERRULE_CODE`) and assigning a default `+NAME` / `SAME_B2` to records that hadn't already been annotated. Entities that weren't part of any cross-batch group are passed through unchanged.

**6. Adding singletons and emitting Senzing-compatible JSON.** Recall that the model is instructed to return *only* multi-record entities, in order to save output tokens. [build_full_entity_list()](tokenomics/llm_er.py#L546) puts the singletons back. It walks the LLM-merged entities first, deduplicates any record that the model accidentally placed in two different entities (logging warnings if so), assigns sequential `ENTITY_ID` values, and wraps each one in a Senzing-style `RESOLVED_ENTITY` block. It then walks the original input record list and emits a singleton entity for any record that wasn't claimed by a merge. Finally, it maps the LLM's `RELATIONSHIPS` array onto the new entity IDs by looking up the entity each referenced `RECORD_ID` belongs to, and writes symmetric `RELATED_ENTITIES` entries on both sides.

**7. Cost accounting.** Throughout the run, the script tracks actual input and output tokens reported by the API and multiplies them by Claude Opus 4.6 pricing (`INPUT_COST_PER_MTOK = $5.00`, `OUTPUT_COST_PER_MTOK = $25.00`) to print an end-of-run cost summary. Before the run starts, it also prints an *estimate* of total cost based on the sum of system-prompt + user-message tokens for every batch and asks for explicit user confirmation (`yes/no`) before sending anything to the API. This is the script that gives the project its "tokenomics" name — every batch is measured both for what it costs in dollars and how its cost changes with batch size and budget.

The output is a JSONL file (default `data/llm_er_output.jsonl`) where every line is a `RESOLVED_ENTITY` with `RELATED_ENTITIES` — exactly the format that comes out of `Senzing's export_json_entity_report`.

#### Reporting: `merge_stats.py`

The reporting step for the LLM pipeline is [tokenomics/merge_stats.py](tokenomics/merge_stats.py). It is the gRPC sibling of `merge_stats_local.py` — every function that does math (`build_stats`, `size_bucket`, `print_report`, `entities_from_file`) is byte-for-byte equivalent. What changes is everything around the engine handle: where the connection comes from, how it's opened, and what configuration is needed. Walking through the differences step by step:

**Step 1 — Argument parsing adds host/port flags.** [parse_args()](tokenomics/merge_stats.py#L29) keeps the same `--file` flag that the local version has, but also accepts `--host` and `--port` for the Senzing gRPC server. Both default to environment variables (`SENZING_GRPC_HOST` and `SENZING_GRPC_PORT`) and then to `localhost:8261`. This is what lets the same script point at a containerized Senzing instance, a remote one, or a local dev server without code changes.

**Step 2 — No engine configuration JSON is needed.** Notice that there is no `get_settings()` function in this script. The reason is that with gRPC, the *server* holds the engine configuration — the client just opens a socket and asks the server for entities. That eliminates the env-var/ini-file dance and makes the script much simpler to invoke: you don't need to source `setupEnv` first.

**Step 3 — Open a gRPC channel and create a factory.** Inside [main()](tokenomics/merge_stats.py#L301), the Senzing branch does three lines of setup:
1. `grpc_channel = grpc.insecure_channel(f"{args.host}:{args.port}")` — opens an unauthenticated gRPC channel to the Senzing server. "Insecure" here means no TLS, which is the right choice for a server reachable on `localhost` or inside a private network. Production deployments would swap this for `grpc.secure_channel(...)` with credentials.
2. `sz_abstract_factory = SzAbstractFactoryGrpc(grpc_channel)` — wraps the channel in Senzing's gRPC factory, which knows how to translate `senzing` SDK calls into gRPC requests.
3. `sz_engine = sz_abstract_factory.create_engine()` — gets a remote engine handle. From this point on, every method call on `sz_engine` (like `export_json_entity_report` or `fetch_next`) is a gRPC RPC under the hood, but the call signatures are identical to the local SDK so the rest of the code can stay the same.

**Step 4 — Stream entities through the same `export_entities()` generator.** [export_entities(sz_engine)](tokenomics/merge_stats.py#L52) is the same function as in the local script: open the export with `SZ_EXPORT_DEFAULT_FLAGS`, loop on `fetch_next(handle)`, parse each line, filter for `RESOLVED_ENTITY`, and close the handle in a `finally`. The only thing that has changed is what's *behind* `sz_engine` — instead of in-process C++ calls, each `fetch_next` is a gRPC round-trip to the server. Streaming still works because gRPC handles the cursor on the server side; the client just keeps asking for more.

**Step 5 — File mode is identical.** [entities_from_file()](tokenomics/merge_stats.py#L74) is the same line-by-line JSONL reader as in `merge_stats_local.py`, with the same `RESOLVED_ENTITY` filter. This is the path that scores the LLM pipeline. Because [llm_er.py](tokenomics/llm_er.py) emits records that follow the same `RESOLVED_ENTITY` / `RECORDS` / `MATCH_KEY` / `ERRULE_CODE` / `RELATED_ENTITIES` schema as Senzing's export, the file iterator drops them straight into the shared aggregator without any transformation:

```
python merge_stats.py --file data/llm_er_output.jsonl
```

**Step 6 — `build_stats()` and `print_report()` are byte-for-byte the shared logic.** [build_stats()](tokenomics/merge_stats.py#L89) and [print_report()](tokenomics/merge_stats.py#L187) in this script are identical to their counterparts in `merge_stats_local.py`. Every accumulator, every loop, every percentage calculation described in the seven steps above applies here unchanged: same per-source counters, same cross-source overlap pair logic, same `size_bucket` mapping with the same `BUCKET_ORDER` constant, same match-key and ER-rule histograms, same anchor-record exclusion via the empty-string check, same symmetric relationship counting. The only stylistic difference is that this version of `print_report()` does not accept a `duration` parameter, because `main()` here doesn't time the build — but the report it produces has the same headings, the same columns, and the same numbers given the same input.

That is the entire point of the design: the loader differs, the resolver differs, the engine handle differs, but the output schema is shared and the scoring code is shared. When you run `merge_stats_local.py` on a freshly-loaded Senzing database and then run `merge_stats.py --file data/llm_er_output.jsonl` on the LLM output of the same input data, the two reports are directly comparable line by line — same compression ratio columns, same cross-source overlap percentages, same size-bucket histogram, same top match keys, same ER rule usage breakdown. Any difference between them is therefore a real difference in *how* each approach resolved entities, not an artifact of how they were measured.
