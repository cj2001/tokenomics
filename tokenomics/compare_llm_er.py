#!/usr/bin/env python3
"""
compare_llm_er.py — Compare LLM entity resolution output against Senzing (ground truth).

Mirrors the analysis in 09_er_comparison.ipynb but adapted for:
  - Local Senzing PostgreSQL (not Docker)
  - LLM ER results in JSONL format (not Excel)
  - EQUIFAX / NPI-PROVIDERS data sources

Usage:
    python compare_llm_er.py data/llm_er_output.jsonl
    python compare_llm_er.py data/llm_er_output.jsonl \
        --host localhost --port 5432 --db tokenomics \
        --user postgres --password workshop

Results are printed to stdout and saved as PNG charts alongside the JSONL file.
"""

import argparse
import json
import os
import re
import sys
import warnings
from collections import Counter, defaultdict
from itertools import combinations

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import psycopg2
import psycopg2.extras

matplotlib.rcParams["figure.figsize"] = (12, 6)
matplotlib.rcParams["font.size"] = 11
warnings.filterwarnings("ignore")


# ─────────────────────────────────────────────────────────────
# 1. Data loading
# ─────────────────────────────────────────────────────────────

def connect_postgres(host, port, dbname, user, password):
    return psycopg2.connect(
        host=host, port=port, dbname=dbname, user=user, password=password,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def load_senzing(conn):
    """
    Pull every record → resolved-entity mapping from Senzing's PostgreSQL schema.
    Returns a list of dicts with keys: record_id, data_source, entity_id,
    match_key, name.
    """
    sql = """
        SELECT
            r.record_id,
            r.json_data,
            m.res_ent_id  AS entity_id,
            m.match_key
        FROM  res_ent_okey m
        JOIN  obs_ent      o ON m.obs_ent_id  = o.obs_ent_id
        JOIN  dsrc_record  r ON o.dsrc_id     = r.dsrc_id
                            AND o.ent_src_key = r.ent_src_key
        ORDER BY m.res_ent_id, r.record_id
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    records = []
    for row in rows:
        jd = json.loads(row["json_data"]) if isinstance(row["json_data"], str) else row["json_data"]
        data_source = jd.get("DATA_SOURCE", "UNKNOWN")
        name = _extract_name(jd)
        records.append({
            "record_id":   str(row["record_id"]),
            "data_source": data_source,
            "entity_id":   int(row["entity_id"]),
            "match_key":   row["match_key"] or "",
            "name":        name,
            # composite key used for cross-source safety
            "key":         f"{data_source}:{row['record_id']}",
        })
    return records


def _extract_name(jd):
    """Best-effort name extraction from a Senzing JSON record."""
    # EQUIFAX-style: top-level fields
    if "PRIMARY_NAME_FIRST" in jd and "PRIMARY_NAME_LAST" in jd:
        parts = [
            jd.get("PRIMARY_NAME_PREFIX", ""),
            jd["PRIMARY_NAME_FIRST"],
            jd.get("PRIMARY_NAME_MIDDLE", ""),
            jd["PRIMARY_NAME_LAST"],
        ]
        return " ".join(p for p in parts if p)
    if "NAME_FIRST" in jd and "NAME_LAST" in jd:
        return f"{jd['NAME_FIRST']} {jd['NAME_LAST']}".strip()
    if "NAME_FULL" in jd:
        return jd["NAME_FULL"]
    if "NAME_ORG" in jd:
        return jd["NAME_ORG"]
    # NPI / nested FEATURES list
    for feat in jd.get("FEATURES", []):
        if "PRIMARY_NAME_LAST" in feat:
            return f"{feat.get('PRIMARY_NAME_FIRST', '')} {feat['PRIMARY_NAME_LAST']}".strip()
        if "NAME_LAST" in feat:
            return f"{feat.get('NAME_FIRST', '')} {feat['NAME_LAST']}".strip()
        if "NAME_FULL" in feat:
            return feat["NAME_FULL"]
        if "NAME_ORG" in feat:
            return feat["NAME_ORG"]
    return str(jd.get("RECORD_ID", "UNKNOWN"))


def load_llm_results(jsonl_path):
    """
    Load LLM ER output JSONL (produced by llm_er.py).
    Returns (record_to_cluster, record_to_match_key, relationships).

    record_to_cluster maps  "DATA_SOURCE:RECORD_ID"  ->  int entity_id
    record_to_match_key maps the same key            ->  str match_key
    relationships is a list of dicts from RELATED_ENTITIES
    """
    record_to_cluster = {}
    record_to_match_key = {}
    relationships = []

    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            entity = obj.get("RESOLVED_ENTITY", {})
            eid = int(entity["ENTITY_ID"])
            for rec in entity.get("RECORDS", []):
                key = f"{rec['DATA_SOURCE']}:{rec['RECORD_ID']}"
                record_to_cluster[key] = eid
                record_to_match_key[key] = rec.get("MATCH_KEY", "")
            for rel in obj.get("RELATED_ENTITIES", []):
                relationships.append(rel)

    return record_to_cluster, record_to_match_key, relationships


def load_senzing_relationships(conn):
    """Load entity-level relationships from res_relate."""
    sql = "SELECT min_res_ent_id, max_res_ent_id, is_disclosed, is_ambiguous, match_key FROM res_relate"
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


# ─────────────────────────────────────────────────────────────
# 2. Metrics helpers
# ─────────────────────────────────────────────────────────────

def get_merge_pairs(record_to_cluster):
    """All (sorted) record-key pairs that share a cluster."""
    cluster_to_records = defaultdict(set)
    for key, cid in record_to_cluster.items():
        cluster_to_records[cid].add(key)
    pairs = set()
    for records in cluster_to_records.values():
        if len(records) > 1:
            for r1, r2 in combinations(sorted(records), 2):
                pairs.add((r1, r2))
    return pairs


def bcubed_metrics(truth_map, pred_map, records):
    truth_clusters = defaultdict(set)
    pred_clusters = defaultdict(set)
    for r in records:
        truth_clusters[truth_map[r]].add(r)
        pred_clusters[pred_map[r]].add(r)

    precisions, recalls = [], []
    for r in records:
        pc = pred_clusters[pred_map[r]]
        tc = truth_clusters[truth_map[r]]
        precisions.append(len(pc & tc) / len(pc))
        recalls.append(len(tc & pc) / len(tc))

    avg_p = np.mean(precisions)
    avg_r = np.mean(recalls)
    f1 = 2 * avg_p * avg_r / (avg_p + avg_r) if (avg_p + avg_r) > 0 else 0
    return avg_p, avg_r, f1, precisions, recalls


def safe_f1(p, r):
    return 2 * p * r / (p + r) if (p + r) > 0 else 0


def pairwise_metrics(tp, fp, fn, total_pairs):
    precision = len(tp) / (len(tp) + len(fp)) if (len(tp) + len(fp)) > 0 else 0
    recall    = len(tp) / (len(tp) + len(fn)) if (len(tp) + len(fn)) > 0 else 0
    f1        = safe_f1(precision, recall)
    tn        = total_pairs - len(tp) - len(fp) - len(fn)
    accuracy  = (len(tp) + tn) / total_pairs if total_pairs > 0 else 0
    return precision, recall, f1, accuracy, tn


# ─────────────────────────────────────────────────────────────
# 3. Main analysis
# ─────────────────────────────────────────────────────────────

def run_comparison(args):
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "figures")
    os.makedirs(out_dir, exist_ok=True)

    # ── Load data ──────────────────────────────────────────────
    print("Connecting to PostgreSQL…")
    conn = connect_postgres(args.host, args.port, args.db, args.user, args.password)

    print("Loading Senzing results (ground truth)…")
    sz_records = load_senzing(conn)
    sz_rels_raw = load_senzing_relationships(conn)
    conn.close()

    print(f"  Senzing: {len(sz_records)} records → "
          f"{len({r['entity_id'] for r in sz_records})} entities")

    print("Loading LLM ER results…")
    llm_cluster, llm_match_key, llm_rels = load_llm_results(args.jsonl)
    print(f"  LLM ER: {len(llm_cluster)} records → "
          f"{len(set(llm_cluster.values()))} entities")

    # ── Build unified record universe ──────────────────────────
    # Use "DATA_SOURCE:RECORD_ID" composite keys throughout
    sz_map = {}          # key -> entity_id
    record_meta = {}     # key -> {data_source, name, match_key}

    for r in sz_records:
        sz_map[r["key"]] = r["entity_id"]
        record_meta[r["key"]] = {
            "data_source": r["data_source"],
            "name":        r["name"],
            "sz_mk":       r["match_key"],
        }

    # Common records (must exist in both systems to compare fairly)
    common_keys = set(sz_map.keys()) & set(llm_cluster.keys())
    sz_only     = set(sz_map.keys()) - common_keys
    llm_only    = set(llm_cluster.keys()) - common_keys

    print(f"\n  Common records:       {len(common_keys)}")
    if sz_only:
        print(f"  Senzing-only records: {len(sz_only)}")
    if llm_only:
        print(f"  LLM-only records:     {len(llm_only)}")

    # Add singletons for LLM records missing from llm_cluster
    max_llm_id = max(llm_cluster.values()) if llm_cluster else 0
    for i, key in enumerate(sorted(set(sz_map.keys()) - set(llm_cluster.keys())),
                              start=max_llm_id + 1):
        llm_cluster[key] = i

    # Filter both maps to common keys only
    sz_filtered  = {k: sz_map[k]      for k in common_keys}
    llm_filtered = {k: llm_cluster[k] for k in common_keys}

    sz_sources = sorted({v["data_source"] for v in record_meta.values()})
    print(f"\n  Data sources: {', '.join(sz_sources)}")

    # ── 4. Pairwise precision / recall / F1 ───────────────────
    print("\n" + "=" * 62)
    print("SECTION 1 — PAIRWISE MERGE QUALITY")
    print("=" * 62)

    sz_pairs  = get_merge_pairs(sz_filtered)
    llm_pairs = get_merge_pairs(llm_filtered)

    tp = sz_pairs & llm_pairs
    fp = llm_pairs - sz_pairs
    fn = sz_pairs  - llm_pairs
    total_pairs = len(common_keys) * (len(common_keys) - 1) // 2
    precision, recall, f1, accuracy, tn = pairwise_metrics(tp, fp, fn, total_pairs)

    print(f"\n  Senzing merge pairs (ground truth):  {len(sz_pairs):>7,}")
    print(f"  LLM ER merge pairs:                  {len(llm_pairs):>7,}")
    print(f"  Total possible record pairs:         {total_pairs:>7,}")
    print(f"\n  Confusion Matrix")
    print(f"  {'─'*38}")
    print(f"    True Positives  (both merge):   {len(tp):>6,}")
    print(f"    False Positives (LLM only):     {len(fp):>6,}")
    print(f"    False Negatives (Senzing only): {len(fn):>6,}")
    print(f"    True Negatives  (both split):   {tn:>6,}")
    print(f"\n  Precision:  {precision:.4f}  (of LLM's merges, how many are correct)")
    print(f"  Recall:     {recall:.4f}  (of Senzing's merges, how many LLM found)")
    print(f"  F1 Score:   {f1:.4f}")
    print(f"  Accuracy:   {accuracy:.6f}")

    # ── 5. B-Cubed ────────────────────────────────────────────
    print("\n" + "=" * 62)
    print("SECTION 2 — B-CUBED CLUSTERING METRICS")
    print("=" * 62)

    bcubed_p, bcubed_r, bcubed_f1, per_p, per_r = bcubed_metrics(
        sz_filtered, llm_filtered, common_keys
    )
    print(f"\n  B-Cubed Precision:  {bcubed_p:.4f}")
    print(f"  B-Cubed Recall:     {bcubed_r:.4f}")
    print(f"  B-Cubed F1:         {bcubed_f1:.4f}")
    print(f"\n  Per-record precision  min={np.min(per_p):.3f} "
          f"med={np.median(per_p):.3f} max={np.max(per_p):.3f}")
    print(f"  Per-record recall     min={np.min(per_r):.3f} "
          f"med={np.median(per_r):.3f} max={np.max(per_r):.3f}")

    # ── 6. Cluster-size distribution ──────────────────────────
    sz_cluster_sizes  = Counter(sz_filtered.values())
    llm_cluster_sizes = Counter(llm_filtered.values())
    sz_sizes  = list(sz_cluster_sizes.values())
    llm_sizes = list(llm_cluster_sizes.values())

    print("\n" + "=" * 62)
    print("SECTION 3 — CLUSTER SIZE DISTRIBUTION")
    print("=" * 62)
    print(f"\n  {'Metric':<32} {'Senzing':>10} {'LLM ER':>10}")
    print(f"  {'─'*52}")
    for label, sfn, afn in [
        ("Total clusters",        lambda s: len(s), lambda s: len(s)),
        ("Singletons",            lambda s: s.count(1), lambda s: s.count(1)),
        ("Multi-record clusters", lambda s: sum(1 for x in s if x > 1),
                                  lambda s: sum(1 for x in s if x > 1)),
        ("Records in merges",     lambda s: sum(x for x in s if x > 1),
                                  lambda s: sum(x for x in s if x > 1)),
        ("Largest cluster",       max, max),
        ("Mean cluster size",     lambda s: f"{np.mean(s):.2f}",
                                  lambda s: f"{np.mean(s):.2f}"),
    ]:
        print(f"  {label:<32} {str(sfn(sz_sizes)):>10} {str(afn(llm_sizes)):>10}")

    # ── 7. Cross-source vs within-source ──────────────────────
    def classify_pair(pair):
        s1 = record_meta.get(pair[0], {}).get("data_source", "?")
        s2 = record_meta.get(pair[1], {}).get("data_source", "?")
        return "cross-source" if s1 != s2 else "within-source"

    print("\n" + "=" * 62)
    print("SECTION 4 — CROSS-SOURCE vs WITHIN-SOURCE")
    print("=" * 62)

    cross_results = {}
    for cat in ("cross-source", "within-source"):
        cat_tp = {p for p in tp if classify_pair(p) == cat}
        cat_fp = {p for p in fp if classify_pair(p) == cat}
        cat_fn = {p for p in fn if classify_pair(p) == cat}
        p_ = len(cat_tp) / (len(cat_tp) + len(cat_fp)) if (len(cat_tp) + len(cat_fp)) > 0 else 0
        r_ = len(cat_tp) / (len(cat_tp) + len(cat_fn)) if (len(cat_tp) + len(cat_fn)) > 0 else 0
        cross_results[cat] = dict(TP=len(cat_tp), FP=len(cat_fp), FN=len(cat_fn),
                                   Precision=p_, Recall=r_, F1=safe_f1(p_, r_),
                                   sz_pairs=len(cat_tp)+len(cat_fn),
                                   llm_pairs=len(cat_tp)+len(cat_fp))

    for cat, r in cross_results.items():
        print(f"\n  [{cat.upper()}]")
        print(f"    Senzing pairs: {r['sz_pairs']:>5}  LLM pairs: {r['llm_pairs']:>5}")
        print(f"    TP {r['TP']:>4}  FP {r['FP']:>4}  FN {r['FN']:>4}")
        print(f"    Precision {r['Precision']:.4f}  Recall {r['Recall']:.4f}  F1 {r['F1']:.4f}")

    # ── 8. Entity-level agreement ─────────────────────────────
    print("\n" + "=" * 62)
    print("SECTION 5 — ENTITY-LEVEL AGREEMENT")
    print("=" * 62)

    sz_clusters_inv  = defaultdict(set)
    llm_clusters_inv = defaultdict(set)
    for k in common_keys:
        sz_clusters_inv[sz_filtered[k]].add(k)
        llm_clusters_inv[llm_filtered[k]].add(k)

    entity_analysis = []
    for sz_cid, sz_recs in sz_clusters_inv.items():
        llm_cids = {llm_filtered[r] for r in sz_recs}
        if len(llm_cids) == 1:
            llm_recs = llm_clusters_inv[next(iter(llm_cids))]
            if llm_recs == sz_recs:
                cat = "exact_match"
            elif llm_recs.issuperset(sz_recs):
                cat = "superset"
            else:
                cat = "partial"
        else:
            has_extra = any(llm_clusters_inv[cid] - sz_recs for cid in llm_cids)
            cat = "split_and_mixed" if has_extra else "split"

        sample = next(iter(sz_recs))
        entity_analysis.append({
            "sz_entity": sz_cid,
            "sz_size":   len(sz_recs),
            "llm_cids":  len(llm_cids),
            "category":  cat,
            "name":      record_meta.get(sample, {}).get("name", "?"),
        })

    multi = [e for e in entity_analysis if e["sz_size"] > 1]
    singletons_ea = [e for e in entity_analysis if e["sz_size"] == 1]

    print(f"\n  Senzing singletons: {len(singletons_ea)}")
    sing_exact = sum(1 for e in singletons_ea if e["category"] == "exact_match")
    sing_super = sum(1 for e in singletons_ea if e["category"] == "superset")
    print(f"    Correctly kept separate: {sing_exact}")
    print(f"    Incorrectly merged by LLM: {sing_super}")

    print(f"\n  Senzing multi-record entities: {len(multi)}")
    for cat in ("exact_match", "split", "superset", "split_and_mixed", "partial"):
        count = sum(1 for e in multi if e["category"] == cat)
        if count > 0:
            pct = 100 * count / len(multi) if multi else 0
            print(f"    {cat.replace('_',' ').title():<22} {count:>4}  ({pct:.1f}%)")

    # ── 9. False negatives ────────────────────────────────────
    print("\n" + "=" * 62)
    print("SECTION 6 — FALSE NEGATIVES (missed by LLM, found by Senzing)")
    print("=" * 62)

    fn_rows = []
    for r1, r2 in sorted(fn):
        m1 = record_meta.get(r1, {})
        m2 = record_meta.get(r2, {})
        fn_rows.append({
            "Name 1":    m1.get("name", r1)[:30],
            "Source 1":  m1.get("data_source", "?"),
            "Name 2":    m2.get("name", r2)[:30],
            "Source 2":  m2.get("data_source", "?"),
            "Cross":     m1.get("data_source") != m2.get("data_source"),
        })

    cross_fn = sum(1 for r in fn_rows if r["Cross"])
    print(f"\n  Total missed merges: {len(fn_rows)}")
    print(f"    Cross-source:    {cross_fn}")
    print(f"    Within-source:   {len(fn_rows) - cross_fn}")
    if fn_rows:
        print(f"\n  Sample (up to 20):")
        hdr = f"  {'Name 1':<30} {'Src1':<18} {'Name 2':<30} {'Src2':<18} {'Cross'}"
        print(hdr)
        print("  " + "─" * (len(hdr) - 2))
        for row in fn_rows[:20]:
            print(f"  {row['Name 1']:<30} {row['Source 1']:<18} "
                  f"{row['Name 2']:<30} {row['Source 2']:<18} {'✓' if row['Cross'] else ''}")

    # ── 10. False positives ────────────────────────────────────
    print("\n" + "=" * 62)
    print("SECTION 7 — FALSE POSITIVES (LLM merged, Senzing did not)")
    print("=" * 62)

    fp_rows = []
    for r1, r2 in sorted(fp):
        m1 = record_meta.get(r1, {})
        m2 = record_meta.get(r2, {})
        fp_rows.append({
            "Name 1":   m1.get("name", r1)[:30],
            "Source 1": m1.get("data_source", "?"),
            "Name 2":   m2.get("name", r2)[:30],
            "Source 2": m2.get("data_source", "?"),
            "Cross":    m1.get("data_source") != m2.get("data_source"),
        })

    cross_fp = sum(1 for r in fp_rows if r["Cross"])
    print(f"\n  Total incorrect merges: {len(fp_rows)}")
    print(f"    Cross-source:   {cross_fp}")
    print(f"    Within-source:  {len(fp_rows) - cross_fp}")
    if fp_rows:
        print(f"\n  Sample (up to 20):")
        hdr = f"  {'Name 1':<30} {'Src1':<18} {'Name 2':<30} {'Src2':<18} {'Cross'}"
        print(hdr)
        print("  " + "─" * (len(hdr) - 2))
        for row in fp_rows[:20]:
            print(f"  {row['Name 1']:<30} {row['Source 1']:<18} "
                  f"{row['Name 2']:<30} {row['Source 2']:<18} {'✓' if row['Cross'] else ''}")

    # ── 11. Match-key signal analysis ─────────────────────────
    print("\n" + "=" * 62)
    print("SECTION 8 — LLM MATCH-KEY SIGNAL ANALYSIS")
    print("=" * 62)

    # Collect match keys per LLM cluster, check if each pair is TP or FP
    llm_cl_to_records = defaultdict(list)
    for k, cid in llm_filtered.items():
        llm_cl_to_records[cid].append(k)

    sig_total   = Counter()
    sig_correct = Counter()
    sig_wrong   = Counter()

    for cid, recs in llm_cl_to_records.items():
        if len(recs) < 2:
            continue
        signals = set()
        for k in recs:
            mk = llm_match_key.get(k, "")
            for feat in re.findall(r"\+([A-Z_]+)", mk):
                signals.add(feat)

        for r1, r2 in combinations(sorted(recs), 2):
            pair = tuple(sorted([r1, r2]))
            is_tp = pair in tp
            for sig in signals:
                sig_total[sig] += 1
                (sig_correct if is_tp else sig_wrong)[sig] += 1

    if sig_total:
        print(f"\n  {'Signal':<22} {'Total':>7} {'Correct':>8} {'Wrong':>7} {'Accuracy':>9}")
        print("  " + "─" * 55)
        for sig in sorted(sig_total, key=sig_total.get, reverse=True):
            acc = sig_correct[sig] / sig_total[sig] if sig_total[sig] else 0
            print(f"  {sig:<22} {sig_total[sig]:>7} {sig_correct[sig]:>8} "
                  f"{sig_wrong[sig]:>7} {acc:>8.1%}")
    else:
        print("\n  (No match-key data available in LLM output)")

    # ── 12. Senzing match-key analysis for missed merges ──────
    print("\n" + "=" * 62)
    print("SECTION 9 — SENZING MATCH FEATURES: FOUND vs MISSED")
    print("=" * 62)

    sz_entity_mk = defaultdict(list)
    for r in sz_records:
        if r["match_key"]:
            sz_entity_mk[r["entity_id"]].append(r["match_key"])

    tp_feats = Counter()
    fn_feats = Counter()
    for pair_set, feat_counter in ((tp, tp_feats), (fn, fn_feats)):
        for r1, r2 in pair_set:
            eid = sz_filtered[r1]
            for mk in sz_entity_mk.get(eid, []):
                for feat in re.findall(r"\+([A-Z_]+)", mk):
                    feat_counter[feat] += 1

    all_feats = sorted(
        set(tp_feats) | set(fn_feats),
        key=lambda x: tp_feats.get(x, 0) + fn_feats.get(x, 0), reverse=True
    )
    if all_feats:
        print(f"\n  {'Feature':<28} {'Found (TP)':>10} {'Missed (FN)':>12}")
        print("  " + "─" * 52)
        for feat in all_feats:
            print(f"  {feat:<28} {tp_feats.get(feat, 0):>10} {fn_feats.get(feat, 0):>12}")

    # ── 13. Relationship comparison ───────────────────────────
    print("\n" + "=" * 62)
    print("SECTION 10 — RELATIONSHIP COMPARISON")
    print("=" * 62)

    # Expand Senzing entity-level rels to record-key pairs
    sz_ent_to_keys = defaultdict(set)
    for k, eid in sz_filtered.items():
        sz_ent_to_keys[eid].add(k)

    sz_rel_pairs = set()
    for rel in sz_rels_raw:
        e1, e2 = int(rel["min_res_ent_id"]), int(rel["max_res_ent_id"])
        for k1 in sz_ent_to_keys.get(e1, set()):
            for k2 in sz_ent_to_keys.get(e2, set()):
                sz_rel_pairs.add(tuple(sorted([k1, k2])))

    # Build LLM related pairs from RELATED_ENTITIES
    llm_rel_pairs = set()
    for rel in llm_rels:
        eid_a = rel.get("ENTITY_ID")
        match_key_rel = rel.get("MATCH_KEY", "")
        # RELATED_ENTITIES only gives entity_id of the related entity;
        # we need to find all keys in that entity
        # Build eid -> keys from llm_filtered
    # Rebuild llm eid -> keys from the full JSONL for relationship mapping
    llm_eid_to_keys = defaultdict(set)
    for k, cid in llm_filtered.items():
        llm_eid_to_keys[cid].add(k)

    # Re-parse JSONL for related entities (entity-level, like Senzing rels)
    llm_entity_rels = []
    with open(args.jsonl) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            eid = obj["RESOLVED_ENTITY"]["ENTITY_ID"]
            for rel in obj.get("RELATED_ENTITIES", []):
                rel_eid = rel.get("ENTITY_ID")
                if rel_eid and min(eid, rel_eid) not in {r[0] for r in llm_entity_rels}:
                    llm_entity_rels.append((min(eid, rel_eid), max(eid, rel_eid)))

    for e1, e2 in set(llm_entity_rels):
        for k1 in llm_eid_to_keys.get(e1, set()):
            for k2 in llm_eid_to_keys.get(e2, set()):
                llm_rel_pairs.add(tuple(sorted([k1, k2])))

    rel_tp  = llm_rel_pairs & sz_rel_pairs
    rel_fp  = llm_rel_pairs - sz_rel_pairs
    rel_fn  = sz_rel_pairs  - llm_rel_pairs

    rel_prec = len(rel_tp) / len(llm_rel_pairs) if llm_rel_pairs else 0
    rel_rec  = len(rel_tp) / len(sz_rel_pairs)  if sz_rel_pairs  else 0

    print(f"\n  Senzing relationship record pairs:    {len(sz_rel_pairs):>6,}")
    print(f"  LLM ER relationship record pairs:     {len(llm_rel_pairs):>6,}")
    print(f"  Overlap:                              {len(rel_tp):>6,}")
    print(f"  LLM only (potential over-detection):  {len(rel_fp):>6,}")
    print(f"  Senzing only (missed by LLM):         {len(rel_fn):>6,}")
    print(f"\n  Relationship Precision: {rel_prec:.4f}")
    print(f"  Relationship Recall:    {rel_rec:.4f}")

    # ── 14. Summary dashboard (text) ──────────────────────────
    print("\n" + "=" * 62)
    print("COMPREHENSIVE SUMMARY")
    print("=" * 62)

    src_counts = Counter(v["data_source"] for v in record_meta.values())
    src_str = "  ".join(f"{src}: {cnt}" for src, cnt in sorted(src_counts.items()))

    print(f"""
  DATASET
    Records in common:           {len(common_keys):>8,}
    Data sources:                {src_str}

  RESOLUTION OVERVIEW          {'Senzing':>10}  {'LLM ER':>8}
    Resolved entities:         {len(sz_cluster_sizes):>10,}  {len(llm_cluster_sizes):>8,}
    Multi-record clusters:     {sum(1 for s in sz_sizes if s > 1):>10,}  {sum(1 for s in llm_sizes if s > 1):>8,}
    Singletons:                {sz_sizes.count(1):>10,}  {llm_sizes.count(1):>8,}
    Merge pairs:               {len(sz_pairs):>10,}  {len(llm_pairs):>8,}
    Largest cluster:           {max(sz_sizes):>10,}  {max(llm_sizes):>8,}

  PAIRWISE MERGE QUALITY (vs Senzing ground truth)
    True Positives:            {len(tp):>8,}
    False Positives:           {len(fp):>8,}
    False Negatives:           {len(fn):>8,}
    Precision:                 {precision:>8.4f}
    Recall:                    {recall:>8.4f}
    F1 Score:                  {f1:>8.4f}

  B-CUBED CLUSTERING QUALITY
    Precision:                 {bcubed_p:>8.4f}
    Recall:                    {bcubed_r:>8.4f}
    F1:                        {bcubed_f1:>8.4f}

  ENTITY-LEVEL AGREEMENT (multi-record entities only)
    Exact matches:             {sum(1 for e in multi if e['category']=='exact_match'):>4} / {len(multi)}
    Split by LLM:              {sum(1 for e in multi if e['category'] in ('split','split_and_mixed')):>4} / {len(multi)}
    Superset (over-merged):    {sum(1 for e in multi if e['category']=='superset'):>4} / {len(multi)}
""")

    # ── 15. Plots ──────────────────────────────────────────────
    print("Generating charts…")

    # ---- Fig 1: Confusion matrix + metric bars ----
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    cm = np.array([[len(tp), len(fn)], [len(fp), tn]])
    cm_pct = cm / cm.sum() * 100
    ax = axes[0]
    ax.imshow(cm_pct, cmap="Blues", aspect="auto")
    ax.set_xticks([0, 1]); ax.set_yticks([0, 1])
    ax.set_xticklabels(["Merge\n(Senzing)", "No Merge\n(Senzing)"])
    ax.set_yticklabels(["Merge\n(LLM)", "No Merge\n(LLM)"])
    ax.set_xlabel("Ground Truth (Senzing)"); ax.set_ylabel("LLM ER")
    ax.set_title("Pairwise Decision Confusion Matrix")
    for i in range(2):
        for j in range(2):
            label = ["TP", "FN", "FP", "TN"][i * 2 + j]
            color = "white" if cm_pct[i, j] > cm_pct.max() * 0.5 else "black"
            ax.text(j, i, f"{label}\n{cm_pct[i,j]:.2f}%\n({cm[i,j]:,})",
                    ha="center", va="center", fontsize=11, fontweight="bold", color=color)

    ax2 = axes[1]
    metrics_dict = {
        "Pairwise\nPrecision": precision, "Pairwise\nRecall": recall,
        "Pairwise\nF1": f1, "B-Cubed\nPrecision": bcubed_p,
        "B-Cubed\nRecall": bcubed_r, "B-Cubed\nF1": bcubed_f1,
    }
    bars = ax2.bar(metrics_dict.keys(), metrics_dict.values(),
                   color=["#2196F3", "#FF9800", "#4CAF50"] * 2,
                   edgecolor="black", linewidth=0.5)
    ax2.set_ylim(0, 1.15); ax2.set_ylabel("Score")
    ax2.set_title("Entity Resolution Quality Metrics")
    ax2.axhline(y=1.0, color="gray", linestyle="--", alpha=0.3)
    for bar, val in zip(bars, metrics_dict.values()):
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.02,
                 f"{val:.3f}", ha="center", va="bottom", fontweight="bold", fontsize=10)

    plt.tight_layout()
    path1 = os.path.join(out_dir, "er_comparison_metrics.png")
    plt.savefig(path1, dpi=150)
    plt.close()
    print(f"  Saved: {path1}")

    # ---- Fig 2: Cluster size distribution ----
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    max_size = max(max(sz_sizes), max(llm_sizes))
    bins = range(1, max_size + 2)

    ax = axes[0]
    ax.hist(sz_sizes, bins=bins, alpha=0.6, color="#2196F3",
            label=f"Senzing ({len(sz_cluster_sizes)} clusters)",
            edgecolor="black", linewidth=0.5, align="left")
    ax.hist(llm_sizes, bins=bins, alpha=0.6, color="#FF9800",
            label=f"LLM ER ({len(llm_cluster_sizes)} clusters)",
            edgecolor="black", linewidth=0.5, align="left")
    ax.set_xlabel("Cluster Size"); ax.set_ylabel("# Clusters")
    ax.set_title("Cluster Size Distribution"); ax.legend()
    ax.set_xticks(range(1, min(max_size + 1, 20)))

    ax2 = axes[1]
    ax2.axis("off")
    tbl = [
        ["Metric", "Senzing", "LLM ER"],
        ["Total clusters", len(sz_cluster_sizes), len(llm_cluster_sizes)],
        ["Singletons", sz_sizes.count(1), llm_sizes.count(1)],
        ["Multi-record", sum(1 for s in sz_sizes if s > 1), sum(1 for s in llm_sizes if s > 1)],
        ["Records in merges", sum(s for s in sz_sizes if s > 1), sum(s for s in llm_sizes if s > 1)],
        ["Largest cluster", max(sz_sizes), max(llm_sizes)],
        ["Mean size", f"{np.mean(sz_sizes):.2f}", f"{np.mean(llm_sizes):.2f}"],
    ]
    table = ax2.table(cellText=tbl, loc="center", cellLoc="center")
    table.auto_set_font_size(False); table.set_fontsize(11); table.scale(1.2, 1.8)
    for j in range(3):
        table[0, j].set_facecolor("#E0E0E0")
        table[0, j].set_text_props(fontweight="bold")
    ax2.set_title("Cluster Statistics", pad=20)

    plt.tight_layout()
    path2 = os.path.join(out_dir, "er_comparison_clusters.png")
    plt.savefig(path2, dpi=150)
    plt.close()
    print(f"  Saved: {path2}")

    # ---- Fig 3: Cross-source vs within-source ----
    fig, ax = plt.subplots(figsize=(10, 5))
    cats = list(cross_results.keys())
    x = np.arange(len(cats))
    width = 0.25
    for i, metric in enumerate(("Precision", "Recall", "F1")):
        vals = [cross_results[c][metric] for c in cats]
        bars_ = ax.bar(x + i * width, vals, width, label=metric,
                       color=["#2196F3", "#FF9800", "#4CAF50"][i],
                       edgecolor="black", linewidth=0.5)
        for bar_, val in zip(bars_, vals):
            ax.text(bar_.get_x() + bar_.get_width() / 2, bar_.get_height() + 0.02,
                    f"{val:.3f}", ha="center", va="bottom", fontsize=10, fontweight="bold")
    ax.set_xticks(x + width); ax.set_xticklabels([c.upper() for c in cats])
    ax.set_ylim(0, 1.15); ax.set_ylabel("Score")
    ax.set_title("Pairwise Metrics: Cross-Source vs Within-Source")
    ax.legend(); ax.axhline(y=1.0, color="gray", linestyle="--", alpha=0.3)
    plt.tight_layout()
    path3 = os.path.join(out_dir, "er_comparison_cross_source.png")
    plt.savefig(path3, dpi=150)
    plt.close()
    print(f"  Saved: {path3}")

    # ---- Fig 4: Entity-level agreement pie ----
    if multi:
        from collections import Counter as _Counter
        cat_counts = _Counter(e["category"] for e in multi)
        colors_map = {
            "exact_match": "#4CAF50", "split": "#FF9800",
            "superset": "#F44336", "split_and_mixed": "#9C27B0", "partial": "#607D8B",
        }
        fig, ax = plt.subplots(figsize=(7, 6))
        ax.pie(
            cat_counts.values(),
            labels=[c.replace("_", " ").title() for c in cat_counts.keys()],
            colors=[colors_map.get(c, "#999") for c in cat_counts.keys()],
            autopct="%1.1f%%", startangle=90,
        )
        ax.set_title(f"Entity-Level Agreement on Multi-Record Entities (n={len(multi)})")
        plt.tight_layout()
        path4 = os.path.join(out_dir, "er_comparison_entity_agreement.png")
        plt.savefig(path4, dpi=150)
        plt.close()
        print(f"  Saved: {path4}")

    # ---- Fig 5: Match signal accuracy ----
    if sig_total:
        signals = sorted(sig_total, key=sig_total.get, reverse=True)
        fig, ax = plt.subplots(figsize=(12, 5))
        x = range(len(signals))
        ax.bar(x, [sig_correct[s] for s in signals],
               label="Correct (TP)", color="#4CAF50", edgecolor="black", linewidth=0.5)
        ax.bar(x, [sig_wrong[s] for s in signals],
               bottom=[sig_correct[s] for s in signals],
               label="Incorrect (FP)", color="#F44336", edgecolor="black", linewidth=0.5)
        ax.set_xticks(x); ax.set_xticklabels(signals, rotation=45, ha="right")
        ax.set_ylabel("# Pairs"); ax.set_title("LLM Match Signals: Correct vs Incorrect")
        ax.legend()
        plt.tight_layout()
        path5 = os.path.join(out_dir, "er_comparison_signals.png")
        plt.savefig(path5, dpi=150)
        plt.close()
        print(f"  Saved: {path5}")

    print("\nDone.")


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Compare LLM ER output (JSONL) against Senzing ground truth in PostgreSQL."
    )
    parser.add_argument("jsonl", help="Path to llm_er_output.jsonl")
    parser.add_argument("--host",     default=os.getenv("POSTGRES_HOST",     "localhost"))
    parser.add_argument("--port",     default=int(os.getenv("POSTGRES_PORT", "5432")), type=int)
    parser.add_argument("--db",       default=os.getenv("POSTGRES_DB",       "tokenomics"))
    parser.add_argument("--user",     default=os.getenv("POSTGRES_USER",     "postgres"))
    parser.add_argument("--password", default=os.getenv("POSTGRES_PASSWORD", "workshop"))
    args = parser.parse_args()

    if not os.path.isfile(args.jsonl):
        print(f"Error: file not found: {args.jsonl}", file=sys.stderr)
        sys.exit(1)

    run_comparison(args)


if __name__ == "__main__":
    main()