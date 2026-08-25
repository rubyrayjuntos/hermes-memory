-- 01_extensions.sql — extensions, graph, and label pre-declaration.
-- Runs once on first init of a fresh $POSTGRES_DATA directory (idempotent guards included).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;

SELECT create_graph('hermes_knowledge');

-- Vertex labels (plan §4). Pre-declared so 03_indexes.sql can reference the tables.
SELECT create_vlabel('hermes_knowledge', 'Person');
SELECT create_vlabel('hermes_knowledge', 'Project');
SELECT create_vlabel('hermes_knowledge', 'Technology');
SELECT create_vlabel('hermes_knowledge', 'Organization');
SELECT create_vlabel('hermes_knowledge', 'Concept');
SELECT create_vlabel('hermes_knowledge', 'Domain');
SELECT create_vlabel('hermes_knowledge', 'Skill');
SELECT create_vlabel('hermes_knowledge', 'Tool');
SELECT create_vlabel('hermes_knowledge', 'Repo');
SELECT create_vlabel('hermes_knowledge', 'File');
SELECT create_vlabel('hermes_knowledge', 'Module');
SELECT create_vlabel('hermes_knowledge', 'Dependency');
SELECT create_vlabel('hermes_knowledge', 'Standard');
SELECT create_vlabel('hermes_knowledge', 'Session');
SELECT create_vlabel('hermes_knowledge', 'Turn');

-- Edge labels (PascalCase per card spec).
SELECT create_elabel('hermes_knowledge', 'Uses');
SELECT create_elabel('hermes_knowledge', 'BuiltWith');
SELECT create_elabel('hermes_knowledge', 'WorksOn');
SELECT create_elabel('hermes_knowledge', 'PartOf');
SELECT create_elabel('hermes_knowledge', 'DependsOn');
SELECT create_elabel('hermes_knowledge', 'Implements');
SELECT create_elabel('hermes_knowledge', 'Imports');
SELECT create_elabel('hermes_knowledge', 'GovernedBy');
SELECT create_elabel('hermes_knowledge', 'Deprecates');
SELECT create_elabel('hermes_knowledge', 'Mentions');
SELECT create_elabel('hermes_knowledge', 'CoMentioned');
SELECT create_elabel('hermes_knowledge', 'SemanticallyRelated');
