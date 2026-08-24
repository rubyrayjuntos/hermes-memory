# AGE Quirks & Workarounds

This file documents non-obvious Apache AGE behaviors discovered during production use of `hybrid-age`.

## Property Syntax

AGE uses `agtype`, not JSONB. Property maps use single-quoted Cypher syntax:

```sql
-- ✅ Correct
MATCH (v:Technology {name: 'PostgreSQL'})
SET v.properties = {name: 'PostgreSQL', version: '16'}

-- ❌ Wrong — double quotes are not valid in Cypher property maps
SET v.properties = {"name": "PostgreSQL"}
```

When reading vertex properties in Python, parse the full `agtype` string:

```python
v_str = row[0]  # '{"id": 123, "label": "Technology", "properties": {...}}::vertex'
if v_str.endswith("::vertex"):
    v_str = v_str[:-len("::vertex")]
v_data = json.loads(v_str)
name = v_data.get("properties", {}).get("name")
```

## No `->>` Operator

Unlike Postgres JSONB, AGE `agtype` does not support `->>`. You cannot extract a nested property directly in SQL. Parse the full vertex/edge string instead.

## Large IDs

`id(v)` in AGE returns `BIGINT` values that can exceed 32-bit range (e.g., `10133099161583617`). Always cast to string for JavaScript precision safety:

```javascript
const nodesDataset = new vis.DataSet(nodes.map(n => ({ ...n, id: String(n.id) })));
```

## Transaction Poisoning

A failed Cypher statement aborts the entire transaction, not just the statement. Isolate risky operations with savepoints:

```sql
SAVEPOINT sp_xxx;
SELECT * FROM cypher('hermes_knowledge', $$ ... $$) AS (...);
RELEASE SAVEPOINT sp_xxx;
```

If the Cypher fails, rollback to the savepoint instead of the whole transaction.

## `create_vlabel` / `create_elabel` Required

Labels must be pre-declared before creating vertices/edges with them:

```sql
-- Must run first
SELECT create_vlabel('hermes_knowledge', 'Person');
SELECT create_elabel('hermes_knowledge', 'USES');

-- Now this works
SELECT * FROM cypher('hermes_knowledge', $$ CREATE (p:Person {name: 'Ray'}) $$) AS (p agtype);
```

## No `ON CREATE SET`

AGE does not support `ON CREATE SET` in `MERGE` or `CREATE`:

```sql
-- ❌ Syntax error at "ON"
MERGE (v:Technology {name: 'PostgreSQL'})
ON CREATE SET v.created_at = now()
RETURN v

-- ✅ Workaround: read-modify-write in two statements
MERGE (v:Technology {name: 'PostgreSQL'})
RETURN v;
-- Then SET properties in a second query
```

## `coalesce()` Inside `MERGE` SET

Setting a property with `coalesce` in a `MERGE` SET clause fails in AGE. Use a two-statement read-modify-write pattern instead:

```sql
-- ✅ Correct pattern
MERGE (a)-[e:RELATED_TO]->(b)
RETURN e;
SET e.weight = coalesce(e.weight, 0) + 1;
```

## `label()` on NULL Returns Nothing

`label(NULL)` does not return a string — it returns nothing. Guard against null vertices:

```sql
MATCH (v)
WHERE v.properties IS NOT NULL
RETURN label(v), v.properties->>'name'
```

## Index Labels

Indexes on AGE graphs must be created on the underlying `ag_label` table, not with standard Postgres syntax:

```sql
-- ✅ Correct
CREATE INDEX idx_technology_name ON ag_catalog.ag_label
    USING btree ((properties->>'name'))
    WHERE name = 'Technology';

-- ❌ Wrong — this creates a standard index, not used by AGE
CREATE INDEX ON hermes_knowledge."Technology"(properties->>'name');
```

## Vacuum & Maintenance

AGE graphs do not automatically reclaim dead tuples. Run periodically:

```sql
VACUUM ANALYZE ag_catalog.ag_label;
VACUUM ANALYZE ag_catalog.ag_edge;
```

After heavy graph mutation, `ANALYZE` is critical for query planner accuracy.
