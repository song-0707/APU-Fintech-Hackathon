"""One-off data migration: backfill the `key` property that
graph_builder._normalize_key introduced onto Person/Project/EntityType-
taxonomy (Organization/System/Policy/Document/Concept) nodes created before
that change, and merge any nodes that turn out to share a key — e.g. an
existing "Sarah Park" node and an existing "sarah park" node — into a
single survivor before relying on the new person_key/project_key/
organization_key/system_key/policy_key/document_key/concept_key uniqueness
constraints (see graph_builder.ensure_constraints).

Also covers the earlier, now-retired generic `Entity` label: if this
database ever ran the brief single-generic-label version of this change,
any leftover :Entity nodes are NOT touched by this script (there's no
mechanical way to know which of the 7 taxonomy labels each one should
become — that's a classification judgment call, not a string-normalization
one). Reprocessing the source meeting is the straightforward way to
replace them: build_from_meeting will recreate each triple's subject/
object under the correct label.

Why this can't just happen automatically at startup: ensure_constraints()
runs on every boot and only adds a constraint, it never rewrites existing
data — nodes from before this change have no `key` property at all, so a
new meeting's `MERGE (p:Person {key: $key})` won't match them and will
create a THIRD node instead of reusing either old one. Run this once, soon
after deploying the normalization change:

    cd backend
    python -m app.scripts.merge_duplicate_names --dry-run   # preview first
    python -m app.scripts.merge_duplicate_names              # then apply

Safe to re-run: nodes that already have a `key` and no colliding sibling
are left untouched.

No APOC dependency (Aura Free / self-hosted Community edition may not have
it installed) — relationships are re-pointed by hand, so every relationship
type that can touch a node of these labels in this schema (see the module
docstring on graph_builder) must be listed in _RELOCATABLE_TYPES below or
explicitly handled. Adding a new relationship type to the schema without
updating this file means a future run of this script would raise rather
than silently drop data.
"""
import argparse
import logging

from app.graph.graph_builder import _normalize_key
from app.graph.neo4j_service import run_query

logger = logging.getLogger(__name__)

# The 7 name-identified labels graph_builder writes — see EntityType.
_TAXONOMY_LABELS = ("Person", "Project", "Organization", "System", "Policy", "Document", "Concept")

# label -> which relationship types (any direction) can touch a node of
# that label, per graph_builder's schema docstring, excluding RELATES_AS
# (allowed for every label unconditionally below, since it carries a
# `predicate` property that must be preserved on the recreated edge, and —
# now that a knowledge-triple subject/object can resolve to *any* of the 7
# taxonomy labels, not just a generic Entity — any of them can be a
# RELATES_AS endpoint). MENTIONED_IN is likewise possible on every label,
# including Person/Project, once a triple resolves to one of those: e.g. a
# Person who's a triple subject but never an actual meeting participant.
_RELOCATABLE_TYPES = {
    "Person": {"PARTICIPATED_IN", "MADE_BY", "ASSIGNED_TO", "MENTIONED_IN"},
    "Project": {"RELATES_TO", "MENTIONED_IN"},
    "Organization": {"MENTIONED_IN"},
    "System": {"MENTIONED_IN"},
    "Policy": {"MENTIONED_IN"},
    "Document": {"MENTIONED_IN"},
    "Concept": {"MENTIONED_IN"},
}


def _relocate_relationships(label: str, dup_id: str, survivor_id: str) -> None:
    """Re-point every relationship on `dup` (any direction) onto
    `survivor`, then delete `dup`. Cypher can't parameterize a relationship
    type, so each type is recreated with its own literal MERGE — the
    allowlist above is what keeps this from silently ignoring a
    relationship type this script doesn't know about."""
    rows = run_query(
        "MATCH (n) WHERE elementId(n) = $id "
        "OPTIONAL MATCH (n)-[r]-(other) "
        "RETURN elementId(r) AS rel_id, type(r) AS rel_type, properties(r) AS props, "
        "elementId(startNode(r)) = $id AS outgoing, elementId(other) AS other_id",
        id=dup_id,
    )
    allowed = _RELOCATABLE_TYPES[label] | {"RELATES_AS"}
    for row in rows:
        if row["rel_id"] is None:
            continue
        rel_type = row["rel_type"]
        if rel_type not in allowed:
            raise ValueError(
                f"merge_duplicate_names: {label} node has an unexpected {rel_type} relationship — "
                f"add it to _RELOCATABLE_TYPES (or a dedicated branch, like RELATES_AS) before re-running"
            )
        from_id, to_id = (survivor_id, row["other_id"]) if row["outgoing"] else (row["other_id"], survivor_id)
        if rel_type == "RELATES_AS":
            run_query(
                "MATCH (a) WHERE elementId(a) = $from_id MATCH (b) WHERE elementId(b) = $to_id "
                "MERGE (a)-[:RELATES_AS {predicate: $predicate}]->(b)",
                from_id=from_id, to_id=to_id, predicate=row["props"].get("predicate"),
            )
        else:
            run_query(
                f"MATCH (a) WHERE elementId(a) = $from_id MATCH (b) WHERE elementId(b) = $to_id "
                f"MERGE (a)-[:{rel_type}]->(b)",
                from_id=from_id, to_id=to_id,
            )
    run_query("MATCH (n) WHERE elementId(n) = $id DETACH DELETE n", id=dup_id)


def _merge_duplicates_for_label(label: str, dry_run: bool) -> int:
    """Group every node of `label` by its normalized key, backfill `key`
    on singletons, and merge every group with more than one member onto an
    (arbitrarily chosen, alphabetically first for determinism) survivor."""
    rows = run_query(f"MATCH (n:{label}) RETURN elementId(n) AS id, n.name AS name, n.key AS key")
    groups: dict[str, list[dict]] = {}
    for row in rows:
        key = row["key"] or _normalize_key(row["name"])
        groups.setdefault(key, []).append(row)

    merged = 0
    for key, group in sorted(groups.items()):
        group.sort(key=lambda r: r["name"])
        survivor = group[0]
        for dup in group[1:]:
            merged += 1
            logger.info(f"[{label}] merging {dup['name']!r} into {survivor['name']!r} (key={key!r})")
            if not dry_run:
                _relocate_relationships(label, dup["id"], survivor["id"])
        if not dry_run:
            run_query(f"MATCH (n:{label}) WHERE elementId(n) = $id SET n.key = $key", id=survivor["id"], key=key)
    return merged


# Same-person cases _normalize_key can't infer on its own — a short name
# vs. a full name (e.g. "Kam" vs "Kam Xin Le") is a different string, not a
# case/whitespace variant of the same one, so no amount of key-normalization
# tuning merges them automatically; only a human can say they're the same
# identity. Add entries here as your team spots them: (label, the name the
# merged node should end up with, [every existing raw name that's actually
# this person/thing]). Applied after the automatic pass above, so by the
# time this runs, any of the variants already normalization-merged into
# each other show up here as whichever one survived that pass.
_MANUAL_ALIASES: list[tuple[str, str, list[str]]] = [
    ("Person", "KAM XIN LE", ["Kam", "KAM", "Kam Xin Le"]),
    ("Person", "YAP EN YU", ["YAP (OPS)"]),
]


def _merge_manual_alias(label: str, canonical_name: str, variant_names: list[str], dry_run: bool) -> int:
    """Merge every existing `label` node whose exact .name is one of
    variant_names (or already canonical_name) into a single node, and set
    that survivor's .name/.key to canonical_name regardless of which raw
    node structurally survives — unlike the automatic pass, the target
    identity here is one the user chose, not whichever variant happened to
    sort first."""
    rows = run_query(
        f"MATCH (n:{label}) WHERE n.name IN $names RETURN elementId(n) AS id, n.name AS name",
        names=variant_names + [canonical_name],
    )
    if len(rows) < 2:
        return 0
    canonical_key = _normalize_key(canonical_name)
    survivor, dups = rows[0], rows[1:]
    for dup in dups:
        logger.info(f"[{label}] manual alias: merging {dup['name']!r} -> {canonical_name!r}")
    if not dry_run:
        for dup in dups:
            _relocate_relationships(label, dup["id"], survivor["id"])
        run_query(
            f"MATCH (n:{label}) WHERE elementId(n) = $id SET n.name = $name, n.key = $key",
            id=survivor["id"], name=canonical_name, key=canonical_key,
        )
    return len(dups)


def main(dry_run: bool = False) -> None:
    total = sum(_merge_duplicates_for_label(label, dry_run) for label in _TAXONOMY_LABELS)
    total += sum(_merge_manual_alias(*alias, dry_run) for alias in _MANUAL_ALIASES)
    verb = "Would merge" if dry_run else "Merged"
    logger.info(
        f"{verb} {total} duplicate node(s) across {', '.join(_TAXONOMY_LABELS)}, "
        f"plus {len(_MANUAL_ALIASES)} manual alias group(s)"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Log what would merge without writing anything")
    main(dry_run=parser.parse_args().dry_run)
